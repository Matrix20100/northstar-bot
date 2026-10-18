
"""
PumpPortal WebSocket feed for new tokens, migrations and optional trades.

New-token and migration subscriptions are requested independently of
optional trade subscriptions. Trade streaming requires explicit opt-in
and an API key.

Features:
- Conservative event classification.
- Separate tracking for requested and observed trade subscriptions.
- Graceful handling of provider subscription errors.
- Disables metered trade requests after a PumpSwap minimum-balance error.
- Keeps free discovery subscriptions running when possible.
- Restores unexpired trade subscriptions after reconnects.
- Caps active trade subscriptions and expires old subscriptions.
- Tracks event counts and connection health.
- Avoids logging the API key or WebSocket URL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Awaitable, Callable, Optional
from urllib.parse import urlencode

log = logging.getLogger("northstar.pumpportal")

URL = "wss://pumpportal.fun/api/data"

OnEvent = Callable[[str, dict], Awaitable[None]]
HealthCallback = Callable[[str, bool, Optional[str]], None]

VALID_EVENT_KINDS = {"new_token", "migration", "trade"}


class PumpPortalStream:
    def __init__(
        self,
        on_event: OnEvent,
        api_key: Optional[str] = None,
        health_cb: Optional[HealthCallback] = None,
        enable_trade_stream: bool = False,
        max_trade_subscriptions: int = 100,
        trade_subscription_ttl_seconds: int = 300,
    ):
        self.on_event = on_event
        self.api_key = api_key
        self.health_cb = health_cb

        # Trade streaming is enabled only when explicitly requested
        # and an API key has been configured.
        self.enable_trade_stream = bool(enable_trade_stream and api_key)

        self.max_trade_subscriptions = max(
            1, min(int(max_trade_subscriptions), 100)
        )
        self.trade_subscription_ttl_seconds = max(
            60, int(trade_subscription_ttl_seconds)
        )

        self.connected = False
        self.connected_at: Optional[float] = None
        self.last_event_at: Optional[float] = None

        self.events_received = 0
        self.events_by_kind = {
            "new_token": 0,
            "migration": 0,
            "trade": 0,
            "unknown": 0,
        }

        self.reconnects = 0

        self._ws = None
        self._stop: Optional[asyncio.Event] = None

        # Mint -> time the trade subscription was requested.
        # This does not prove provider acceptance.
        self._trade_subscriptions: dict[str, float] = {}

        # Mints awaiting observed trade activity or acknowledgement.
        self._pending_subscriptions: set[str] = set()

        # Last observed trade event timestamp by mint.
        self._last_trade_by_mint: dict[str, float] = {}

        # Once PumpPortal rejects metered trade access for this process,
        # don't repeatedly request it after reconnects.
        self._trade_access_rejected = False

    def _health(
        self,
        healthy: bool,
        message: Optional[str] = None,
    ) -> None:
        if self.health_cb:
            try:
                self.health_cb("pumpportal", healthy, message)
            except Exception:
                log.exception("PumpPortal health callback failed")

    def stats(self) -> dict:
        """Return a safe snapshot of feed statistics."""
        return {
            "connected": self.connected,
            "trade_stream_enabled": self.enable_trade_stream,
            "trade_access_rejected": self._trade_access_rejected,
            "events_received": self.events_received,
            "events_by_kind": dict(self.events_by_kind),
            "active_trade_subscriptions": len(
                self._trade_subscriptions
            ),
            "pending_trade_subscriptions": len(
                self._pending_subscriptions
            ),
            "reconnects": self.reconnects,
            "last_event_at": self.last_event_at,
        }

    async def run(self, stop: asyncio.Event) -> None:
        try:
            import websockets
        except ImportError:
            self._health(False, "Missing dependency: websockets")
            log.error("PumpPortal requires the websockets package")
            raise

        self._stop = stop
        backoff = 2.0

        while not stop.is_set():
            try:
                url = URL

                if self.api_key:
                    url += "?" + urlencode({"api-key": self.api_key})

                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                    max_size=2**20,
                ) as ws:
                    self._ws = ws

                    # Free discovery subscriptions.
                    await ws.send(
                        json.dumps({"method": "subscribeNewToken"})
                    )
                    await ws.send(
                        json.dumps({"method": "subscribeMigration"})
                    )

                    # Restore unexpired trade subscriptions only if
                    # trade access remains enabled.
                    if (
                        self.enable_trade_stream
                        and not self._trade_access_rejected
                    ):
                        now = time.time()
                        self._remove_expired_local_subscriptions(now)

                        active = list(self._trade_subscriptions)

                        for start in range(0, len(active), 100):
                            batch = active[start : start + 100]

                            if batch:
                                await ws.send(
                                    json.dumps(
                                        {
                                            "method": "subscribeTokenTrade",
                                            "keys": batch,
                                        }
                                    )
                                )

                    self.connected = True
                    self.connected_at = time.time()

                    last_valid_event = time.monotonic()
                    last_status_log = last_valid_event

                    log.info(
                        "PumpPortal connected; discovery subscriptions "
                        "requested; metered_trade_stream=%s; "
                        "trade_access_rejected=%s; "
                        "restored_trade_subscriptions=%d",
                        self.enable_trade_stream,
                        self._trade_access_rejected,
                        len(self._trade_subscriptions),
                    )

                    self._health(True, None)

                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(),
                                timeout=30,
                            )
                        except asyncio.TimeoutError:
                            now_mono = time.monotonic()
                            quiet = now_mono - last_valid_event

                            if quiet >= 180:
                                raise TimeoutError(
                                    "No usable PumpPortal events for "
                                    f"{int(quiet)} seconds"
                                )

                            if now_mono - last_status_log >= 60:
                                log.info(
                                    "PumpPortal connected but quiet: "
                                    "events=%d new=%d migrations=%d "
                                    "trades=%d unknown=%d "
                                    "trade_subscriptions=%d "
                                    "quiet_seconds=%d",
                                    self.events_received,
                                    self.events_by_kind["new_token"],
                                    self.events_by_kind["migration"],
                                    self.events_by_kind["trade"],
                                    self.events_by_kind["unknown"],
                                    len(self._trade_subscriptions),
                                    int(quiet),
                                )
                                last_status_log = now_mono

                            continue

                        usable = await self._handle(raw)

                        if usable:
                            last_valid_event = time.monotonic()
                            backoff = 2.0

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                self.connected = False
                self.reconnects += 1

                message = f"{type(exc).__name__}: {exc}"[:200]

                self._health(False, message)

                log.warning(
                    "PumpPortal connection failed; reconnecting in %.0fs "
                    "(attempt=%d, error=%s)",
                    backoff,
                    self.reconnects,
                    type(exc).__name__,
                )

                try:
                    await asyncio.wait_for(
                        stop.wait(),
                        timeout=backoff + random.uniform(0, 1),
                    )
                except asyncio.TimeoutError:
                    pass

                backoff = min(backoff * 2, 120)

            finally:
                self.connected = False
                self._ws = None
                self._pending_subscriptions.clear()

        self.connected = False
        self._ws = None

        log.info("PumpPortal stream stopped")

    async def _handle(self, raw) -> bool:
        """Process a WebSocket frame; return True for a recognized event."""
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            log.debug("PumpPortal sent a non-JSON frame")
            return False

        if not isinstance(msg, dict):
            log.debug("PumpPortal sent a non-object JSON frame")
            return False

        # Subscription errors and acknowledgements may not contain a mint.
        error = msg.get("errors") or msg.get("error")

        if error:
            error_text = str(error)[:400]
            normalized_error = error_text.lower()

            if (
                "minimum balance" in normalized_error
                and "pumpswap" in normalized_error
            ):
                self._disable_trade_access(
                    "PumpPortal reported a PumpSwap minimum-balance "
                    "requirement."
                )
                return False

            log.warning(
                "PumpPortal subscription/error message: %s",
                error_text,
            )

            self._health(
                False,
                "PumpPortal reported a subscription error",
            )
            return False

        # Acknowledgements and status frames may not contain a mint.
        if not msg.get("mint"):
            log.debug(
                "PumpPortal acknowledgement/status frame: %s",
                str(msg)[:250],
            )
            return False

        mint = str(msg["mint"]).strip()

        if not mint:
            return False

        tx_type = str(msg.get("txType", "")).strip().lower()
        kind = self._classify_event(tx_type, msg)

        if kind == "unknown":
            self.events_by_kind["unknown"] += 1

            log.debug(
                "Ignoring unknown PumpPortal event type=%r fields=%s",
                tx_type,
                sorted(msg.keys()),
            )
            return False

        now = time.time()

        if kind == "new_token":
            await self._subscribe_to_trades(mint, now)

        if kind == "trade":
            self._last_trade_by_mint[mint] = now
            self._pending_subscriptions.discard(mint)

        self.last_event_at = now
        self.events_received += 1

        self.events_by_kind[kind] = (
            self.events_by_kind.get(kind, 0) + 1
        )

        if kind == "trade":
            log.debug(
                "PumpPortal trade event received for mint=%s txType=%s",
                mint,
                tx_type,
            )

        self._health(True, None)

        try:
            await self.on_event(kind, msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "PumpPortal event callback failed for kind=%s mint=%s",
                kind,
                mint,
            )

        return True

    def _disable_trade_access(self, reason: str) -> None:
        """
        Disable metered trade requests after an explicit provider rejection.

        Discovery subscriptions are not intentionally changed here.
        """
        first_rejection = not self._trade_access_rejected

        self._trade_access_rejected = True
        self.enable_trade_stream = False

        self._trade_subscriptions.clear()
        self._pending_subscriptions.clear()
        self._last_trade_by_mint.clear()

        if first_rejection:
            # Expected on the free tier: trades are read from the chain instead, so this is a
            # notice, not a failure.
            log.info(
                "%s Paid trade stream not available (free tier) - trades are read on-chain "
                "instead. New-token and migration feeds stay active.",
                reason,
            )

    @staticmethod
    def _classify_event(tx_type: str, msg: dict) -> str:
        """Classify known event types without guessing unknown ones."""
        if tx_type in {"buy", "sell"}:
            return "trade"

        if tx_type in {"migrate", "migration"}:
            return "migration"

        if tx_type in {"create", "newtoken", "new_token"}:
            return "new_token"

        # Use known creation fields only when txType is absent.
        if not tx_type and any(
            field in msg
            for field in (
                "name",
                "symbol",
                "uri",
                "bondingCurveKey",
            )
        ):
            return "new_token"

        return "unknown"

    async def _subscribe_to_trades(
        self,
        mint: str,
        now: Optional[float] = None,
    ) -> None:
        """Request a trade subscription only when access is enabled."""
        if (
            not self.enable_trade_stream
            or self._trade_access_rejected
            or self._ws is None
        ):
            return

        now = now or time.time()

        await self._expire_trade_subscriptions(now)

        if mint in self._trade_subscriptions:
            return

        if mint in self._pending_subscriptions:
            return

        if len(self._trade_subscriptions) >= self.max_trade_subscriptions:
            log.debug(
                "Trade subscription cap reached; skipping mint=%s cap=%d",
                mint,
                self.max_trade_subscriptions,
            )
            return

        try:
            await self._ws.send(
                json.dumps(
                    {
                        "method": "subscribeTokenTrade",
                        "keys": [mint],
                    }
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "Could not request trade subscription for mint=%s: %s",
                mint,
                type(exc).__name__,
            )
            return

        # This records a request, not confirmed provider acceptance.
        self._trade_subscriptions[mint] = now
        self._pending_subscriptions.add(mint)

        log.debug(
            "Requested PumpPortal trade subscription for mint=%s; "
            "requested=%d cap=%d",
            mint,
            len(self._trade_subscriptions),
            self.max_trade_subscriptions,
        )

    async def _expire_trade_subscriptions(
        self,
        now: Optional[float] = None,
    ) -> None:
        """Unsubscribe expired trade mints and remove local state."""
        if not self.enable_trade_stream:
            return

        now = now or time.time()

        expired = [
            mint
            for mint, subscribed_at in self._trade_subscriptions.items()
            if now - subscribed_at >= self.trade_subscription_ttl_seconds
        ]

        if not expired:
            return

        ws = self._ws

        if ws is not None:
            for start in range(0, len(expired), 100):
                batch = expired[start : start + 100]

                try:
                    await ws.send(
                        json.dumps(
                            {
                                "method": "unsubscribeTokenTrade",
                                "keys": batch,
                            }
                        )
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning(
                        "Could not send trade unsubscribe request: %s",
                        type(exc).__name__,
                    )

        for mint in expired:
            self._trade_subscriptions.pop(mint, None)
            self._pending_subscriptions.discard(mint)
            self._last_trade_by_mint.pop(mint, None)

        log.debug(
            "Expired %d PumpPortal trade subscriptions",
            len(expired),
        )

    def _remove_expired_local_subscriptions(
        self,
        now: Optional[float] = None,
    ) -> None:
        """Remove expired subscriptions before restoring after reconnect."""
        now = now or time.time()

        expired = [
            mint
            for mint, subscribed_at in self._trade_subscriptions.items()
            if now - subscribed_at >= self.trade_subscription_ttl_seconds
        ]

        for mint in expired:
            self._trade_subscriptions.pop(mint, None)
            self._pending_subscriptions.discard(mint)
            self._last_trade_by_mint.pop(mint, None)

        if expired:
            log.info(
                "Removed %d expired trade subscriptions before reconnect",
                len(expired),
            )