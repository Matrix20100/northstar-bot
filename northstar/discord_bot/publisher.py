"""Publishing backends.

DiscordPublisher - real bot (discord.py). Needs only the bot token, a channel id, and the
                   "Send Messages", "Embed Links" and "Read Message History" permissions.
DryRunPublisher  - writes embeds to a JSONL file; never touches Discord.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional, Protocol

log = logging.getLogger("northstar.discord")


class PublishError(Exception):
    def __init__(self, msg: str, retryable: bool, uncertain: bool = False):
        super().__init__(msg)
        self.retryable = retryable
        self.uncertain = uncertain  # the message may or may not have been delivered


class Publisher(Protocol):
    async def start(self) -> None: ...
    async def publish(self, embed: dict, channel_id: Optional[int] = None) -> tuple[int, int]: ...
    async def edit(self, channel_id: int, message_id: int, embed: dict) -> None: ...
    async def find_message_with(self, text: str, channel_id: Optional[int] = None, limit: int = 100) -> Optional[int]: ...
    async def close(self) -> None: ...


class DryRunPublisher:
    def __init__(self, path: str | Path, channel_id: int = 0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.channel_id = channel_id
        self._next = int(time.time() * 1000)

    async def start(self) -> None:
        log.info("dry-run publisher active: alerts are written to %s, nothing is posted", self.path)

    def _write(self, kind: str, payload: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "kind": kind, **payload}, default=str) + "\n")

    async def publish(self, embed: dict, channel_id: Optional[int] = None) -> tuple[int, int]:
        self._next += 1
        self._write("publish", {"message_id": self._next, "embed": embed})
        return channel_id or self.channel_id, self._next

    async def edit(self, channel_id: int, message_id: int, embed: dict) -> None:
        self._write("edit", {"message_id": message_id, "embed": embed})

    async def find_message_with(self, text: str, channel_id: Optional[int] = None, limit: int = 100) -> Optional[int]:
        if not self.path.exists():
            return None
        for line in reversed(self.path.read_text(encoding="utf-8").splitlines()[-2000:]):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("kind") == "publish" and text in json.dumps(row.get("embed")):
                return row.get("message_id")
        return None

    async def close(self) -> None:
        return None


def _split_buttons(embed: dict) -> tuple[dict, list[dict]]:
    """Separate Northstar's private `_buttons` key from the Discord embed payload."""
    if not isinstance(embed, dict) or "_buttons" not in embed:
        return embed, []
    clean = {k: v for k, v in embed.items() if k != "_buttons"}
    buttons = [b for b in (embed.get("_buttons") or [])
               if isinstance(b, dict) and b.get("label")
               and (str(b.get("url", "")).startswith("https://") or str(b.get("custom_id", "")).startswith("ns:"))]
    return clean, buttons[:5]


