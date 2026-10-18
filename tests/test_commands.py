"""Slash commands driven with a fake `discord` module (no network). SYNTHETIC data only."""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import time
import types

import pytest

from conftest import MINT


class FakeResponse:
    def __init__(self, log):
        self.log, self._done = log, False

    def is_done(self):
        return self._done

    async def defer(self, thinking=False, ephemeral=False):
        assert not self._done, "responded twice"
        self._done = True
        self.log.append(("defer", time.monotonic(), ephemeral))

    async def send_message(self, content=None, embed=None, ephemeral=False):
        assert not self._done, "send_message after the interaction was already acknowledged"
        self._done = True
        self.log.append(("send", content, embed, ephemeral))


class FakeFollowup:
    def __init__(self, log):
        self.log = log

    async def send(self, content=None, embed=None, ephemeral=False):
        self.log.append(("followup", content, embed, ephemeral))


class FakeInteraction:
    def __init__(self, admin=True):
        self.log = []
        self.response = FakeResponse(self.log)
        self.followup = FakeFollowup(self.log)
        self.user = types.SimpleNamespace(id=42, guild_permissions=types.SimpleNamespace(administrator=admin))
        self.guild = object()
        self.command = types.SimpleNamespace(name="test")


def fake_discord():
    discord = types.ModuleType("discord")

    class Embed(dict):
        def __init__(self, title=None, description=None, color=None, timestamp=None):
            super().__init__(title=title, description=description)

        def set_footer(self, text):
            self["footer"] = text

    class Intents:
        guilds = False

        @classmethod
        def none(cls):
            return cls()

    class Tree:
        def __init__(self):
            self.commands, self.on_error = {}, None

        def command(self, name, description):
            def deco(fn):
                self.commands[name] = types.SimpleNamespace(callback=fn, name=name)
                return self.commands[name]
            return deco

        def error(self, fn):
            self.on_error = fn
            return fn

        async def sync(self):
            return list(self.commands)

    class Bot:
        def __init__(self, command_prefix=None, intents=None):
            self.tree = Tree()

    app_commands = types.ModuleType("discord.app_commands")
    app_commands.describe = lambda **kw: (lambda fn: fn)
    ext = types.ModuleType("discord.ext")
    ext_commands = types.ModuleType("discord.ext.commands")
    ext_commands.Bot = Bot
    ext_commands.when_mentioned = object()
    ext.commands = ext_commands
    discord.Embed, discord.Intents, discord.Interaction = Embed, Intents, FakeInteraction
    discord.utils = types.SimpleNamespace(utcnow=lambda: None)
    discord.app_commands, discord.ext = app_commands, ext
    return {"discord": discord, "discord.app_commands": app_commands, "discord.ext": ext,
            "discord.ext.commands": ext_commands}


@pytest.fixture
def cmds(monkeypatch, settings, db):
    for name, mod in fake_discord().items():
        monkeypatch.setitem(sys.modules, name, mod)
    sys.modules.pop("northstar.discord_bot.commands", None)
    module = importlib.import_module("northstar.discord_bot.commands")
    bot = module.NorthstarCommands(settings, str(db.path))
    module.install_commands(bot)
    yield bot
    sys.modules.pop("northstar.discord_bot.commands", None)


def seed_snapshot(db):
    report = {"flags": [{"message": "LP status unknown"}], "reasons": ["gate failed: liquidity"]}
    db.execute("INSERT INTO research_snapshots(mint, ts, action, score, risk, report) VALUES (?,?,?,?,?,?)",
               (MINT, time.time(), "REJECT", 41.5, "CRITICAL", json.dumps(report)))


async def test_check_and_why_acknowledge_first_and_answer(cmds, db):
    seed_snapshot(db)
    for name in ("check", "why"):
        it = FakeInteraction()
        await cmds.tree.commands[name].callback(it, MINT)
        assert it.log[0][0] == "defer", f"/{name} must acknowledge before doing work"
        kind, _, embed, ephemeral = it.log[-1]
        assert kind == "followup" and ephemeral
        assert "REJECT" in embed["description"]
    assert "gate failed: liquidity" in embed["description"]          # /why shows the saved reasons


