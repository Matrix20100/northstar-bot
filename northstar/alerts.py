"""Alert publishing with duplicate protection (outbox pattern) and restart recovery."""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from .config import Settings
from .db import Database
from .discord_bot.embeds import (
    callout_embed,
    early_watch_embed,
    money,
    updated_callout,
    usd_or_sol,
)
from .discord_bot.publisher import PublishError, Publisher
from .models import Action
from .net import ProviderError
from .research import ResearchOutcome
from . import trade_plan as TP

log = logging.getLogger("northstar.alerts")

MAX_ATTEMPTS = 3



def v3_call_embed(report: dict, plan: Optional[list] = None) -> dict:
    """v3.0.0: every VIP call is the compact post (key numbers, image, More info button)."""
    from .discord_bot.embeds import compact_call_embed

    def val(x):
        return x.get("value") if isinstance(x, dict) else x

    ct = report.get("call_type") or "STANDARD"
    kind = {"RUG_SCALP": "scalp", "MANUAL": "manual"}.get(ct, "standard")
    entry = val((report.get("market") or {}).get("market_cap"))
    now = time.time()
    call = {"entry_mcap": entry, "current_mcap": entry, "peak_mcap": entry, "sent_at": now, "phase": "POOL",
            "status_line": " · ".join(str(x) for x in (plan or [])[:3])[:300] or None}
    return compact_call_embed(report, call, kind=kind)


def wants_free_copy(call_type: str, report: dict) -> bool:
    """v3.0.0: no automatic copies. The free channel only gets proof posts once a call reaches
    `free_proof_multiple` (monitor.py / early_calls.py). The one exception is a manual call whose
    caller ticked "also post to the free channel" for that call."""
    return call_type == "MANUAL" and (report.get("manual") or {}).get("free_copy") is True


def _legacy_wants_free_copy(call_type: str, report: dict) -> bool:
    """Standard calls get a delayed free-channel copy; scalps never do; manual calls if asked."""
    if call_type == "STANDARD":
        return True
    if call_type == "MANUAL":
        return bool((report.get("manual") or {}).get("free_copy", True))
    return False

