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

from . import curve_watch as CW
from . import trade_plan as TP
from .analysis.pda import pump_bonding_curve_address
from .discord_bot.embeds import milestone_embed, early_call_embed, early_free_embed, money, plan_event_embed
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
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(early_calls)").fetchall()}
        if "milestones" not in cols:      # v4.0.0: milestones posted (2x, 3x, 5x, 10x)
            db._conn.execute("ALTER TABLE early_calls ADD COLUMN milestones TEXT NOT NULL DEFAULT '[]'")


# ----------------------------------------------------------------------------- curve radar
class CurveRadar:
    def __init__(self, s, db, dex):
        self.s, self.db, self.dex = s, db, dex
        self._last: dict[str, tuple] = {}     # mint -> (last looked at, 5-min volume then)
        init_schema(db)

    def _sample(self, mint: str, mc, now: float) -> None:
        """v4.0.0: one market-cap sample per scan (every 30 s) for the 15-minute pulse pattern."""
        if not mc:
            return
        hist = self.__dict__.setdefault("_hist", {})
        q = hist.setdefault(mint, [])
        q.append((now, float(mc)))
        if len(q) > 40:
            del q[:-40]

    def _record(self, mint: str, p, now: float) -> None:
        """Store the DexScreener numbers the runner check reads (runner.radar_metrics), with the pulse."""
        from . import runner as RN
        payload = _radar_payload(p, now)
        payload["pulse"] = RN.pulse_pattern(self.__dict__.get("_hist", {}).get(mint, []), now)
        self.db.upsert_discovery(mint, "curve_radar", 9.0, p.base_name, p.base_symbol, payload)

    async def snapshot(self, mints: list[str], now: Optional[float] = None) -> int:
        """v3.5.0: fresh numbers for coins found another way (tracked-wallet flow), whatever their band,
        so the runner check can judge them. Returns how many were recorded."""
        now = now or time.time()
        if self.dex is None or not mints:
            return 0
        try:
            pairs = await self.dex.tokens_batch(list(mints))
        except Exception as e:  # noqa: BLE001
            log.info("curve radar snapshot failed: %s", e)
            return 0
        best: dict = {}
        for p in pairs:
            if p.base_mint in mints and (p.base_mint not in best
                                         or (p.liquidity_usd or 0) > (best[p.base_mint].liquidity_usd or 0)):
                best[p.base_mint] = p
        n = 0
        for mint, p in best.items():
            if (p.dex_id or "").lower() in ("pumpfun", "pump.fun", "pump") and (p.market_cap or p.fdv):
                self._sample(mint, p.market_cap or p.fdv, now)
                self._record(mint, p, now)
                self._last[mint] = (now, float((p.vol or {}).get("m5") or 0))
                n += 1
        return n

    async def run_once(self, now: Optional[float] = None) -> list[str]:
        now = now or time.time()
        if self.dex is None:
            return []
        rows = self.db.fetchall(
            "SELECT mint FROM candidates WHERE discovered_at > ? AND sources LIKE '%pumpportal_new%' "
            "AND state NOT IN ('PUBLISHED','WARNING','CLOSED','QUALIFIED','IGNORED','DROPPED','RUGGED') "
            "AND mint NOT IN (SELECT mint FROM early_calls) ORDER BY discovered_at DESC LIMIT ?",
            (now - self.s.curve_radar_hours * 3600, self.s.curve_radar_max_tokens))
        mints = [r["mint"] for r in rows]
        researchable = set(mints)   # v3.4.0: curve-watch-only coins are measured, never re-queued
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
            res = CW.observe(self.db, by_mint, now, self.s, getattr(self, "attention", None))
            if res.get("entered") or res.get("closed"):
                log.info("curve watch: %s", res)
        best: dict[str, object] = {}
        for p in pairs:
            if p.base_mint in best and (best[p.base_mint].liquidity_usd or 0) >= (p.liquidity_usd or 0):
                continue
            best[p.base_mint] = p
        out = []
        runner = getattr(self.s, "runner_calls_enabled", True)
        if runner:
            # v3.0.0: the runner band (where call bots post), with real 5-minute volume
            lo, hi = float(self.s.runner_min_mc), float(self.s.runner_max_mc)
            min_vol = float(self.s.runner_min_vol_m5) * 0.7
        else:
            lo, hi, min_vol = self.s.early_call_min_mc * 0.8, self.s.early_call_max_mc, 0.0
        for mint, p in best.items():
            if (p.dex_id or "").lower() in ("pumpfun", "pump.fun", "pump"):
                self._sample(mint, p.market_cap or p.fdv, now)  # every scan, for the pulse pattern
            if mint not in researchable:
                continue                                        # dropped / untracked / called: measure only
            if (p.dex_id or "").lower() not in ("pumpfun", "pump.fun", "pump"):
                continue                                        # only coins still on the curve
            mc = p.market_cap or p.fdv
            t5 = (p.txns or {}).get("m5") or {}
            buys, sells = t5.get("buys") or 0, t5.get("sells") or 0
            vol = float((p.vol or {}).get("m5") or 0)
            if not mc or not (lo <= mc <= hi) or buys < self.s.curve_radar_min_buys_m5 or buys < sells:
                continue
            if vol < min_vol or (not runner and buys <= sells):
                continue
            # a coin is re-looked at most every 10 min, unless its volume has doubled since
            last = self._last.get(mint)
            if last and now - last[0] < 600 and vol < 2 * last[1]:
                continue
            self._last[mint] = (now, vol)
            out.append(mint)
            self._record(mint, p, now)
            # launches are no longer queued on a timer, so this is what puts one in the queue
            self.db.execute("UPDATE candidates SET next_research_at=? WHERE mint=?", (now, mint))
        if out:
            log.info("curve radar: %d coin(s) entering the early band: %s", len(out), ", ".join(out[:5]))
        if len(self._last) > 20000:
            self._last = {k: v for k, v in self._last.items() if v[0] > now - 3600}
        hist = self.__dict__.get("_hist")
        if hist and len(hist) > 5000:
            self._hist = {k: v for k, v in hist.items() if v and v[-1][0] > now - 1800}
        return out