async def test_every_db_command_acknowledges_before_opening_the_database(cmds, db, monkeypatch):
    seen = []
    real = cmds.db

    def spy():
        seen.append(it.log[:])
        return real()
    monkeypatch.setattr(cmds, "db", spy)
    for name in ("scan", "calls", "watchlist", "performance", "northstar_status", "northstar_diagnostics",
                 "northstar_scan"):
        it = FakeInteraction()
        await cmds.tree.commands[name].callback(it)
        assert seen[-1] and seen[-1][0][0] == "defer", f"/{name} opened the DB before acknowledging"
        assert it.log[-1][0] == "followup"


async def test_watch_add_then_watchlist(cmds):
    it = FakeInteraction()
    await cmds.tree.commands["watch_add"].callback(it, MINT)
    it2 = FakeInteraction()
    await cmds.tree.commands["watchlist"].callback(it2)
    assert MINT in it2.log[-1][2]["description"]


async def test_non_admin_gets_a_reply_not_silence(cmds):
    it = FakeInteraction(admin=False)
    await cmds.tree.commands["northstar_status"].callback(it)
    assert it.log == [("send", "Administrator permission required.", None, True)]


async def test_errors_are_answered_instead_of_timing_out(cmds):
    it = FakeInteraction()
    await it.response.defer()
    await cmds.tree.on_error(it, RuntimeError("boom"))
    assert it.log[-1][0] == "followup" and "Command failed" in it.log[-1][1]
    fresh = FakeInteraction()
    await cmds.tree.on_error(fresh, RuntimeError("boom"))
    assert fresh.log[-1][0] == "send"


def test_commands_run_on_their_own_thread_and_loop(cmds, monkeypatch):
    import northstar.discord_bot.commands as module
    started = {}

    async def fake_run(settings, db_path):
        started["loop"] = asyncio.get_running_loop()
        started["thread"] = __import__("threading").current_thread().name
    monkeypatch.setattr(module, "run_commands", fake_run)
    t = module.start_commands_thread(cmds.settings, cmds.db_path)
    t.join(5)
    assert started["thread"] == "northstar-slash-commands" and t.daemon


async def test_research_summary_shows_bonding_curve_results(cmds):
    import northstar.discord_bot.commands as module
    from northstar.models import Action, RiskLevel
    out = types.SimpleNamespace(action=Action.WATCHLIST, score=82.0, risk=RiskLevel.INCOMPLETE,
                                reasons=["early-watch criteria met"], report={
        "mint": MINT, "lifecycle": "PRE_MIGRATION_BONDING_CURVE",
        "identity": {"name": "Banana Cat", "symbol": "BCAT"},
        "pre_migration": {"market_cap_sol_current": 149.15, "market_cap_change_pct": 316.5, "trade_events": 83,
                          "buys": 52, "sells": 31, "unique_buyers": 37, "creator": "CreatorWa11etAddressxxxxxxxxxxxx",
                          "holder_risk": "VERIFIED: top-10 9.0% / largest 1.2% of total supply (bonding curve excluded)"}})
    title, desc, _ = module.research_summary(out)
    assert title == "WATCHLIST • Banana Cat ($BCAT)"
    for part in ("Bonding curve (pre-migration)", "149.15 SOL", "+316.5%", "83 (52 buys / 31 sells)",
                 "top-10 9.0%", "Creator:"):
        assert part in desc
    assert "Price:" not in desc


async def test_check_runs_fresh_assessment_when_nothing_saved(cmds, monkeypatch):
    import northstar.discord_bot.commands as module
    from northstar.models import Action, RiskLevel
    fake = types.SimpleNamespace(action=Action.REJECT, score=None, risk=RiskLevel.CRITICAL, reasons=["gate failed: x"],
                                 report={"mint": MINT, "identity": {"name": "T", "symbol": "T"}})

    async def fake_research(bot, token):
        return fake
    monkeypatch.setattr(module, "run_research", fake_research)
    it = FakeInteraction()
    await cmds.tree.commands["check"].callback(it, MINT)
    assert it.log[0][0] == "defer"
    assert it.log[-1][0] == "followup" and "QUICK CHECK • REJECT" in it.log[-1][2]["title"]


