"""DiscordPublisher.start retry behaviour, with a fake `discord` module (no network)."""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from northstar.discord_bot.publisher import DiscordPublisher, PublishError


def fake_discord(script: list):
    """script: one entry per client created: 'ok', 'dns', 'login' or 'hang'."""
    mod = types.ModuleType("discord")

    class LoginFailure(Exception):
        pass

    class Intents:
        guilds = False

        @classmethod
        def none(cls):
            return cls()

    created = []

    class Client:
        def __init__(self, intents=None):
            self.mode = script[len(created)] if len(created) < len(script) else "ok"
            created.append(self)
            self._ready = asyncio.Event()
            self.user = "FakeBot#0001"
            self.closed = False

        async def start(self, token):
            if self.mode == "dns":
                raise OSError("Temporary failure in name resolution")
            if self.mode == "login":
                raise LoginFailure("Improper token has been passed.")
            if self.mode == "ok":
                self._ready.set()
            await asyncio.Event().wait()   # stay connected

        async def wait_until_ready(self):
            await self._ready.wait()

        async def close(self):
            self.closed = True

    mod.LoginFailure, mod.Intents, mod.Client = LoginFailure, Intents, Client
    mod.created = created
    return mod


def install(monkeypatch, script):
    mod = fake_discord(script)
    monkeypatch.setitem(sys.modules, "discord", mod)
    return mod


async def test_transient_dns_failure_is_retried(monkeypatch):
    mod = install(monkeypatch, ["dns", "dns", "ok"])
    pub = DiscordPublisher("token", 1, ready_timeout=1, start_attempts=5, retry_delay=0)
    await pub.start()
    assert len(mod.created) == 3 and mod.created[0].closed and mod.created[1].closed
    assert pub.client is mod.created[2]
    await pub.close()


async def test_rejected_token_fails_immediately(monkeypatch):
    mod = install(monkeypatch, ["login"])
    pub = DiscordPublisher("bad", 1, ready_timeout=1, start_attempts=5, retry_delay=0)
    with pytest.raises(PublishError) as e:
        await pub.start()
    assert not e.value.retryable and len(mod.created) == 1


async def test_gives_up_after_attempts_with_retryable_error(monkeypatch):
    mod = install(monkeypatch, ["hang", "dns"])
    pub = DiscordPublisher("token", 1, ready_timeout=0.05, start_attempts=2, retry_delay=0)
    with pytest.raises(PublishError) as e:
        await pub.start()
    assert e.value.retryable and "2 attempts" in str(e.value) and len(mod.created) == 2


async def test_link_buttons_are_sent_as_a_view_not_inside_the_embed(monkeypatch):
    mod = install(monkeypatch, ["ok"])

    class Button:
        def __init__(self, label, url, style):
            self.label, self.url, self.style = label, url, style

    class View:
        def __init__(self, timeout=None):
            self.items = []

        def add_item(self, item):
            self.items.append(item)

    class Embed:
        @staticmethod
        def from_dict(d):
            return dict(d)

    sent = {}

    class Channel:
        id = 1

        async def send(self, **kwargs):
            sent.update(kwargs)
            return types.SimpleNamespace(id=99)

    mod.ui = types.SimpleNamespace(View=View, Button=Button)
    mod.ButtonStyle = types.SimpleNamespace(link="link")
    mod.Embed = Embed
    pub = DiscordPublisher("token", 1, ready_timeout=1, retry_delay=0)
    pub.client.get_channel = lambda cid: Channel()
    await pub.publish({"title": "x", "_buttons": [{"label": "View on Axiom", "url": "https://axiom.trade/t/abc"}]})
    assert "_buttons" not in sent["embed"]
    assert sent["view"].items[0].url == "https://axiom.trade/t/abc" and sent["view"].items[0].style == "link"