class DiscordPublisher:
    def __init__(self, token: str, channel_id: int, ready_timeout: float = 60,
                 start_attempts: int = 5, retry_delay: float = 15):
        import discord  # imported lazily so tests/analysis never need a Discord connection

        self._discord = discord
        self.token = token
        self.channel_id = channel_id
        self.ready_timeout = ready_timeout
        self.start_attempts = max(1, int(start_attempts))
        self.retry_delay = retry_delay
        self.client = self._new_client()
        self._task: Optional[asyncio.Task] = None

    def _new_client(self):
        intents = self._discord.Intents.none()
        intents.guilds = True
        return self._discord.Client(intents=intents)

    async def start(self) -> None:
        """Connect, retrying transient failures (e.g. a DNS blip) instead of exiting the process.

        A rejected token is permanent and fails immediately. Each retry uses a fresh client,
        because a discord.py client cannot be restarted after its start task has died.
        """
        last_error = "not ready within timeout"
        for attempt in range(1, self.start_attempts + 1):
            if attempt > 1:
                self.client = self._new_client()
            self._task = asyncio.create_task(self.client.start(self.token), name="discord-client")
            ready = asyncio.create_task(self.client.wait_until_ready())
            done, _ = await asyncio.wait({ready, self._task}, timeout=self.ready_timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if ready in done and not ready.cancelled() and ready.exception() is None:
                log.info("discord connected as %s", self.client.user)
                return
            ready.cancel()
            exc = None
            if self._task.done() and not self._task.cancelled():
                exc = self._task.exception()
            if exc is not None and isinstance(exc, self._discord.LoginFailure):
                raise PublishError("Discord login failed: the bot token was rejected", False)
            last_error = f"{type(exc).__name__}: {exc}" if exc is not None else "not ready within timeout"
            await self._discard_client()
            if attempt < self.start_attempts:
                delay = self.retry_delay * attempt
                log.warning("Discord connection attempt %d/%d failed (%s); retrying in %.0fs",
                            attempt, self.start_attempts, last_error[:200], delay)
                await asyncio.sleep(delay)
        raise PublishError(f"Discord not reachable after {self.start_attempts} attempts: {last_error[:200]}", True)

    async def _discard_client(self) -> None:
        try:
            await self.client.close()
        except Exception:  # noqa: BLE001 - best effort cleanup of a failed client
            pass
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _channel(self, channel_id: Optional[int]):
        cid = channel_id or self.channel_id
        ch = self.client.get_channel(cid)
        if ch is None:
            try:
                ch = await self.client.fetch_channel(cid)
            except self._discord.NotFound:
                raise PublishError(f"channel {cid} not found", False)
            except self._discord.Forbidden:
                raise PublishError(f"no access to channel {cid}", False)
        return ch

    def _wrap(self, e: Exception, after_send: bool = False) -> PublishError:
        d = self._discord
        if isinstance(e, d.Forbidden):
            return PublishError(f"forbidden: {e.text if hasattr(e, 'text') else e}", False)
        if isinstance(e, d.NotFound):
            return PublishError("message/channel not found", False)
        if isinstance(e, d.HTTPException):
            return PublishError(f"discord HTTP {e.status}", e.status >= 500 or e.status == 429,
                                uncertain=after_send and e.status >= 500)
        if isinstance(e, (asyncio.TimeoutError, OSError)):
            return PublishError(f"network: {type(e).__name__}", True, uncertain=after_send)
        return PublishError(f"{type(e).__name__}: {e}", False)

    def _view(self, buttons: list[dict]):
        if not buttons:
            return None
        view = self._discord.ui.View(timeout=None)
        for b in buttons:
            if b.get("custom_id"):
                # v3.0.0 "More info": answered by the slash-command bot (commands.py), not here
                view.add_item(self._discord.ui.Button(label=str(b["label"])[:80], custom_id=str(b["custom_id"])[:100],
                                                      style=self._discord.ButtonStyle.primary))
            else:
                view.add_item(self._discord.ui.Button(label=str(b["label"])[:80], url=b["url"],
                                                      style=self._discord.ButtonStyle.link))
        return view

    async def publish(self, embed: dict, channel_id: Optional[int] = None) -> tuple[int, int]:
        ch = await self._channel(channel_id)
        embed, buttons = _split_buttons(embed)
        kwargs = {"embed": self._discord.Embed.from_dict(embed)}
        view = self._view(buttons)
        if view is not None:
            kwargs["view"] = view
        try:
            msg = await asyncio.wait_for(ch.send(**kwargs), timeout=30)
        except Exception as e:  # noqa: BLE001 - mapped to PublishError
            raise self._wrap(e, after_send=True) from e
        finally:
            if view is not None and hasattr(view, "stop"):
                view.stop()          # this client never answers button clicks; commands.py does
        return ch.id, msg.id

    async def edit(self, channel_id: int, message_id: int, embed: dict) -> None:
        ch = await self._channel(channel_id)
        try:
            embed, buttons = _split_buttons(embed)
            kwargs = {"embed": self._discord.Embed.from_dict(embed)}
            view = self._view(buttons)
            if view is not None:
                kwargs["view"] = view
            msg = ch.get_partial_message(message_id)
            try:
                await asyncio.wait_for(msg.edit(**kwargs), timeout=30)
            finally:
                if view is not None and hasattr(view, "stop"):
                    view.stop()
        except Exception as e:  # noqa: BLE001
            raise self._wrap(e) from e

    async def find_message_with(self, text: str, channel_id: Optional[int] = None, limit: int = 100) -> Optional[int]:
        """Used after a restart/timeout to discover whether an alert was actually delivered."""
        ch = await self._channel(channel_id)
        try:
            async for msg in ch.history(limit=limit):
                if msg.author.id != self.client.user.id:
                    continue
                for e in msg.embeds:
                    if text in (e.footer.text or "") or text in (e.description or ""):
                        return msg.id
        except Exception as e:  # noqa: BLE001
            raise self._wrap(e) from e
        return None

    async def close(self) -> None:
        try:
            await self.client.close()
        finally:
            if self._task:
                self._task.cancel()