class AlertService:
    def __init__(
        self,
        s: Settings,
        db: Database,
        publisher: Publisher,
        dex=None,
    ):
        self.s = s
        self.db = db
        self.pub = publisher
        self.dex = dex
        self._fresh: dict = {}   # mint -> pool re-read at post time (research can take minutes)
        self.last_hold: Optional[str] = None   # why the last publish() did not post, in plain words

    async def preflight(self, out: ResearchOutcome) -> Optional[str]:
        """Re-check the selected pool immediately before posting."""
        if self.dex is None or out.pair is None:
            return "no market data for pre-flight check"

        try:
            pairs = await self.dex.token_pairs(out.mint)
        except ProviderError as exc:
            return f"pre-flight market refresh failed: {exc}"

        current = next(
            (
                pair
                for pair in pairs
                if pair.pair_address == out.pair.pair_address
            ),
            None,
        )

        if current is None or not current.price_usd:
            return "selected pool no longer returns a price"
        self._fresh[out.mint] = current

        original_liquidity = out.pair.liquidity_usd
        current_liquidity = current.liquidity_usd

        if (
            original_liquidity
            and current_liquidity is not None
            and current_liquidity
            < original_liquidity
            * (1 - self.s.liquidity_drop_reject_pct / 100)
        ):
            return (
                f"liquidity fell from ${original_liquidity:,.0f} "
                f"to ${current_liquidity:,.0f} during research"
            )

        return None

    async def publish(self, out: ResearchOutcome, manual: bool = False) -> Optional[int]:
        """Publish a qualified AUTO_POST outcome, a RUG SCALP, or a MANUAL call from the dashboard.

        A manual call skips the decision gate (the owner made the decision) but nothing else: the
        pre-flight market check, the cooldown, the VIP-first ordering and the tracking all apply.
        When it returns None, `last_hold` says why.
        """
        call_type = out.report.get("call_type") or "STANDARD"
        assert manual or out.action == Action.AUTO_POST or call_type == "RUG_SCALP"
        profile = "scalp" if call_type == "RUG_SCALP" else "standard"
        self.last_hold = None

        hold_reason = await self.preflight(out)

        if hold_reason:
            self.last_hold = hold_reason
            log.info(
                "holding %s: %s",
                out.mint,
                hold_reason,
            )

            self.db.set_state(
                out.mint,
                "WATCHLIST",
                next_research_at=(
                    time.time() + self.s.watchlist_recheck_seconds
                ),
                reasons=[f"pre-flight hold: {hold_reason}"],
            )

            return None

        pair = self._fresh.pop(out.mint, None) or out.pair
        if pair is not out.pair:
            # Research can take minutes; the call must show the market at the moment it is posted,
            # or "Called at" (and every plan level) would be stale.
            market = out.report.setdefault("market", {})
            for key, value in (("price_usd", pair.price_usd), ("market_cap", pair.market_cap),
                               ("liquidity_usd", pair.liquidity_usd), ("fdv", pair.fdv)):
                if value is not None:
                    market[key] = {"value": value, "status": "OBSERVED", "source": "dexscreener (at post time)"}
            moved = (pair.market_cap / out.pair.market_cap - 1) * 100 if (out.pair.market_cap and pair.market_cap) else None
            if moved is not None:
                market["moved_during_research_pct"] = round(moved, 1)
                log.info("%s moved %+.1f%% during research; call uses the post-time market cap", out.mint, moved)
                # dumping while we checked, or (for a scalp) already ran away: don't post it now
                limit_down = -12.0 if call_type == "RUG_SCALP" else -20.0
                limit_up = 40.0 if call_type == "RUG_SCALP" else None
                if moved <= limit_down or (limit_up is not None and moved >= limit_up):
                    self.last_hold = f"price moved {moved:+.0f}% since the research — research it again"
                    log.info("holding %s: moved %+.1f%% during research", out.mint, moved)
                    self.db.set_state(out.mint, "WATCHLIST",
                                      next_research_at=time.time() + self.s.watchlist_recheck_seconds,
                                      reasons=[f"pre-flight hold: price moved {moved:+.0f}% during research"])
                    return None
        now = time.time()
        plan_vals = TP.plan_values(self.s, profile) if getattr(self.s, "trade_plan_enabled", False) else None
        if out.snapshot_id and (call_type != "STANDARD" or pair is not out.pair):
            # keep the saved report identical to what was posted (used if a send must be resumed)
            self.db.execute("UPDATE research_snapshots SET report = ? WHERE id = ?",
                            (json.dumps(out.report, default=str), out.snapshot_id))

        alert_id = self.db.create_alert_intent(
            out.mint,
            now - self.s.alert_cooldown_hours * 3600,
            call_type=call_type,
            plan_json=json.dumps(plan_vals) if plan_vals else None,
            snapshot_id=out.snapshot_id,
            pool_address=pair.pair_address,
            symbol=pair.base_symbol,
            name=pair.base_name,
            entry_price=pair.price_usd,
            entry_mcap=pair.market_cap,
            entry_fdv=pair.fdv,
            entry_liq=pair.liquidity_usd,
            score=out.score,
            risk=out.risk.value,
            confidence=out.confidence.value,
            current_price=pair.price_usd,
            current_mcap=pair.market_cap,
            current_liq=pair.liquidity_usd,
            price_status="OBSERVED",
            last_valid_price_at=now,
            max_multiple=1.0,
            min_multiple=1.0,
            max_drawdown_pct=0.0,
            entry_top10_adj_pct=(
                out.report.get("holders") or {}
            ).get("top10_adj"),
        )

        if alert_id is None:
            self.last_hold = (f"already called within the last {self.s.alert_cooldown_hours:g} hours "
                              "(cooldown)")
            log.info(
                "duplicate alert suppressed for %s (cooldown)",
                out.mint,
            )
            return None

        self.db.set_state(out.mint, "QUALIFIED")

        delivered = await self._send(
            alert_id,
            v3_call_embed(out.report, self._callout_plan(out.report, plan_vals)),
        )

        # The VIP channel receives the qualified call first.
        # The free-channel copy is scheduled only after successful delivery.
        # Scalps are minutes-long trades on rugs: a delayed public copy would be useless and dangerous.
        alert = self.db.get_alert(alert_id)
        free_channel = self.s.discord_free_channel_id if wants_free_copy(call_type, out.report) else None

        if (
            free_channel
            and alert
            and alert["status"] in ("SENT", "DRY_RUN")
        ):
            delay_minutes = max(
                0,
                self.s.free_callout_delay_minutes,
            )

            due_at = time.time() + delay_minutes * 60

            self.db.schedule_free_channel_post(
                alert_id,
                due_at,
                free_channel,
            )

            log.info(
                "scheduled free-channel copy for alert %s in %d minute(s)",
                alert_id,
                delay_minutes,
            )

        # None means "not delivered (yet)": a retryable failure is retried by resume_pending.
        if not delivered:
            self.last_hold = "Discord did not accept the message yet; it will be retried automatically"
        return alert_id if delivered else None

    async def publish_due_free_posts(self) -> None:
        """Publish delayed public copies after the configured VIP head start."""
        for row in self.db.due_free_channel_posts():
            alert_id = row["alert_id"]
            alert = self.db.get_alert(alert_id)

            if not alert:
                self.db.update_free_channel_post(
                    alert_id,
                    status="FAILED",
                    last_error="alert missing",
                )
                continue

            try:
                done = {w[5:] for w in json.loads(alert.get("warnings") or "[]") if str(w).startswith("PLAN:")}
            except (TypeError, ValueError):
                done = set()
            if done & TP.EXIT_KINDS:
                # The VIP plan already exited (stop / time stop / trailing / danger): a delayed public
                # copy would be a call nobody should act on.
                self.db.update_free_channel_post(alert_id, status="SKIPPED",
                                                 last_error="trade plan exited before the free-channel delay")
                log.info("free-channel copy skipped for alert %s: plan already exited (%s)", alert_id,
                         ", ".join(sorted(done & TP.EXIT_KINDS)))
                continue

            event = self.db.fetchone(
                """
                SELECT detail
                FROM alert_events
                WHERE alert_id = ? AND kind = 'published'
                ORDER BY ts
                LIMIT 1
                """,
                (alert_id,),
            )

            if not event:
                self.db.update_free_channel_post(
                    alert_id,
                    status="FAILED",
                    last_error="original embed missing",
                )
                continue

            try:
                event_data = json.loads(event["detail"])
                original_embed = event_data.get("embed") or {}

                embed = updated_callout(
                    original_embed,
                    alert,
                )

                channel_id, message_id = await self.pub.publish(
                    embed,
                    row["channel_id"],
                )

            except (
                PublishError,
                ValueError,
                TypeError,
            ) as exc:
                attempts = int(row["attempts"] or 0) + 1

                status = (
                    "FAILED"
                    if attempts >= MAX_ATTEMPTS
                    else "PENDING"
                )

                self.db.update_free_channel_post(
                    alert_id,
                    attempts=attempts,
                    status=status,
                    due_at=time.time() + min(300, 30 * attempts),
                    last_error=str(exc),
                )

                log.warning(
                    "free-channel copy failed for alert %s "
                    "(attempt %d): %s",
                    alert_id,
                    attempts,
                    exc,
                )
                continue

            self.db.update_free_channel_post(
                alert_id,
                status="SENT",
                attempts=int(row["attempts"] or 0) + 1,
                message_id=message_id,
                channel_id=channel_id,
                sent_at=time.time(),
                last_error=None,
            )

            log.info(
                "published delayed free-channel copy for alert %s "
                "(message %s)",
                alert_id,
                message_id,
            )

    async def publish_early_watch(
        self,
        out: ResearchOutcome,
    ) -> Optional[int]:
        """
        Publish or update a separate pre-migration early-watch message.

        This is not a standard safe callout. Existing sent messages are edited
        instead of creating another message for the same token.
        """
        curve = out.report.get("pre_migration") or {}

        if not curve.get("qualified_early_watch"):
            return None

        market_cap_sol = self._valid_market_cap(
            curve.get("market_cap_sol_current")
        )

        try:
            opportunity_score = float(
                curve.get("opportunity_score") or 0
            )
        except (TypeError, ValueError):
            opportunity_score = 0.0

        row, created = self.db.create_or_update_early_signal(
            out.mint,
            market_cap_sol,
            opportunity_score,
            out.report,
        )

        if not row:
            log.warning(
                "database did not return an early-signal record for %s",
                out.mint,
            )
            return None

        status = row.get("status")
        channel_id = row.get("channel_id")
        message_id = row.get("message_id")

        # Do not post a duplicate if a previous send may have succeeded
        # but its result was not recorded.
        if status == "UNCERTAIN":
            log.warning(
                "early-watch delivery uncertain for %s; "
                "suppressing duplicate post",
                out.mint,
            )
            return None

        # Existing message: update it with the latest available tracking data.
        if (
            status in ("SENT", "DRY_RUN")
            and channel_id
            and message_id
        ):
            embed = self._build_early_watch_embed(
                out.report,
                row,
                self.s,
            )

            try:
                await self.pub.edit(
                    int(channel_id),
                    int(message_id),
                    embed,
                )
            except PublishError as exc:
                log.warning(
                    "could not update early-watch message for %s "
                    "(message %s): %s",
                    out.mint,
                    message_id,
                    exc,
                )

                # Return the existing message ID. Do not create a duplicate
                # just because an edit failed.
                return int(message_id)

            log.info(
                "updated pre-migration early watch for %s "
                "(message %s)",
                out.mint,
                message_id,
            )

            return int(message_id)

        # Only new or explicitly pending records can be posted.
        if not created and status != "PENDING":
            log.info(
                "early-watch post suppressed for %s (status=%s)",
                out.mint,
                status,
            )
            return None

        target = self._early_watch_channel()
        if target == "SILENT":
            # v3.0.0: early watches are off by default (runner calls replace them). Nothing is
            # posted and nothing is tracked: the curve watch already measures these coins.
            self.db.update_early_signal_status(out.mint, "UNTRACKED")
            log.info("early watch for %s not posted (NORTHSTAR_EARLY_WATCH_CHANNEL=%s)",
                     out.mint, self.s.early_watch_channel)
            return None

        embed = self._build_early_watch_embed(
            out.report,
            row,
            self.s,
        )

        try:
            channel_id, message_id = await self.pub.publish(embed, target)

        except PublishError as exc:
            if exc.uncertain:
                next_status = "UNCERTAIN"
            elif exc.retryable:
                next_status = "PENDING"
            else:
                next_status = "FAILED"

            self.db.update_early_signal_status(
                out.mint,
                next_status,
            )

            log.warning(
                "early-watch publish failed for %s "
                "(status=%s): %s",
                out.mint,
                next_status,
                exc,
            )

            return None

        final_status = (
            "DRY_RUN"
            if self.s.dry_run
            else "SENT"
        )

        self.db.update_early_signal_status(
            out.mint,
            final_status,
            channel_id,
            message_id,
        )

        log.info(
            "published pre-migration early watch for %s "
            "(message %s)",
            out.mint,
            message_id,
        )

        return message_id

    @staticmethod
    def _valid_market_cap(value) -> Optional[float]:
        """Return a positive finite market cap, or None."""
        if value is None:
            return None

        try:
            number = float(value)
        except (TypeError, ValueError):
            return None

        if number <= 0:
            return None

        # Reject NaN and infinity without needing another dependency.
        if number != number or number in (float("inf"), float("-inf")):
            return None

        return number

    def _early_watch_channel(self):
        """Channel for a NEW early-watch post: None = the main (VIP) channel, "SILENT" = do not post.

        Early watches are experimental (pre-migration risk is always INCOMPLETE), so by default they
        go to the free channel only and never to VIP. With no free channel configured they are
        tracked silently instead of falling back to VIP.
        """
        mode = getattr(self.s, "early_watch_channel", "off")
        if mode == "vip":
            return None
        return "SILENT"               # v3.0.0: never the free channel (it only gets proven wins)

    def _callout_plan(self, report: dict, values: Optional[dict] = None) -> Optional[list[str]]:
        if not getattr(self.s, "trade_plan_enabled", False):
            return None
        mc = ((report.get("market") or {}).get("market_cap") or {}).get("value")
        profile = "scalp" if report.get("call_type") == "RUG_SCALP" else "standard"
        p = TP.build_plan(mc, self.s, profile, values)
        return TP.plan_lines(p, money) if p else None

    @staticmethod
    def _build_early_watch_embed(
        report: dict,
        tracking: dict,
        settings=None,
    ) -> dict:
        """Early-watch embed plus a compact, honest market-cap tracking block.

        The multiple is observed market-cap change, not an executable trading return.
        """
        embed = early_watch_embed(report)
        tracking = dict(tracking)
        tracking.setdefault("current_sol_usd", (report.get("pre_migration") or {}).get("sol_usd"))
        lines = AlertService.early_performance_lines(tracking)
        plan = AlertService.early_plan_lines(report, tracking, settings)
        if plan:
            lines += [""] + plan
        embed["description"] = (embed.get("description") or "") + "\n\n" + "\n".join(lines)
        return embed

    @staticmethod
    def early_plan(tracking: dict, settings) -> Optional["TP.Plan"]:
        if settings is None or not getattr(settings, "trade_plan_enabled", False):
            return None
        return TP.build_plan(AlertService._valid_market_cap(tracking.get("entry_market_cap_sol")), settings)

    @staticmethod
    def early_fmt(tracking: dict):
        rate = tracking.get("entry_sol_usd")
        return lambda mc_sol: usd_or_sol(mc_sol, rate)

    @staticmethod
    def early_plan_lines(report: dict, tracking: dict, settings) -> list[str]:
        p = AlertService.early_plan(tracking, settings)
        if p is None:
            return []
        sent = set(((report.get("early_tracking") or {}).get("plan_sent")) or [])
        try:  # the stored row is authoritative for what has already been posted
            stored = json.loads(tracking.get("report") or "{}") if isinstance(tracking.get("report"), str) else {}
            sent |= set((stored.get("early_tracking") or {}).get("plan_sent") or [])
        except (TypeError, ValueError):
            pass
        lines = TP.plan_lines(p, AlertService.early_fmt(tracking))
        done = [k for k in ("STOP", "TIME", "TRAIL", "DANGER") if k in sent]
        if done:
            lines.append(f"**Plan status:** {TP.EVENTS[done[0]]} posted — plan finished")
        elif "TP2" in sent or "TP1" in sent:
            lines.append(f"**Plan status:** {'TP2' if 'TP2' in sent else 'TP1'} hit — trailing the rest")
        return lines

    @staticmethod
    def early_performance_lines(tracking: dict) -> list[str]:
        entry_cap = AlertService._valid_market_cap(tracking.get("entry_market_cap_sol"))
        current_cap = AlertService._valid_market_cap(tracking.get("current_market_cap_sol"))

        def multiple(value) -> Optional[str]:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            if number != number or number in (float("inf"), float("-inf")):
                return None
            return f"{number:.2f}x"

        if entry_cap is None:
            return ["**Called At:** waiting for a valid market-cap reading"]
        entry_rate = tracking.get("entry_sol_usd")
        now_rate = tracking.get("current_sol_usd") or entry_rate
        lines = [f"**Called At:** {usd_or_sol(entry_cap, entry_rate)}"]
        if current_cap is not None:
            if entry_rate and now_rate:
                m = (current_cap * now_rate) / (entry_cap * entry_rate)   # change in dollar terms
            else:
                m = current_cap / entry_cap
            lines.append(f"**Now:** {usd_or_sol(current_cap, now_rate if entry_rate else None)} "
                         f"({m:.2f}x · {(m - 1) * 100:+.1f}%)")
        else:
            lines.append("**Now:** UNAVAILABLE")
        peak = multiple(tracking.get("peak_multiple"))
        if peak:
            lines.append(f"**Peak:** {peak}")
        if tracking.get("baseline_mode") == "delayed":
            lines.append("_Tracking began after the call; earlier movement not included._")
        return lines

    async def _send(
        self,
        alert_id: int,
        embed: dict,
    ) -> Optional[int]:
        """Send an alert while recording delivery state for recovery."""
        alert = self.db.get_alert(alert_id)

        if not alert:
            log.error(
                "cannot publish missing alert row %s",
                alert_id,
            )
            return None

        attempts = int(alert["publish_attempts"] or 0) + 1

        self.db.update_alert(
            alert_id,
            publish_attempts=attempts,
        )

        try:
            channel_id, message_id = await self.pub.publish(embed)

        except PublishError as exc:
            if exc.uncertain:
                status = "UNCERTAIN"
            elif exc.retryable and attempts < MAX_ATTEMPTS:
                status = "PENDING"
            else:
                status = "FAILED"

            self.db.update_alert(
                alert_id,
                status=status,
            )

            self.db.add_alert_event(
                alert_id,
                "publish_error",
                {
                    "error": str(exc),
                    "status": status,
                    "attempt": attempts,
                },
            )

            log.warning(
                "publish failed for alert %s (%s): %s",
                alert_id,
                status,
                exc,
            )

            if status == "FAILED":
                self.db.set_state(
                    alert["mint"],
                    "WATCHLIST",
                    next_research_at=(
                        time.time()
                        + self.s.watchlist_recheck_seconds
                    ),
                    reasons=[f"publish failed: {exc}"],
                )

            return None

        final_status = (
            "DRY_RUN"
            if self.s.dry_run
            else "SENT"
        )

        self.db.update_alert(
            alert_id,
            status=final_status,
            sent_at=time.time(),
            channel_id=channel_id,
            message_id=message_id,
            last_embed_edit_at=time.time(),
        )

        self.db.add_alert_event(
            alert_id,
            "published",
            {"embed": embed},
        )

        self.db.set_state(
            alert["mint"],
            "PUBLISHED",
            next_research_at=None,
        )

        log.info(
            "published alert %s for %s (message %s)",
            alert_id,
            alert["mint"],
            message_id,
        )

        return alert_id

    async def resume_pending(self) -> None:
        """Recover pending alerts after a restart without blindly double-posting."""
        for alert in self.db.pending_alerts():
            try:
                found_message_id = await self.pub.find_message_with(
                    f"mint:{alert['mint']}"
                )

            except PublishError as exc:
                log.warning(
                    "cannot verify pending alert %s: %s",
                    alert["id"],
                    exc,
                )
                continue

            if found_message_id:
                self.db.update_alert(
                    alert["id"],
                    status=(
                        "DRY_RUN"
                        if self.s.dry_run
                        else "SENT"
                    ),
                    message_id=found_message_id,
                    channel_id=(
                        alert["channel_id"]
                        or self.s.discord_channel_id
                    ),
                    sent_at=time.time(),
                )

                self.db.add_alert_event(
                    alert["id"],
                    "recovered",
                    {"message_id": found_message_id},
                )

                self.db.set_state(
                    alert["mint"],
                    "PUBLISHED",
                    next_research_at=None,
                )

                log.info(
                    "recovered existing message for alert %s",
                    alert["id"],
                )
                continue

            # A call resent late would show a stale entry and plan levels. Scalps are minutes-long trades,
            # so they are abandoned after 2 minutes; normal calls after 5. The coin is re-researched
            # right away and, if it still qualifies, posted fresh with current prices.
            max_age = 120 if (alert.get("call_type") or "STANDARD") == "RUG_SCALP" else 300
            too_old = (
                time.time() - alert["created_at"] > max_age
            )
            attempts_exhausted = (
                alert["publish_attempts"] >= MAX_ATTEMPTS
            )

            if (
                alert["status"] == "UNCERTAIN"
                or attempts_exhausted
                or too_old
            ):
                self.db.update_alert(
                    alert["id"],
                    status="FAILED",
                )

                self.db.add_alert_event(
                    alert["id"],
                    "abandoned",
                    {
                        "reason": (
                            "not delivered / too old to post"
                        )
                    },
                )

                self.db.set_state(
                    alert["mint"],
                    "WATCHLIST",
                    next_research_at=time.time(),
                )

                continue

            snapshot = self.db.fetchone(
                """
                SELECT report
                FROM research_snapshots
                WHERE id = ?
                """,
                (alert["snapshot_id"],),
            )

            if snapshot:
                try:
                    report = json.loads(snapshot["report"])
                except (ValueError, TypeError) as exc:
                    log.warning(
                        "invalid saved report for alert %s: %s",
                        alert["id"],
                        exc,
                    )
                    continue

                report["call_type"] = alert.get("call_type") or "STANDARD"
                try:
                    values = json.loads(alert.get("plan_json") or "null")
                except (TypeError, ValueError):
                    values = None
                delivered = await self._send(
                    alert["id"],
                    v3_call_embed(report, self._callout_plan(report, values)),
                )
                if delivered and wants_free_copy(report["call_type"], report) and self.s.discord_free_channel_id:
                    self.db.schedule_free_channel_post(
                        alert["id"], time.time() + max(0, self.s.free_callout_delay_minutes) * 60,
                        self.s.discord_free_channel_id)