class Gone(Exception):
    def __init__(self, code=10008):
        super().__init__(f"404 Not Found (error code: {code}): Unknown Message")
        self.code = code


class GoneFollowup(FakeFollowup):
    async def send(self, content=None, embed=None, ephemeral=False):
        raise Gone()


class FakeChannel:
    def __init__(self, log):
        self.log = log

    async def send(self, content=None, embed=None):
        self.log.append(("channel", content, embed))


def gone_interaction():
    it = FakeInteraction()
    it.followup = GoneFollowup(it.log)
    it.channel = FakeChannel(it.log)
    it.user.mention = "<@42>"
    return it


async def test_research_result_still_delivered_when_placeholder_was_deleted(cmds, monkeypatch):
    import northstar.discord_bot.commands as module
    from northstar.models import Action, RiskLevel
    fake = types.SimpleNamespace(action=Action.REJECT, score=None, risk=RiskLevel.CRITICAL, reasons=["x"],
                                 report={"mint": MINT, "identity": {"name": "T", "symbol": "T"}})

    async def fake_research(bot, token):
        return fake
    monkeypatch.setattr(module, "run_research", fake_research)
    it = gone_interaction()
    await cmds.tree.commands["research"].callback(it, MINT)
    kind, content, embed = it.log[-1]
    assert kind == "channel" and content == "<@42>" and embed["title"].startswith("REJECT")


async def test_private_reply_is_never_reposted_publicly(cmds, db):
    seed_snapshot(db)
    it = gone_interaction()
    await cmds.tree.commands["check"].callback(it, MINT)
    assert not any(e[0] == "channel" for e in it.log)


async def test_error_handler_quiet_when_reply_target_gone(cmds):
    it = gone_interaction()
    await it.response.defer()
    await cmds.tree.on_error(it, types.SimpleNamespace(original=Gone()))
    assert it.log == [("defer", it.log[0][1], False)]


async def test_other_followup_errors_still_raise(cmds):
    import northstar.discord_bot.commands as module
    it = gone_interaction()

    async def boom(**kw):
        raise Gone(code=50013)
    it.followup.send = boom
    await it.response.defer()
    with pytest.raises(Gone):
        await module._reply(it, content="x")


def test_socials_line(cmds):
    import northstar.discord_bot.commands as module
    soc = {"links": [{"kind": "x_account"}, {"kind": "telegram"}, {"kind": "website"}], "telegram_members": 2540,
           "recycled": []}
    assert module.socials_line(soc) == "X account • Telegram 2,540 members • website"
    bad = {"links": [{"kind": "x_post"}], "recycled": ["x_account:serial"], "telegram_members": None}
    assert module.socials_line(bad).startswith("⚠️ RECYCLED") and "X post link only" in module.socials_line(bad)


async def test_learning_commands(cmds, db):
    from northstar import learning as L
    L.init_schema(db)
    db.execute("INSERT INTO learned_settings(key, value, updated_at) VALUES ('plan_tp1', 2.5, 0)")
    it = FakeInteraction()
    await cmds.tree.commands["learning"].callback(it)
    assert it.log[0][0] == "defer"
    assert "`plan_tp1` = 2.5" in it.log[-1][2]["description"]
    it2 = FakeInteraction()
    await cmds.tree.commands["learning_reset"].callback(it2)
    assert "plan_tp1" in it2.log[-1][2]["description"]
    assert db.fetchone("SELECT COUNT(*) n FROM learned_settings")["n"] == 0
    it3 = FakeInteraction(admin=False)
    await cmds.tree.commands["learning_reset"].callback(it3)
    assert it3.log == [("send", "Administrator permission required.", None, True)]
