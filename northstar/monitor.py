"""Post-callout monitoring: performance, milestones, warnings, embed edits.

Price for a callout is always read from the SAME pool that was selected at callout time.
Missing data never becomes zero: the last valid observation is kept and the status changes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from .analysis import wallets as W
from .config import Settings
from .db import Database
from . import trade_plan as TP
from .discord_bot.embeds import milestone_embed, money, plan_event_embed, updated_callout, warning_embed
from .discord_bot.publisher import PublishError, Publisher
from .net import ProviderError

log = logging.getLogger("northstar.monitor")
STALE_AFTER = 1800


class Monitor:
    def __init__(self, s: Settings, db: Database, publisher: Publisher, dex=None, rpc=None, cg=None):
        self.s, self.db, self.pub, self.dex, self.rpc, self.cg = s, db, publisher, dex, rpc, cg
        self._last_creator_check: dict[int, float] = {}
        self._last_holder_check: dict[int, float] = {}
        self._slow_tasks: dict[int, asyncio.Task] = {}
        self._slow_warnings: dict[int, list] = {}
        self._slow_top10: dict[int, float] = {}
        self._candle_peaks: dict[int, tuple[float, float]] = {}   # alert id -> (multiple, unix time)
        self._candle_tasks: dict[int, asyncio.Task] = {}
        self._last_candle_check: dict[int, float] = {}

    async def run_once(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        alerts = self.db.open_alerts()
        if not alerts or self.dex is None:
            return
        try:
            pairs = await self.dex.tokens_batch(list({a["mint"] for a in alerts}))
            fetch_ok = True
        except ProviderError as e:
            log.warning("monitor market refresh failed: %s", e)
            pairs, fetch_ok = [], False
        by_pool = {p.pair_address: p for p in pairs}
        open_ids = {a["id"] for a in alerts}
        for d in (self._last_creator_check, self._last_holder_check, self._last_candle_check, self._slow_warnings,
                  self._slow_top10, self._candle_peaks):
            for k in [k for k in d if k not in open_ids]:
                d.pop(k, None)
        started = time.monotonic()
        priced = 0
        for a in alerts:
            pair = by_pool.get(a["pool_address"])
            if pair is not None and pair.price_usd:
                priced += 1
            elif fetch_ok:
                log.warning("monitor: no price for alert %s ($%s) on its pool %s", a["id"], a.get("symbol"),
                            a["pool_address"])
            try:
                await self._update(a, pair, fetch_ok, now)
            except Exception:  # one bad alert must not stop the others
                log.exception("monitor update failed for alert %s", a["id"])
        log.info("monitor: %d open call(s), %d priced, cycle %.1fs", len(alerts), priced, time.monotonic() - started)

    async def _update(self, a: dict, pair, fetch_ok: bool, now: float) -> None:
        s = self.s
        upd: dict = {"last_update_at": now}
        warnings = json.loads(a["warnings"] or "[]")
        hit = json.loads(a["milestones_hit"] or "[]")
        new_warnings: list[tuple[str, str]] = []
        mult = None
        if pair and pair.price_usd:
            price, liq = pair.price_usd, pair.liquidity_usd
            upd.update(current_price=price, current_mcap=pair.market_cap, current_liq=liq,
                       price_status="OBSERVED", last_valid_price_at=now)
            # Track the user's requested market-cap multiple whenever both observations exist.
            # Fall back to price multiple only when a market-cap value is unavailable.
            if a.get("entry_mcap") and a["entry_mcap"] > 0:
                # Do not substitute price for market cap when the requested metric is missing;
                # that would mix measurement bases and could trigger a false market-cap milestone.
                mult = pair.market_cap / a["entry_mcap"] if pair.market_cap is not None else None
            else:
                mult = price / a["entry_price"] if a["entry_price"] else None
            if mult is not None and mult > 0:
                upd["max_multiple"] = max(a["max_multiple"] or 1, mult)
                upd["min_multiple"] = min(a["min_multiple"] or 1, mult)
                upd["max_drawdown_pct"] = round((1 - upd["min_multiple"]) * 100, 2)
                self._raise_peak(a, upd, mult, now)
            self.db.add_price_point(a["id"], price, liq, pair.market_cap, "OBSERVED")
            if a["entry_liq"] and liq is not None and liq <= a["entry_liq"] * (1 - s.liquidity_collapse_pct / 100) \
                    and "LIQUIDITY_COLLAPSE" not in warnings:
                new_warnings.append(("LIQUIDITY_COLLAPSE",
                                     f"Liquidity fell from ${a['entry_liq']:,.0f} at callout to ${liq:,.0f} "
                                     f"(-{(1 - liq / a['entry_liq']) * 100:.0f}%). Exits may be severely impaired."))
            if mult is not None and mult <= 1 - s.severe_drawdown_pct / 100 and "SEVERE_DRAWDOWN" not in warnings:
                new_warnings.append(("SEVERE_DRAWDOWN", f"Price is {(mult - 1) * 100:.0f}% below the callout entry."))
        else:
            # keep the last valid observation; never write zero
            status = "UNAVAILABLE" if fetch_ok else "FETCH_FAILED"
            upd["price_status"] = status
            self.db.add_price_point(a["id"], None, None, None, status)
            if a["last_valid_price_at"] and now - a["last_valid_price_at"] > STALE_AFTER and a["state"] != "STALE":
                upd["state"] = "STALE"
                self.db.add_alert_event(a["id"], "stale", {"since": a["last_valid_price_at"]})

        # 1-minute candle highs catch spikes that happen between two 30-second checks.
        candle = self._candle_peaks.pop(a["id"], None)
        if candle and a.get("entry_mcap"):
            cmult, cat = candle
            seen = max(upd.get("max_multiple") or 0, a["max_multiple"] or 0, mult or 0, 1.0)
            if cmult > 3 * seen:
                # a candle 3x above anything observed is almost certainly wrong data (e.g. quote/base
                # mix-up); never let it fire milestones or exits
                log.warning("ignoring implausible candle peak %.2fx for alert %s (observed max %.2fx)",
                            cmult, a["id"], seen)
            elif cmult > (upd.get("max_multiple") or a["max_multiple"] or 1):
                upd["max_multiple"] = cmult
                self._raise_peak(a, upd, cmult, cat)

        # Slow on-chain checks (creator selling every 10 min, holder drift every 30 min) run in the
        # background. They can take minutes when RPC is busy, and used to block this loop, so prices,
        # milestones, take-profit/exit messages and embed edits were delayed for every open call.
        self._schedule_slow_checks(a, now)
        self._schedule_candle_check(a, now)
        for kind, detail in self._slow_warnings.pop(a["id"], []):
            if kind not in warnings:
                new_warnings.append((kind, detail))
        if a["id"] in self._slow_top10:
            upd["current_top10_adj_pct"] = self._slow_top10.pop(a["id"])

        merged = {**a, **upd}
        for m in s.milestone_list:
            if (merged.get("max_multiple") or 0) >= m and m not in hit:
                hit.append(m)
                self.db.add_alert_event(a["id"], "milestone", {"multiple": m})
                milestone = milestone_embed(merged, m)
                # VIP gets all configured milestones; free gets only the selected public milestones.
                await self._send(milestone, a["id"])
                # v4.0.0: once the delayed free copy is out, every new milestone goes to free as well
                # (the ones reached before it went out with the copy, all at once)
                if self.s.discord_free_channel_id and a.get("call_type") != "RUG_SCALP":
                    fp = self.db.fetchone("SELECT status FROM free_channel_posts WHERE alert_id = ?", (a["id"],))
                    if fp and fp["status"] == "SENT":
                        await self._send(milestone, a["id"], channel=self.s.discord_free_channel_id)
        upd["milestones_hit"] = hit
        for kind, detail in new_warnings:
            warnings.append(kind)
            upd["state"] = "WARNING"
            merged["state"] = "WARNING"
            self.db.add_alert_event(a["id"], "warning", {"kind": kind, "detail": detail})
            # v4.0.0: drawdowns and danger warnings are recorded, never posted to VIP or free; they go
            # out only to a separate warning channel when one is set (NORTHSTAR_DISCORD_WARNING_CHANNEL_ID)
            if getattr(s, "post_drawdowns", False) or s.discord_warning_channel_id:
                await self._send(warning_embed(merged, kind.replace("_", " "), detail,
                                               critical=kind in ("LIQUIDITY_COLLAPSE", "CREATOR_DUMP")),
                                 a["id"], channel=s.discord_warning_channel_id)
        await self._trade_plan(a, merged, mult, warnings, new_warnings, now)
        upd["warnings"] = warnings
        rug_x = float(getattr(s, "callout_rug_close_x", 0.3) or 0)
        rugged = rug_x > 0 and mult is not None and 0 < mult <= rug_x
        from . import brain as B
        if not rugged and mult and a.get("entry_mcap") and B.is_dead(mult * a["entry_mcap"], a["entry_mcap"]):
            rugged = True                     # v4.3.0: back at $5K (dead_mcap_usd) or lower = dead
        if rugged and "RUGGED" not in warnings:
            # v2.5.10: a call that trades at or under 0.3x of its call (a verified price read) is a
            # rug: say so once and stop tracking it, instead of following it for 72 hours
            detail = (f"Now {mult:.2f}x of the call ({(mult - 1) * 100:.0f}%). Marked rugged; "
                      "tracking ended.")
            warnings.append("RUGGED")
            upd["warnings"] = warnings
            self.db.add_alert_event(a["id"], "warning", {"kind": "RUGGED", "detail": detail})
            if getattr(s, "post_drawdowns", False) or s.discord_warning_channel_id:
                await self._send(warning_embed(merged, "RUGGED", detail, critical=True), a["id"],
                                 channel=s.discord_warning_channel_id)
        if rugged or now - a["created_at"] > s.max_tracking_hours * 3600:
            upd["state"] = "CLOSED"
            upd["closed_at"] = now
            merged["state"] = "CLOSED"
            self.db.set_state(a["mint"], "CLOSED")
        elif upd.get("state") == "WARNING":
            self.db.set_state(a["mint"], "WARNING")
        self.db.update_alert(a["id"], **upd)
        if a["message_id"] and (now - (a["last_embed_edit_at"] or 0) >= s.embed_edit_min_seconds
                                or upd.get("state") in ("CLOSED", "WARNING", "STALE")):
            await self._edit(a["id"])

    @staticmethod
    def _raise_peak(a: dict, upd: dict, mult: float, at: float) -> None:
        best = max(a.get("max_multiple") or 1, (a.get("peak_mcap") or 0) / a["entry_mcap"] if a.get("entry_mcap") else 1)
        if mult > best + 1e-9 or (a.get("peak_mcap") is None and mult >= best):
            upd["peak_at"] = at
            if a.get("entry_mcap"):
                upd["peak_mcap"] = a["entry_mcap"] * mult

    def _schedule_candle_check(self, a: dict, now: float) -> None:
        if self.cg is None or not a.get("pool_address") or not a.get("entry_price") or a["id"] in self._candle_tasks:
            return
        if now - self._last_candle_check.get(a["id"], 0) < self.s.coingecko_peak_check_seconds:
            return
        self._last_candle_check[a["id"]] = now
        task = asyncio.create_task(self._candle_check(dict(a)))
        self._candle_tasks[a["id"]] = task
        task.add_done_callback(lambda _t, i=a["id"]: self._candle_tasks.pop(i, None))

    async def _candle_check(self, a: dict) -> None:
        from .providers.coingecko import peak_after
        since = a.get("sent_at") or a["created_at"]
        urgent = time.time() - since < 3600      # first hour: your CoinGecko key; afterwards the free API
        try:
            candles = await asyncio.wait_for(
                self.cg.candles(a["pool_address"], since, urgent=urgent, token=a["mint"]), timeout=60)
        except Exception:  # noqa: BLE001 - optional data source
            log.debug("candle check failed for alert %s", a["id"], exc_info=True)
            return
        best = peak_after(candles, a["entry_price"], since)
        if best:
            self._candle_peaks[a["id"]] = best

    def _schedule_slow_checks(self, a: dict, now: float) -> None:
        if self.rpc is None or a["id"] in self._slow_tasks:
            return
        creator_due = now - self._last_creator_check.get(a["id"], 0) >= 600
        holders_due = now - self._last_holder_check.get(a["id"], 0) >= 1800
        if not (creator_due or holders_due):
            return
        if creator_due:
            self._last_creator_check[a["id"]] = now
        if holders_due:
            self._last_holder_check[a["id"]] = now
        task = asyncio.create_task(self._slow_checks(dict(a), creator_due, holders_due))
        self._slow_tasks[a["id"]] = task
        task.add_done_callback(lambda _t, i=a["id"]: self._slow_tasks.pop(i, None))

    async def _slow_checks(self, a: dict, creator: bool, holders: bool) -> None:
        found: list[tuple[str, str]] = []
        try:
            if creator:
                dump = await asyncio.wait_for(self._creator_sold_since(a), timeout=120)
                if dump:
                    found.append(("CREATOR_DUMP", dump))
            if holders:
                top10 = await asyncio.wait_for(self._top10_adjusted(a), timeout=120)
                if top10 is not None:
                    self._slow_top10[a["id"]] = top10
                    base = a["entry_top10_adj_pct"]
                    if base is not None and top10 - base >= 15:
                        found.append(("CONCENTRATION_RISING",
                                      f"Adjusted top-10 holder share rose from {base:.1f}% to {top10:.1f}%."))
        except asyncio.TimeoutError:
            log.info("on-chain checks for alert %s timed out; retried next interval", a["id"])
        except Exception:  # noqa: BLE001 - a failed background check must never affect the monitor
            log.exception("on-chain checks failed for alert %s", a["id"])
        if found:
            self._slow_warnings.setdefault(a["id"], []).extend(found)

    async def _trade_plan(self, a: dict, merged: dict, mult, warnings: list, new_warnings: list, now: float) -> None:
        """Post entry/exit plan events (TP1, TP2, trailing, stop, time stop, danger) as new messages."""
        if not self.s.trade_plan_enabled or not a.get("entry_mcap"):
            return
        try:
            values = json.loads(a.get("plan_json") or "null")    # levels frozen when the call was posted
        except (TypeError, ValueError):
            values = None
        p = TP.build_plan(a["entry_mcap"], self.s, values=values)
        if p is None:
            return
        sent = {w[5:] for w in warnings if isinstance(w, str) and w.startswith("PLAN:")}
        events = []
        danger = [d for k, d in new_warnings if k in ("LIQUIDITY_COLLAPSE", "CREATOR_DUMP")]
        if danger and not sent & TP.EXIT_KINDS and not (TP.sells_everything(p) and "TP2" in sent):
            events.append(TP.danger_event(danger[0]))
        else:
            age = now - (a.get("sent_at") or a["created_at"])
            events = TP.evaluate(p, mult, float(merged.get("max_multiple") or 1), age, sent, money)
        for kind, text in events:
            warnings.append("PLAN:" + kind)
            self.db.add_alert_event(a["id"], "plan", {"kind": kind, "detail": text, "multiple": mult})
            if kind == "ENTRY_CLOSED":
                continue  # shown on the edited callout; not worth a ping
            if kind in TP.EXIT_KINDS and not getattr(self.s, "post_drawdowns", False):
                continue  # v4.0.0: only good news is posted; exits are recorded (paper, learning)
            await self._send(plan_event_embed(symbol=a.get("symbol") or "?", name=a.get("name") or "",
                                              mint=a["mint"],
                                              title=("☠️ SCALP · " if a.get("call_type") == "RUG_SCALP" else "")
                                              + TP.EVENTS[kind], text=text,
                                              is_exit=kind in TP.EXIT_KINDS), a["id"])

    async def _top10_adjusted(self, a: dict) -> Optional[float]:
        from .analysis.holders import analyze_holders, build_holders_from_rpc
        from .models import DataStatus
        try:
            info = await self.rpc.get_token_supply(a["mint"])
            supply = float(info["uiAmountString"]) if info else None
            if not supply:
                return None
            largest = await self.rpc.get_token_largest_accounts(a["mint"])
            owners = await self.rpc.get_multiple_accounts([x["address"] for x in largest if x.get("address")])
        except (ProviderError, KeyError, TypeError, ValueError):
            return None
        ha = analyze_holders(build_holders_from_rpc(largest, owners, supply), DataStatus.VERIFIED, "solana-rpc",
                             self.s, pair_address=a["pool_address"], creator=None, rc=None, supply_ui=supply)
        return ha.top10_adj

    async def _creator_sold_since(self, a: dict) -> Optional[str]:
        snap = self.db.fetchone("SELECT report FROM research_snapshots WHERE id=?", (a["snapshot_id"],))
        if not snap:
            return None
        rep = json.loads(snap["report"])
        creator = (rep.get("creator") or {}).get("creator")
        if not creator or not a["pool_address"]:
            return None
        try:
            trades, _ = await W.sample_trades(self.rpc, a["pool_address"], a["mint"], {a["pool_address"]}, 40)
        except ProviderError:
            return None
        sold = [t for t in trades if t.wallet == creator and t.side == "sell"
                and (t.block_time or 0) >= (a["sent_at"] or a["created_at"])]
        if not sold:
            return None
        amt = sum(t.token_amount for t in sold)
        evidence = ("VERIFIED on-chain swap events" if all(t.verification in W.VERIFIED_LABELS for t in sold)
                    else "on-chain transaction sample; swap not decoded, treat as unconfirmed")
        return (f"Creator wallet {creator[:4]}…{creator[-4:]} sold {amt:,.0f} tokens in {len(sold)} "
                f"transaction(s) after the callout ({evidence}).")

    async def _send(self, embed: dict, alert_id: int, channel: Optional[int] = None) -> None:
        try:
            await self.pub.publish(embed, channel)
        except PublishError as e:
            log.warning("failed to send follow-up for alert %s: %s", alert_id, e)
            self.db.add_alert_event(alert_id, "followup_error", {"error": str(e)})

    async def _edit(self, alert_id: int) -> None:
        a = self.db.get_alert(alert_id)
        ev = self.db.fetchone("SELECT detail FROM alert_events WHERE alert_id=? AND kind='published' "
                              "ORDER BY ts LIMIT 1", (alert_id,))
        if not ev:
            return
        if self.db.fetchone("SELECT 1 FROM alert_events WHERE alert_id=? AND kind='message_missing' LIMIT 1",
                            (alert_id,)):
            return  # the Discord message was deleted; stop trying to edit it
        original = json.loads(ev["detail"]).get("embed") or {}
        try:
            await self.pub.edit(a["channel_id"], a["message_id"], updated_callout(original, a))
            self.db.update_alert(alert_id, last_embed_edit_at=time.time())
        except PublishError as e:
            if "not found" in str(e):
                log.info("alert %s: Discord message was deleted; no more embed edits for it", alert_id)
                self.db.add_alert_event(alert_id, "message_missing", {"error": str(e)})
                return
            log.warning("embed edit failed for alert %s: %s", alert_id, e)