# ----------------------------------------------------------------------------- caller
def _radar_payload(p, now: float) -> dict:
    t5 = (p.txns or {}).get("m5") or {}
    age = (now - p.pair_created_at) / 60 if p.pair_created_at else None
    return {"market_cap": p.market_cap or p.fdv, "buys_m5": t5.get("buys") or 0, "sells_m5": t5.get("sells") or 0,
            "vol_m5": float((p.vol or {}).get("m5") or 0), "chg_m5": (p.price_change or {}).get("m5"),
            "liq": p.liquidity_usd, "age_min": round(age, 1) if age is not None else None,
            "image_url": p.image_url, "seen_at": now}


class EarlyCaller:
    def __init__(self, s, db, publisher, events=None, judge=None):
        self.runner_stats = self._fresh_stats()
        self.s, self.db, self.pub, self.events, self.judge = s, db, publisher, events, judge
        self.attention = None            # set by the engine (attention.py)
        init_schema(db)
        self._no_key_logged = False

    # ------------------------------------------------------------------ v3.4.0 follow-up (v4.0.0 cadence)
    PERMANENT_GATES = {"mint_authority", "freeze_authority", "token_extensions"}

    def permanent_blockers(self, report: dict, m: Optional[dict]) -> list[str]:
        """Blockers that can never clear on this coin: a trap, too old, or migrating."""
        out = []
        for g in report.get("gates") or []:
            if isinstance(g, dict) and g.get("status") == "FAIL" and g.get("name") in self.PERMANENT_GATES:
                out.append(f"{g['name'].replace('_', ' ')}: {g.get('reason') or 'failed'}")
        if (report.get("verdict") or {}).get("level") == "RUG LIKELY":
            out.append("rug check RUG LIKELY")
        from . import runner as RN
        out += RN.extreme(report, self.s)            # v4.0.1: a 40%+ bundle or a 20%+ dev bag never clears
        pm = report.get("pre_migration") or {}
        if pm.get("curve_complete"):
            out.append("curve complete (migrating)")
        age = (m or {}).get("age_min")
        if age is not None and age > float(getattr(self.s, "runner_max_age_min", 45)):
            out.append(f"{age:.0f} min old")
        return out

    def score_bar(self, now: float) -> tuple[float, float]:
        """v4.2.0 (operator: "try and call out a coin at least twice an hour"): the score a call needs.
        With fewer than `runner_target_calls_per_hour` calls in the last hour, the bar is lowered by up
        to `runner_pacing_max_relief` points (2.5 per missing call), never under
        `curve_learn_min_floor`. Traps and extreme cases are never relaxed; they are not part of the score."""
        base = float(getattr(self.s, "runner_min_score", 45))
        target = int(getattr(self.s, "runner_target_calls_per_hour", 2) or 0)
        max_relief = float(getattr(self.s, "runner_pacing_max_relief", 5.0) or 0)
        if not target or not max_relief:
            return base, 0.0
        try:
            n = (self.db.fetchone("SELECT COUNT(*) AS n FROM early_calls WHERE created_at > ?", (now - 3600,))
                 or {}).get("n", 0)
        except Exception:  # noqa: BLE001
            return base, 0.0
        missing = max(0, target - int(n))
        relief = min(max_relief, 2.5 * missing)
        floor = float(getattr(self.s, "curve_learn_min_floor", 35.0))
        bar = max(min(base, floor), base - relief)
        return bar, round(base - bar, 1)

    def recheck_delay(self, checks: int) -> float:
        """1 min after the first look, then 2, 4, 8 ... capped at `runner_recheck_max_seconds` (10 min)."""
        first = float(getattr(self.s, "runner_recheck_seconds", 60.0))
        cap = float(getattr(self.s, "runner_recheck_max_seconds", 600.0))
        return min(cap, first * (2 ** max(0, checks - 1)))

    def follow_up(self, mint: str, report: dict, now: Optional[float] = None) -> Optional[tuple[str, str]]:
        """After a runner check that did not call: ("RECHECK", why) or ("DROP", why); None when the
        coin was called or never reached the check.

        v4.0.0 (operator: "coins scoring high get re-researched every 1 to 10 minutes, then lose score
        and frequency"): a coin within `runner_recheck_margin` points of the floor, with no trap, is
        researched again after 1, 2, 4, 8 min (max 10), up to `runner_max_rechecks` times (the pulse
        pattern changes between looks); everything else is dropped from tracking (the curve watch
        keeps measuring it for the learners)."""
        now = now or time.time()
        run = report.get("runner") or {}
        if not run.get("metrics") or run.get("called"):
            return None
        self.db.execute("CREATE TABLE IF NOT EXISTS runner_followups (mint TEXT PRIMARY KEY, checks INTEGER NOT NULL, "
                        "first_at REAL NOT NULL, last_at REAL NOT NULL, best_score REAL, last_block TEXT)")
        score = float(run.get("score") or 0)
        first = (run.get("blockers") or ["not called"])[0]
        row = self.db.fetchone("SELECT checks, best_score FROM runner_followups WHERE mint = ?", (mint,))
        checks = (row["checks"] if row else 0) + 1
        self.db.execute("INSERT INTO runner_followups(mint, checks, first_at, last_at, best_score, last_block) "
                        "VALUES (?,?,?,?,?,?) ON CONFLICT(mint) DO UPDATE SET checks = excluded.checks, "
                        "last_at = excluded.last_at, best_score = MAX(COALESCE(best_score, 0), excluded.best_score), "
                        "last_block = excluded.last_block", (mint, checks, now, now, score, first[:120]))
        need = float(run.get("bar") or self.score_bar(now)[0])      # the bar this check used (paced)
        margin = float(getattr(self.s, "runner_recheck_margin", 15.0))
        permanent = self.permanent_blockers(report, run.get("metrics"))
        limit = int(getattr(self.s, "runner_max_rechecks", 5))
        if permanent:
            res = "DROP", f"can never pass: {permanent[0]}"
        elif score < need - margin:
            res = "DROP", f"runner score {score:.0f} far under {need:.0f}"
        elif checks > limit:
            res = "DROP", f"still not called after {limit} rescan(s): {first}"
        else:
            res = "RECHECK", f"score {score:.0f}; rescan {checks} of {limit} in {self.recheck_delay(checks):.0f}s: {first}"
        self.runner_stats["rechecks" if res[0] == "RECHECK" else "dropped"] += 1
        return res

    @staticmethod
    def _fresh_stats() -> dict:
        return {"candidates": 0, "called": 0, "blockers": {}, "best": (0.0, ""), "rechecks": 0, "dropped": 0}

    def runner_summary(self, reset: bool = True) -> Optional[str]:
        """One line for the log: how many coins the runner check saw, and what stopped them."""
        st = self.runner_stats
        if reset:
            self.runner_stats = self._fresh_stats()
        if not st["candidates"]:
            return None
        top = sorted(st["blockers"].items(), key=lambda kv: -kv[1])[:5]
        return (f"runner check: {st['candidates']} coin(s) in the band, {st['called']} called, "
                f"{st.get('rechecks', 0)} to recheck, {st.get('dropped', 0)} dropped; best score "
                f"{st['best'][0]:.0f} (${st['best'][1]}); top blockers: "
                + ("; ".join(f"{k} x{n}" for k, n in top) or "none"))

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

    async def consider_runner(self, out, now: Optional[float] = None, sources: Optional[list] = None) -> Optional[dict]:
        """v3.0.0 runner call: momentum + every safety check passed + runner score. No AI needed."""
        from . import runner as RN

        now = now or time.time()
        r = out.report
        if r.get("lifecycle") != "PRE_MIGRATION_BONDING_CURVE":
            return None
        if self.db.fetchone("SELECT 1 FROM early_calls WHERE mint=?", (out.mint,)):
            return None
        m = RN.radar_metrics(self.db, out.mint, now)
        ident = r.get("identity") or {}
        att = (self.attention.score(out.mint, ident.get("name"), ident.get("symbol"), ident.get("description"), r,
                                    sources) if getattr(self, "attention", None) else {"score": 0, "reasons": []})
        r["attention"] = att
        sc = RN.score(r, m or {}, att) if m else {"score": 0.0}
        why = RN.blockers(r, m, self.s, now)      # v4.0.0: candidate rules and traps only
        need, relief = self.score_bar(now)
        sc["bar"], sc["pacing_relief"] = need, relief
        if sc["score"] < need:
            why.append(f"runner score {sc['score']:.0f} < {need:.0f}"
                       + (f" (bar lowered {relief:.0f} to reach {getattr(self.s, 'runner_target_calls_per_hour', 2)} "
                          "calls an hour)" if relief else ""))
        cap = int(getattr(self.s, "runner_max_per_hour", 6) or 0)
        if not why and cap:
            n = (self.db.fetchone("SELECT COUNT(*) AS n FROM early_calls WHERE created_at > ?", (now - 3600,)) or {})
            if n.get("n", 0) >= cap:
                why.append(f"hourly cap of {cap} runner calls reached")
        r["runner"] = {**sc, "metrics": m, "blockers": why[:8], "called": not why}
        if m:                                    # a coin the radar saw in the band: count it
            # v3.3.0: the curve learner learns the score floor from coins that were safe enough
            safety = [b for b in why if not b.startswith(("runner score", "hourly cap"))]
            CW.record_runner(self.db, out.mint, sc.get("score", 0.0), not safety, now,
                             block=safety[0].split(":")[0] if safety else None,
                             bundle_pct=(r.get("launch_buys") or {}).get("bundle_pct"))
            st = self.runner_stats
            st["candidates"] += 1
            st["called"] += 0 if why else 1
            for b in why[:4]:
                key = b.split(":")[0].split(" ($")[0][:60]
                st["blockers"][key] = st["blockers"].get(key, 0) + 1
            st["best"] = max(st["best"], (sc.get("score", 0), ident.get("symbol") or out.mint[:6]))
        if why:
            if r.get("runner_candidate"):
                log.info("runner %s ($%s): score %.0f - not called: %s", out.mint, ident.get("symbol"),
                         sc.get("score", 0), "; ".join(why[:3]))
            return None
        return await self.publish(out, {"score": round(sc["score"]), "verdict": "CALL",
                                        "narrative": "; ".join(att.get("reasons") or [])[:200] or None}, now,
                                  entry_usd=float(m["market_cap"]))

    async def consider(self, out, now: Optional[float] = None, sources: Optional[list] = None) -> Optional[dict]:
        """Gate -> AI -> post. Returns the call row when posted. v3.0.0: runner calls (no AI) unless
        NORTHSTAR_RUNNER_CALLS_ENABLED=false."""
        if getattr(self.s, "runner_calls_enabled", True):
            return await self.consider_runner(out, now, sources)
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

    async def publish(self, out, ai: dict, now: float, entry_usd: Optional[float] = None) -> Optional[dict]:
        pm = out.report.get("pre_migration") or {}
        entry = float(entry_usd or pm["market_cap_usd_current"])
        plan = TP.build_plan(entry, self.s, "early") if self.s.trade_plan_enabled else None
        call = {"entry_mcap": entry, "current_mcap": entry, "peak_mcap": entry, "peak_at": now, "sent_at": now,
                "phase": "CURVE", "last_update_at": now}
        from .discord_bot.embeds import compact_call_embed
        if plan:
            call["status_line"] = " · ".join(TP.plan_lines(plan, money)[:3])[:300]
        embed = compact_call_embed(out.report, call, kind="runner" if out.report.get("runner") else "narrative")
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
        log.info("⚡ %s %s ($%s) at %s — %s (score %d/100)",
                 "RUNNER CALL" if out.report.get("runner") else "EARLY NARRATIVE CALL", out.mint,
                 (out.report.get("identity") or {}).get("symbol"), money(entry), ai.get("narrative"), ai.get("score", 0))
        return self.db.fetchone("SELECT * FROM early_calls WHERE mint=?", (out.mint,))


