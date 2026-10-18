"""Post-publication tracking for PRE-MIGRATION early-watch posts.

Why this exists: an early-watch post is stored in ``early_signals`` (not ``alerts``), so the
standard Monitor never sees it, and the only other writer was the PumpPortal trade stream
(which is often unavailable). Once research re-evaluated the token and returned REJECT, the
post simply stopped changing.

This tracker is independent of both. Every ``early_watch_refresh_seconds`` it:
  1. reads each open post's bonding-curve account over Solana RPC (one batched call),
  2. decodes the market cap with the same parser research uses,
  3. updates baseline-safe tracking (``create_or_update_early_signal`` never moves the baseline),
  4. edits the original Discord message.

Honesty rules: a failed or undecodable read never writes zero or a made-up value - the last
valid observation stays and the post says when it was last read. If Northstar's own latest
research no longer qualifies the token, the post says so. Tracking stops at migration
(curve ``complete``) or after ``max_tracking_hours``.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from . import trade_plan as TP
from .alerts import AlertService
from .analysis.pda import pump_bonding_curve_address
from .config import Settings
from .db import Database
from .discord_bot.embeds import plan_event_embed
from .discord_bot.publisher import PublishError, Publisher
from .net import ProviderError
from .research import Researcher

log = logging.getLogger("northstar.early_tracker")

OPEN_STATUSES = ("SENT", "DRY_RUN")
MIN_CHANGE_FOR_EDIT = 0.005   # 0.5% market-cap move
STALE_FAILURES = 3            # consecutive failed reads before the post says its data is stale


class EarlyWatchTracker:
    def __init__(self, s: Settings, db: Database, publisher: Publisher, rpc=None, sol_price=None):
        self.s, self.db, self.pub, self.rpc = s, db, publisher, rpc
        self.sol_price = sol_price
        self._sol_usd: Optional[float] = None
        self._curve_keys: dict[str, Optional[str]] = {}
        self._failures: dict[str, int] = {}
        self._last_edit_cap: dict[str, Optional[float]] = {}
        self._last_edit_flags: dict[str, tuple] = {}

    # ------------------------------------------------------------------ helpers
    def _curve_key(self, mint: str) -> Optional[str]:
        if mint in self._curve_keys and self._curve_keys[mint]:
            return self._curve_keys[mint]
        # The bonding curve is a PDA of the mint: no launch event needed. (The account is still
        # verified on every read by the curve parser: owner + discriminator.)
        derived = pump_bonding_curve_address(mint)
        if derived:
            self._curve_keys[mint] = derived
            return derived
        row = self.db.fetchone(
            "SELECT payload FROM discovery_events WHERE mint=? AND source='pumpportal_new' "
            "ORDER BY ts ASC LIMIT 1", (mint,))
        key = None
        if row:
            try:
                key = (json.loads(row.get("payload") or "{}") or {}).get("bondingCurveKey")
            except (TypeError, ValueError):
                key = None
        self._curve_keys[mint] = str(key) if key else None
        return self._curve_keys[mint]

    def _research_notice(self, mint: str) -> Optional[str]:
        """If Northstar's latest research no longer qualifies the token, say so on the post."""
        cand = self.db.fetchone("SELECT last_action, last_score, reasons FROM candidates WHERE mint=?", (mint,))
        if not cand or cand.get("last_action") not in ("REJECT", "INSUFFICIENT_DATA"):
            return None
        reason = ""
        try:
            reasons = json.loads(cand.get("reasons") or "[]")
            if reasons:
                reason = f" Main reason: {str(reasons[0])[:200]}"
        except (TypeError, ValueError):
            pass
        score = cand.get("last_score")
        score_txt = f" (score {score:.0f})" if isinstance(score, (int, float)) else ""
        return (f"Northstar's latest re-check{score_txt} returned **{cand['last_action']}** - this token no "
                f"longer meets the early-watch criteria.{reason}")

    def _embed(self, row: dict, *, stale: bool, migrated: bool, closed: bool) -> dict:
        try:
            report = json.loads(row.get("report") or "{}")
        except (TypeError, ValueError):
            report = {}
        row = dict(row)
        tracking = (report.get("early_tracking") or {})
        row.setdefault("baseline_mode", tracking.get("baseline_mode"))
        row.setdefault("entry_sol_usd", tracking.get("entry_sol_usd"))
        row["current_sol_usd"] = self._sol_usd
        embed = AlertService._build_early_watch_embed(report, row, self.s)
        read_at = int(row.get("updated_at") or time.time())
        if migrated:
            status = f"🏁 Migrated <t:{read_at}:R> — tracking ended (last curve reading shown)"
        elif closed:
            status = f"⏹️ Tracking ended after {self.s.max_tracking_hours:g}h (last reading <t:{read_at}:R>)"
        elif stale:
            status = f"⚠️ Latest on-chain read failed — values from <t:{read_at}:R>, may be out of date"
        else:
            status = f"🔄 Updated <t:{read_at}:R> (on-chain, verified)"
        lines = [status]
        notice = self._research_notice(row["mint"])
        if notice:
            lines.append("⚠️ " + notice)
        desc = (embed.get("description") or "") + "\n" + "\n".join(lines)
        embed["description"] = desc if len(desc) <= 4000 else desc[:3999] + "…"
        return embed

    # ------------------------------------------------------------------ main loop
    async def run_once(self, now: Optional[float] = None) -> dict:
        now = now or time.time()
        counts = {"open": 0, "read": 0, "edited": 0, "failed_reads": 0, "migrated": 0, "closed": 0}
        placeholders = ",".join("?" for _ in OPEN_STATUSES)
        rows = self.db.fetchall(
            f"SELECT * FROM early_signals WHERE (status IN ({placeholders}) "
            "AND message_id IS NOT NULL AND channel_id IS NOT NULL) OR status = 'SILENT'", OPEN_STATUSES)
        counts["open"] = len(rows)
        if not rows or self.rpc is None:
            return counts
        if self.sol_price is not None:
            try:
                self._sol_usd = await self.sol_price.get()
            except Exception:  # noqa: BLE001 - display only; posts fall back to SOL
                self._sol_usd = None

        live, keyed = [], {}
        for row in rows:
            if now - float(row["created_at"] or now) > self.s.max_tracking_hours * 3600:
                await self._finish(row, "CLOSED", counts)
                continue
            key = self._curve_key(row["mint"])
            if not key:
                log.warning("early-watch %s: no bonding-curve address stored; cannot track", row["mint"])
                continue
            live.append(row)
            keyed[row["mint"]] = key
        if not live:
            return counts

        mints = [r["mint"] for r in live]
        try:
            accounts = await self.rpc.get_multiple_accounts([keyed[m] for m in mints])
            batch_ok = len(accounts) == len(mints)
        except ProviderError as exc:
            log.warning("early-watch curve refresh failed: %s", exc)
            accounts, batch_ok = [], False

        for i, row in enumerate(live):
            mint = row["mint"]
            state = Researcher._parse_pump_curve_account(accounts[i]) if batch_ok else None
            cap = state.get("market_cap_sol") if state else None
            if cap is None or not (cap > 0):
                self._failures[mint] = self._failures.get(mint, 0) + 1
                counts["failed_reads"] += 1
                if self._failures[mint] == STALE_FAILURES:
                    await self._edit(row, counts, stale=True)
                continue
            self._failures[mint] = 0
            counts["read"] += 1
            try:
                report = json.loads(row.get("report") or "{}")
            except (TypeError, ValueError):
                report = {}
            updated, _ = self.db.create_or_update_early_signal(mint, cap, float(row.get("score") or 0), report)
            if await self._trade_plan(updated, now):
                updated = self.db.fetchone("SELECT * FROM early_signals WHERE mint=?", (mint,)) or updated
                self._last_edit_cap.pop(mint, None)  # force an edit so the plan status shows
            if state.get("complete"):
                await self._finish(updated, "MIGRATED", counts)
                continue
            last = self._last_edit_cap.get(mint)
            flags = (self._research_notice(mint),)
            moved = last is None or (last > 0 and abs(cap - last) / last >= MIN_CHANGE_FOR_EDIT)
            if moved or flags != self._last_edit_flags.get(mint):
                await self._edit(updated, counts, stale=False)
        return counts

    async def _trade_plan(self, row: dict, now: float) -> bool:
        """Post TP / stop / time-stop messages for an open early watch. True if anything was posted."""
        if not self.s.trade_plan_enabled or row.get("status") not in OPEN_STATUSES or not row.get("channel_id"):
            return False
        try:
            report = json.loads(row.get("report") or "{}")
        except (TypeError, ValueError):
            return False
        tracking = {**(report.get("early_tracking") or {}), **row}
        p = AlertService.early_plan(tracking, self.s)
        entry, cur = row.get("entry_market_cap_sol"), row.get("current_market_cap_sol")
        if p is None or not entry or not cur:
            return False
        sent = set((report.get("early_tracking") or {}).get("plan_sent") or [])
        fmt = AlertService.early_fmt(report.get("early_tracking") or {})
        events = TP.evaluate(p, cur / entry, float(row.get("peak_multiple") or 1),
                             now - float(row.get("created_at") or now), sent, fmt)
        if not events:
            return False
        ident = report.get("identity") or {}
        posted = []
        for kind, text in events:
            posted.append(kind)
            if kind == "ENTRY_CLOSED":
                continue
            try:
                await self.pub.publish(plan_event_embed(
                    symbol=ident.get("symbol") or "?", name=ident.get("name") or "", mint=row["mint"],
                    title=TP.EVENTS[kind], text=text, is_exit=kind in TP.EXIT_KINDS, early=True,
                    image_url=ident.get("image_url")), int(row["channel_id"]))
            except PublishError as exc:
                log.warning("early-watch plan message failed for %s (%s): %s", row["mint"], kind, exc)
                if exc.retryable:
                    posted.pop()  # try again next cycle
        if posted:
            self.db.add_early_plan_events(row["mint"], posted)
            log.info("early-watch plan events for %s: %s", row["mint"], posted)
        return bool(posted)

    async def _finish(self, row: dict, status: str, counts: dict) -> None:
        await self._edit(row, counts, stale=False, migrated=(status == "MIGRATED"), closed=(status == "CLOSED"))
        self.db.update_early_signal_status(row["mint"], status)
        counts["migrated" if status == "MIGRATED" else "closed"] += 1
        log.info("early-watch tracking ended for %s (%s)", row["mint"], status)

    async def _edit(self, row: dict, counts: dict, *, stale: bool, migrated: bool = False,
                    closed: bool = False) -> None:
        mint = row["mint"]
        if row.get("status") == "SILENT" or not row.get("message_id") or not row.get("channel_id"):
            return  # tracked for performance only; there is no message to edit
        try:
            embed = self._embed(row, stale=stale, migrated=migrated, closed=closed)
            await self.pub.edit(int(row["channel_id"]), int(row["message_id"]), embed)
        except PublishError as exc:
            log.warning("early-watch embed edit failed for %s (message %s): %s", mint, row.get("message_id"), exc)
            if not exc.retryable:
                # message deleted / no permission: stop retrying every cycle, keep the record
                self.db.update_early_signal_status(mint, "UNTRACKED")
            return
        self._last_edit_cap[mint] = row.get("current_market_cap_sol")
        self._last_edit_flags[mint] = (self._research_notice(mint),)
        counts["edited"] += 1
        log.info("early-watch performance updated for %s: current=%s SOL peak=%.2fx low=%.2fx", mint,
                 row.get("current_market_cap_sol"), float(row.get("peak_multiple") or 1),
                 float(row.get("min_multiple") or 1))
