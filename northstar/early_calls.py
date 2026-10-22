"""Early narrative calls: bonding-curve coins at 30-50k that ride a LIVE narrative.

Pipeline
  1. CurveRadar     every ~2 min looks at recent pump.fun launches on DexScreener and picks coins
                    whose curve market cap enters the early band with real buying -> research now.
  2. EarlyCaller    after research, a coin is called only if ALL hold:
                      * still on the bonding curve, market cap inside the band (USD)
                      * recent verified flow: trades, unique buyers, buys vs sells, SOL bought (5 min)
                      * no failed security check, rug check not RUG LIKELY / HIGH RISK, no recycled socials
                      * Claude (ai_judge.py) says CALL with score >= early_call_min_ai_score, judged
                        against live launch waves, news, trending searches and Wikipedia
                    Posted to VIP as "⚡ EARLY NARRATIVE CALL" with the AI's reasoning and an early plan.
  3. EarlyCallTracker every refresh: market cap from the curve (on-chain) until migration, then from
                    the PumpSwap pool (DexScreener). Records the peak, posts plan pings (TP/stop/
                    trail/time stop), edits the message, and posts a proof copy to the FREE channel
                    once the call passes early_call_free_at (default 1.5x).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from . import trade_plan as TP
from .analysis.pda import pump_bonding_curve_address
from .discord_bot.embeds import early_call_embed, early_free_embed, money, plan_event_embed
from .discord_bot.publisher import PublishError

log = logging.getLogger("northstar.early_calls")

# Checks an early call cannot do without. Each must have RUN and PASSED: UNKNOWN (RPC failed,
# holder check skipped for low activity, creator not identified) holds the call back. These are
# the curve-stage equivalents of the trap checks; nothing else on the curve can stand in for them.
EARLY_REQUIRED_PASS = {
    "mint_authority": "mint authority",
    "freeze_authority": "freeze authority",
    "token_extensions": "token extensions",
    "holder_concentration": "holder concentration",
    "creator_holdings": "creator holding",
}


def serious_red_flags(report: dict) -> list[str]:
    """Rug-check red flags at HIGH or CRITICAL level (including medium flags the learner upgraded
    after they crashed 80%+ of the time). The standard path caps these at WATCH through the
    unified engine; early calls skip that engine, so before v2.5 a single serious flag (verdict
    CAUTION — e.g. one wallet holding 14% under a loosened holder threshold) could still be posted.
    """
    v = report.get("verdict") or {}
    return [str(x.get("text") or x.get("key")) for x in v.get("red") or []
            if isinstance(x, dict) and x.get("level") in ("high", "critical")]


def safety_unknowns(report: dict) -> list[str]:
    """Required early-call checks that did not run and pass (empty list = all verified)."""
    gates = {g.get("name"): g for g in report.get("gates") or [] if isinstance(g, dict)}
    missing = []
    for name, label in EARLY_REQUIRED_PASS.items():
        g = gates.get(name)
        if g is None:
            missing.append(f"{label} not checked")
        elif g.get("status") != "PASS":
            missing.append(f"{label} {str(g.get('status')).lower()}: {str(g.get('reason') or '')[:60]}")
    return missing

SCHEMA = """
CREATE TABLE IF NOT EXISTS early_calls (
    mint TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    channel_id INTEGER,
    message_id INTEGER,
    status TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'CURVE',
    entry_mcap REAL NOT NULL,
    current_mcap REAL,
    peak_mcap REAL,
    peak_at REAL,
    low_mcap REAL,
    plan TEXT,
    plan_sent TEXT NOT NULL DEFAULT '[]',
    free_message_id INTEGER,
    ai_score INTEGER,
    report TEXT NOT NULL,
    last_update_at REAL,
    last_edit_at REAL,
    closed_at REAL
);
CREATE INDEX IF NOT EXISTS ix_early_calls_status ON early_calls(status, created_at);
"""
OPEN = ("SENT", "DRY_RUN")


def init_schema(db) -> None:
    with db._lock:
        db._conn.executescript(SCHEMA)


# ----------------------------------------------------------------------------- curve radar
class CurveRadar:
    def __init__(self, s, db, dex):
        self.s, self.db, self.dex = s, db, dex
        self._last: dict[str, float] = {}
        init_schema(db)

    async def run_once(self, now: Optional[float] = None) -> list[str]:
        now = now or time.time()
        if self.dex is None:
            return []
        rows = self.db.fetchall(
            "SELECT mint FROM candidates WHERE discovered_at > ? AND sources LIKE '%pumpportal_new%' "
            "AND state NOT IN ('PUBLISHED','WARNING','CLOSED','QUALIFIED','IGNORED') "
            "AND mint NOT IN (SELECT mint FROM early_calls) ORDER BY discovered_at DESC LIMIT ?",
            (now - self.s.curve_radar_hours * 3600, self.s.curve_radar_max_tokens))
        mints = [r["mint"] for r in rows]
        from . import curve_watch as CW
        watching = getattr(self.s, "curve_watch_enabled", True)
        if watching:
            # coins inside their outcome window stay in the batch after they leave the launch list
            known = set(mints)
            mints += [m for m in CW.open_mints(self.db, now, float(getattr(self.s, "curve_watch_hours", 4.0)))
                      if m not in known]
        if not mints:
            return []
        try:
            pairs = await self.dex.tokens_batch(mints)
        except Exception as e:  # noqa: BLE001
            log.info("curve radar: DexScreener batch failed: %s", e)
            return []
        if watching:
            by_mint: dict[str, list] = {}
            for p in pairs:
                by_mint.setdefault(p.base_mint, []).append(p)
            res = CW.observe(self.db, by_mint, now, self.s)
            if res.get("entered") or res.get("closed"):
                log.info("curve watch: %s", res)
        best: dict[str, object] = {}
        for p in pairs:
            if p.base_mint in best and (best[p.base_mint].liquidity_usd or 0) >= (p.liquidity_usd or 0):
                continue
            best[p.base_mint] = p
        out = []
        lo, hi = self.s.early_call_min_mc * 0.8, self.s.early_call_max_mc
        for mint, p in best.items():
            if (p.dex_id or "").lower() not in ("pumpfun", "pump.fun", "pump"):
                continue                                        # only coins still on the curve
            mc = p.market_cap or p.fdv
            t5 = (p.txns or {}).get("m5") or {}
            buys, sells = t5.get("buys") or 0, t5.get("sells") or 0
            if not mc or not (lo <= mc <= hi) or buys < self.s.curve_radar_min_buys_m5 or buys <= sells:
                continue
            if now - self._last.get(mint, 0) < 600:
                continue
            self._last[mint] = now
            out.append(mint)
            self.db.upsert_discovery(mint, "curve_radar", 9.0, p.base_name, p.base_symbol,
                                     {"market_cap": mc, "buys_m5": buys, "sells_m5": sells})
            # launches are no longer queued on a timer, so this is what puts one in the queue
            self.db.execute("UPDATE candidates SET next_research_at=? WHERE mint=?", (now, mint))
        if out:
            log.info("curve radar: %d coin(s) entering the early band: %s", len(out), ", ".join(out[:5]))
        if len(self._last) > 20000:
            self._last = {k: v for k, v in self._last.items() if v > now - 3600}
        return out


# ----------------------------------------------------------------------------- caller
class EarlyCaller:
    def __init__(self, s, db, publisher, events=None, judge=None):
        self.s, self.db, self.pub, self.events, self.judge = s, db, publisher, events, judge
        init_schema(db)
        self._no_key_logged = False

    def gate(self, out) -> tuple[bool, str]:
        """Cheap checks before spending an AI call. (ok, reason)."""
        r = out.report
        if not self.s.early_call_enabled:
            return False, "early calls off"
        if r.get("lifecycle") != "PRE_MIGRATION_BONDING_CURVE":
            return False, "not on the bonding curve"
        if self.db.fetchone("SELECT 1 FROM early_calls WHERE mint=?", (out.mint,)):
            return False, "already called"
        pm = r.get("pre_migration") or {}
        mc = pm.get("market_cap_usd_current")
        if not mc or not (self.s.early_call_min_mc <= mc <= self.s.early_call_max_mc):
            return False, f"market cap {money(mc)} outside {money(self.s.early_call_min_mc)}-{money(self.s.early_call_max_mc)}"
        if pm.get("curve_complete"):
            return False, "curve complete (migrating)"
        checks = [(pm.get("trade_events", 0), self.s.early_call_min_trades, "trades"),
                  (pm.get("unique_buyers", 0), self.s.early_call_min_buyers, "unique buyers"),
                  (pm.get("buy_sell_count_ratio", 0), self.s.early_call_min_buy_sell_ratio, "buy/sell ratio"),
                  (pm.get("buy_sol_5m", 0), self.s.early_call_min_buy_sol_5m, "SOL bought")]
        for val, need, label in checks:
            if (val or 0) < need:
                return False, f"{label} {val} < {need} (last 5 min)"
        if any(g.get("status") == "FAIL" for g in r.get("gates") or [] if isinstance(g, dict)):
            return False, "failed a security check"
        level = (r.get("verdict") or {}).get("level")
        if level in ("RUG LIKELY", "HIGH RISK"):
            return False, f"rug check {level}"
        # A check that never ran is not a pass. Before v2.5 a curve coin whose holder check was
        # skipped (or whose RPC read failed) went to the AI as if its holders were clean.
        missing = safety_unknowns(r)
        if missing:
            return False, "safety not verified: " + "; ".join(missing)
        serious = serious_red_flags(r)
        if serious:
            return False, "serious rug-check red flag: " + "; ".join(serious[:2])
        if ((pm.get("socials") or {}).get("recycled")):
            return False, "recycled socials"
        if self.s.early_call_max_per_hour:
            n = self.db.fetchone("SELECT COUNT(*) n FROM early_calls WHERE created_at > ?", (time.time() - 3600,))
            if (n or {}).get("n", 0) >= self.s.early_call_max_per_hour:
                return False, "hourly early-call cap reached"
        return True, "ok"

    def coin_for_ai(self, out) -> dict:
        r = out.report
        ident, pm = r.get("identity") or {}, r.get("pre_migration") or {}
        soc = pm.get("socials") or {}
        links = ", ".join(f"{l.get('kind')}" + (f" @{l.get('handle')}" if l.get("handle") else "")
                          for l in soc.get("links") or [] if isinstance(l, dict))
        if soc.get("telegram_members"):
            links += f" (Telegram {soc['telegram_members']} members)"
        return {"name": ident.get("name"), "symbol": ident.get("symbol"), "description": ident.get("description"),
                "socials": links or "none", "stage": "pump.fun bonding curve",
                "market_cap_usd": pm.get("market_cap_usd_current"),
                "flow": f"{pm.get('buys', 0)} buys / {pm.get('sells', 0)} sells, {pm.get('unique_buyers', 0)} buyers, "
                        f"{pm.get('buy_sol_5m', 0):.1f} SOL bought",
                "ticker_crowding": (r.get("context") or {}).get("ticker_crowding_6h")}

    async def consider(self, out, now: Optional[float] = None) -> Optional[dict]:
        """Gate -> AI -> post. Returns the call row when posted."""
        now = now or time.time()
        ok, why = self.gate(out)
        if not ok:
            return None
        if self.judge is None or not self.judge.enabled:
            if not self._no_key_logged:
                log.warning("early narrative call skipped: no NORTHSTAR_ANTHROPIC_API_KEY set (AI judge off)")
                self._no_key_logged = True
            return None
        coin = self.coin_for_ai(out)
        feed = self.events.snapshot() if self.events else {}
        related = self.events.related(coin["name"], coin["symbol"], coin.get("description")) if self.events else {}
        ai = await self.judge.judge(out.mint, coin, feed, related, now)
        out.report["ai"] = ai
        ctx = out.report.setdefault("context", {})
        ctx["waves"] = related.get("waves") or []
        ctx["related"] = {k: v for k, v in related.items() if k != "waves"}
        if not ai or ai.get("verdict") != "CALL" or ai.get("score", 0) < self.s.early_call_min_ai_score:
            log.info("early call %s ($%s): AI %s %s/100 — not called", out.mint, coin["symbol"],
                     (ai or {}).get("verdict"), (ai or {}).get("score"))
            return None
        return await self.publish(out, ai, now)

    async def publish(self, out, ai: dict, now: float) -> Optional[dict]:
        pm = out.report.get("pre_migration") or {}
        entry = float(pm["market_cap_usd_current"])
        plan = TP.build_plan(entry, self.s, "early") if self.s.trade_plan_enabled else None
        call = {"entry_mcap": entry, "current_mcap": entry, "peak_mcap": entry, "peak_at": now, "sent_at": now,
                "phase": "CURVE", "last_update_at": now}
        embed = early_call_embed(out.report, call, TP.plan_lines(plan, money) if plan else None)
        target = self.s.discord_channel_id
        try:
            channel_id, message_id = await self.pub.publish(embed, target)
        except PublishError as e:
            log.warning("early call publish failed for %s: %s", out.mint, e)
            return None
        status = "DRY_RUN" if self.s.dry_run else "SENT"
        self.db.execute(
            "INSERT OR IGNORE INTO early_calls(mint, created_at, channel_id, message_id, status, phase, entry_mcap, "
            "current_mcap, peak_mcap, peak_at, low_mcap, plan, ai_score, report, last_update_at, last_edit_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (out.mint, now, channel_id, message_id, status, "CURVE", entry, entry, entry, now, entry,
             json.dumps(plan.__dict__) if plan else None, ai.get("score"), json.dumps(out.report, default=str), now, now))
        log.info("⚡ EARLY NARRATIVE CALL %s ($%s) at %s — %s (AI %d/100)", out.mint,
                 (out.report.get("identity") or {}).get("symbol"), money(entry), ai.get("narrative"), ai.get("score", 0))
        return self.db.fetchone("SELECT * FROM early_calls WHERE mint=?", (out.mint,))


# ----------------------------------------------------------------------------- tracker
class EarlyCallTracker:
    def __init__(self, s, db, publisher, rpc=None, dex=None, sol_price=None):
        self.s, self.db, self.pub, self.rpc, self.dex, self.sol_price = s, db, publisher, rpc, dex, sol_price
        init_schema(db)

    async def _caps(self, rows: list[dict]) -> dict[str, tuple[Optional[float], bool]]:
        """mint -> (market cap USD, migrated?)"""
        out: dict[str, tuple[Optional[float], bool]] = {}
        curve = [r for r in rows if r["phase"] == "CURVE"]
        pool = [r for r in rows if r["phase"] != "CURVE"]
        if curve and self.rpc is not None:
            sol_usd = None
            if self.sol_price is not None:
                try:
                    sol_usd = await self.sol_price.get()
                except Exception:  # noqa: BLE001
                    sol_usd = None
            keys = [pump_bonding_curve_address(r["mint"]) for r in curve]
            try:
                accounts = await self.rpc.get_multiple_accounts([k for k in keys if k])
            except Exception as e:  # noqa: BLE001
                log.info("early-call curve read failed: %s", e)
                accounts = []
            from .research import Researcher
            it = iter(accounts)
            for r, k in zip(curve, keys):
                acc = next(it, None) if k else None
                st = Researcher._parse_pump_curve_account(acc) if acc else None
                if not st:
                    continue
                if st.get("complete"):
                    out[r["mint"]] = (None, True)
                    pool.append(r)
                elif st.get("market_cap_sol") and sol_usd:
                    out[r["mint"]] = (st["market_cap_sol"] * sol_usd, False)
        if pool and self.dex is not None:
            try:
                pairs = await self.dex.tokens_batch([r["mint"] for r in pool])
            except Exception as e:  # noqa: BLE001
                log.info("early-call pool read failed: %s", e)
                pairs = []
            best: dict = {}
            for p in pairs:
                if (p.dex_id or "").lower() in ("pumpfun", "pump.fun"):
                    continue
                if p.base_mint not in best or (p.liquidity_usd or 0) > (best[p.base_mint].liquidity_usd or 0):
                    best[p.base_mint] = p
            for r in pool:
                p = best.get(r["mint"])
                if p and (p.market_cap or p.fdv):
                    out[r["mint"]] = (p.market_cap or p.fdv, True)
                elif r["mint"] not in out:
                    out[r["mint"]] = (None, True)
        return out

    async def run_once(self, now: Optional[float] = None) -> dict:
        now = now or time.time()
        counts = {"open": 0, "read": 0, "edited": 0, "events": 0, "free_posts": 0, "closed": 0}
        rows = self.db.fetchall("SELECT * FROM early_calls WHERE status IN (?, ?)", OPEN)
        counts["open"] = len(rows)
        if not rows:
            return counts
        caps = await self._caps(rows)
        for r in rows:
            mint = r["mint"]
            try:
                report = json.loads(r["report"])
            except (TypeError, ValueError):
                report = {"mint": mint}
            mc, migrated = caps.get(mint, (None, False))
            fields: dict = {}
            if migrated and r["phase"] == "CURVE":
                fields["phase"] = "POOL"
            if mc and mc > 0:
                counts["read"] += 1
                fields.update(current_mcap=mc, last_update_at=now, low_mcap=min(r["low_mcap"] or mc, mc))
                if mc > (r["peak_mcap"] or 0):
                    fields.update(peak_mcap=mc, peak_at=now)
            row = {**r, **fields}
            plan = TP.Plan(**json.loads(r["plan"])) if r.get("plan") else None
            sent = set(json.loads(r["plan_sent"] or "[]"))
            status_line = None
            if plan and mc:
                mult, peak_mult = mc / r["entry_mcap"], (row["peak_mcap"] or mc) / r["entry_mcap"]
                for kind, text in TP.evaluate(plan, mult, peak_mult, now - r["created_at"], sent, money):
                    sent.add(kind)
                    counts["events"] += 1
                    if kind == "ENTRY_CLOSED":
                        continue
                    ident = report.get("identity") or {}
                    try:
                        await self.pub.publish(plan_event_embed(
                            symbol=ident.get("symbol") or "?", name=ident.get("name") or "", mint=mint,
                            title="⚡ " + TP.EVENTS[kind], text=text, is_exit=kind in TP.EXIT_KINDS, early=True,
                            image_url=ident.get("image_url")), r["channel_id"])
                    except PublishError as e:
                        log.warning("early-call plan ping failed for %s: %s", mint, e)
                fields["plan_sent"] = json.dumps(sorted(sent))
            for kind, label in (("STOP", "🛑 stopped out"), ("TIME", "⏱️ time stop — exited"),
                                ("TRAIL", "🚪 trailing stop — exited"), ("DANGER", "🚨 exit posted"),
                                ("TP2", "🎯 TP2 hit — trailing the rest"), ("TP1", "🎯 TP1 hit — stop at break-even")):
                if kind in sent:
                    status_line = label
                    break
            # free-channel proof copy
            peak = row.get("peak_mcap") or 0
            if (not r.get("free_message_id") and self.s.discord_free_channel_id and r["entry_mcap"]
                    and peak / r["entry_mcap"] >= self.s.early_call_free_at):
                try:
                    _, fid = await self.pub.publish(early_free_embed(report, {**row}), self.s.discord_free_channel_id)
                    fields["free_message_id"] = fid
                    counts["free_posts"] += 1
                    log.info("early call %s passed %.1fx: posted to the free channel", mint, self.s.early_call_free_at)
                except PublishError as e:
                    log.warning("early-call free copy failed for %s: %s", mint, e)
            closing = now - r["created_at"] > self.s.early_call_track_hours * 3600
            if closing:
                fields.update(status="CLOSED", closed_at=now)
                counts["closed"] += 1
            # edit the VIP message (rate limited, or always on a status change / close)
            moved = mc and r.get("current_mcap") and abs(mc / r["current_mcap"] - 1) >= 0.01
            due = now - (r.get("last_edit_at") or 0) >= max(15, self.s.embed_edit_min_seconds)
            if r.get("message_id") and r.get("channel_id") and ((moved and due) or closing or "plan_sent" in fields
                                                                 or "phase" in fields):
                call = {**row, **fields, "sent_at": r["created_at"], "status_line": status_line}
                try:
                    await self.pub.edit(r["channel_id"], r["message_id"], early_call_embed(
                        report, call, TP.plan_lines(plan, money) if plan else None))
                    fields["last_edit_at"] = now
                    counts["edited"] += 1
                except PublishError as e:
                    log.info("early-call edit failed for %s: %s", mint, e)
                    if "not found" in str(e):
                        fields.update(status="UNTRACKED")
            if fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                self.db.execute(f"UPDATE early_calls SET {sets} WHERE mint=?", (*fields.values(), mint))
        return counts