# ----------------------------------------------------------------------------- tracker
def report_symbol(r) -> Optional[str]:
    try:
        return ((json.loads(r["report"]) or {}).get("identity") or {}).get("symbol")
    except (TypeError, ValueError, KeyError):
        return None


class EarlyCallTracker:
    def __init__(self, s, db, publisher, rpc=None, dex=None, sol_price=None, guard=None):
        self.s, self.db, self.pub, self.rpc, self.dex, self.sol_price = s, db, publisher, rpc, dex, sol_price
        # v5.1.3: DexScreener readings are checked before they move a price, a peak or a milestone
        from .price_guard import PriceGuard
        self.guard = guard or PriceGuard(rpc, None, sol_price)
        self._source: dict[str, str] = {}             # mint -> "chain" / "dex" for the last _caps() read
        self._chain_migrated: set = set()
        init_schema(db)

    async def _caps(self, rows: list[dict]) -> dict[str, tuple[Optional[float], bool]]:
        """mint -> (market cap USD, migrated?). self._source says where each number came from."""
        from . import price_guard as PG
        out: dict[str, tuple[Optional[float], bool]] = {}
        self._source = {}
        self._chain_migrated: set = set()
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
                from .research import read_curve_accounts
                accounts = await read_curve_accounts(self.rpc, [k for k in keys if k])
            except Exception as e:  # noqa: BLE001
                log.info("early-call curve read failed: %s", e)
                accounts = []
            from .research import Researcher
            it = iter(accounts)
            for r, k in zip(curve, keys):
                acc = next(it, None) if k else None
                st = Researcher._parse_pump_curve_account(acc) if acc else None
                if not st:
                    self._why_unread(r["mint"], "curve account missing or unreadable" if acc is None
                                     else "curve account did not parse: " + Researcher.curve_parse_problem(acc))
                    continue
                if st.get("complete"):
                    out[r["mint"]] = (None, True)
                    self._chain_migrated.add(r["mint"])      # the chain says so: a fact, whatever the price
                    pool.append(r)
                elif st.get("market_cap_sol") and sol_usd:
                    out[r["mint"]] = (st["market_cap_sol"] * sol_usd, False)
                    self._source[r["mint"]] = "chain"
                elif not sol_usd:
                    self._why_unread(r["mint"], "no SOL/USD price")
        # v3.4.1: a curve coin the chain read could not price is read from DexScreener instead (it lists
        # the curve as a "pumpfun" pair with a market cap). Before, it was silently skipped every time:
        # the first live runner call was never priced, so no plan pings, no 2x proof, no paper result.
        fallback = [r for r in curve if r["mint"] not in out and r not in pool]
        if (pool or fallback) and self.dex is not None:
            try:
                pairs = await self.dex.tokens_batch([r["mint"] for r in pool + fallback])
            except Exception as e:  # noqa: BLE001
                log.info("early-call pool read failed: %s", e)
                pairs = []
            best: dict = {}
            curve_pair: dict = {}
            for p in pairs:
                if PG.is_curve(p):
                    if PG.mcap_of(p) and p.base_mint not in curve_pair:
                        curve_pair[p.base_mint] = p
                    continue
                # v5.1.3: only a believable pool (SOL/USD quote, real liquidity for the market cap it claims)
                if not PG.trusted(p):
                    continue
                if p.base_mint not in best or (p.liquidity_usd or 0) > (best[p.base_mint].liquidity_usd or 0):
                    best[p.base_mint] = p
            for r in pool:
                p = best.get(r["mint"])
                if p and PG.mcap_of(p):
                    out[r["mint"]] = (PG.mcap_of(p), True)
                    self._source[r["mint"]] = "dex"
                elif r["mint"] not in out:
                    out[r["mint"]] = (None, True)
            for r in fallback:
                p = best.get(r["mint"])
                if p and PG.mcap_of(p):                            # it migrated after all
                    out[r["mint"]] = (PG.mcap_of(p), True)
                    self._source[r["mint"]] = "dex"
                elif r["mint"] in curve_pair:
                    cp = curve_pair[r["mint"]]
                    out[r["mint"]] = (PG.mcap_of(cp), False)
                    self._source[r["mint"]] = "dex"
                else:
                    self._why_unread(r["mint"], "DexScreener has no believable price for it either")
        return out

    def _why_unread(self, mint: str, why: str) -> None:
        """Say once per coin and reason why a call could not be priced (a silent miss hid the bug)."""
        seen = self.__dict__.setdefault("_unread_logged", set())
        if (mint, why) in seen:
            return
        seen.add((mint, why))
        log.warning("early-call tracker: could not price %s from the chain: %s", mint, why)

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
            if mc and self._source.get(mint) == "dex":
                # v5.1.3: a DexScreener number is checked before it moves the price, the peak or a milestone
                checked = await self.guard.verify(mint, mc, r["current_mcap"] or r["entry_mcap"],
                                                  (report_symbol(r) or ""))
                if checked is None:
                    # not confirmed: keep the last price; only the chain's own word moves the coin to POOL
                    mc, migrated = None, mint in self._chain_migrated
                else:
                    mc = checked
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
                    if kind in TP.EXIT_KINDS and not getattr(self.s, "post_drawdowns", False):
                        continue          # v4.0.0: only good news is posted; the exit is recorded
                    ident = report.get("identity") or {}
                    try:
                        await self.pub.publish(plan_event_embed(
                            symbol=ident.get("symbol") or "?", name=ident.get("name") or "", mint=mint,
                            title="⚡ " + TP.EVENTS[kind], text=text, is_exit=kind in TP.EXIT_KINDS, early=True,
                            image_url=ident.get("image_url")), r["channel_id"])
                    except PublishError as e:
                        log.warning("early-call plan ping failed for %s: %s", mint, e)
                if json.dumps(sorted(sent)) != (r.get("plan_sent") or "[]"):
                    fields["plan_sent"] = json.dumps(sorted(sent))      # v5.0.0: only a real change edits
            for kind, label in (("STOP", "🛑 stopped out"), ("TIME", "⏱️ time stop — exited"),
                                ("TRAIL", "🚪 trailing stop — exited"), ("DANGER", "🚨 exit posted"),
                                ("TP2", "🎯 TP2 hit — trailing the rest"), ("TP1", "🎯 TP1 hit — stop at break-even")):
                if kind in sent:
                    status_line = label
                    break
            # v4.0.0 milestones (2x, 3x, 5x, 10x since the call) go to VIP as they happen; the free
            # channel gets the call `free_callout_delay_minutes` (20) later with every milestone reached so
            # far posted at once, then each new milestone as it happens
            ident = report.get("identity") or {}
            peak_x = (row.get("peak_mcap") or 0) / r["entry_mcap"] if r["entry_mcap"] else 0
            done_ms = [float(x) for x in json.loads(r.get("milestones") or "[]")]
            ms_alert = {"mint": mint, "symbol": ident.get("symbol"), "entry_mcap": r["entry_mcap"],
                        "current_mcap": row.get("current_mcap"), "peak_mcap": row.get("peak_mcap"),
                        "sent_at": r["created_at"], "image_url": ident.get("image_url")}
            new_ms = [x for x in self.s.milestone_list if x >= 2 and peak_x >= x and x not in done_ms]
            free_ch = self.s.discord_free_channel_id
            free_sent = bool(r.get("free_message_id"))
            for x in new_ms:
                emb = milestone_embed(ms_alert, x)
                try:
                    await self.pub.publish(emb, r["channel_id"])
                except PublishError as e:
                    log.warning("early-call milestone %sx failed for %s: %s", x, mint, e)
                if free_ch and free_sent:
                    try:
                        await self.pub.publish(emb, free_ch)
                    except PublishError as e:
                        log.warning("early-call free milestone %sx failed for %s: %s", x, mint, e)
                done_ms.append(x)
            if new_ms:
                fields["milestones"] = json.dumps(sorted(done_ms))
            delay = float(getattr(self.s, "free_callout_delay_minutes", 20)) * 60
            exited = bool(sent & TP.EXIT_KINDS)
            if (free_ch and not free_sent and r["entry_mcap"] and now - r["created_at"] >= delay
                    and not exited and r["status"] in OPEN):
                call = {**row, "sent_at": r["created_at"], "status_line": None}
                try:
                    from .discord_bot.embeds import compact_call_embed
                    _, fid = await self.pub.publish(compact_call_embed(
                        report, call, kind="runner" if report.get("runner") else "early"), free_ch)
                    fields["free_message_id"] = fid
                    counts["free_posts"] += 1
                    for x in sorted(done_ms):                     # every milestone so far, at once
                        try:
                            await self.pub.publish(milestone_embed(ms_alert, x), free_ch)
                        except PublishError as e:
                            log.warning("early-call free milestone %sx failed for %s: %s", x, mint, e)
                    log.info("early call %s: copied to the free channel after %.0f min (%d milestone(s))",
                             mint, delay / 60, len(done_ms))
                except PublishError as e:
                    log.warning("early-call free copy failed for %s: %s", mint, e)
            closing = now - r["created_at"] > self.s.early_call_track_hours * 3600
            # v4.3.0: back at $5K (dead_mcap_usd) or lower after a $10K+ call = dead. Close it now (nothing is
            # posted: only good news goes out) and record DEAD so paper trading sells and the learner knows
            from . import brain as B
            low_now = row.get("current_mcap") if mc else None
            if not closing and low_now and B.is_dead(low_now, r["entry_mcap"]):
                sent.add("DEAD")
                fields["plan_sent"] = json.dumps(sorted(sent))
                status_line = "💀 coin died — tracking ended"
                closing = True
                counts["dead"] = counts.get("dead", 0) + 1
                log.info("early call %s: dead at $%.0f market cap (called at $%.0f); closed", mint, low_now,
                         r["entry_mcap"] or 0)
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
                    from .discord_bot.embeds import compact_call_embed
                    if not call.get("status_line") and plan:
                        call["status_line"] = " · ".join(TP.plan_lines(plan, money)[:3])[:300]
                    await self.pub.edit(r["channel_id"], r["message_id"], compact_call_embed(
                        report, call, kind="runner" if report.get("runner") else "narrative"))
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
