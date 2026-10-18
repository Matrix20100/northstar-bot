"""Self-learning: Northstar scores its own decisions against what really happened, then retunes itself.

The loop
  1. RECORD  every coin that got a full research pass (or a radar surge) is saved as a "shadow"
             with the numbers the decision used - called or not. Rejected coins matter as much as
             called ones: they show what a looser or tighter rule would have done.
  2. EVALUATE two hours later the real minute-by-minute price path is pulled from CoinGecko
             (free GeckoTerminal API first, your key as fallback) and stored.
  3. SIMULATE the trade plan is replayed on that path, candle by candle, assuming the worst case
             inside each candle (stop before take-profit), with fees and slippage.
  4. TUNE     every few hours each tunable setting is tried across its range. A change is only
             accepted when it improves results on the older data AND on the most recent data it was
             not fitted on (walk-forward check), with enough samples. Each run moves a setting at most
             one step, so one lucky coin can't swing the bot.
  5. APPLY    accepted values are stored in the database, applied live, announced in Discord with the
             numbers behind them, and can be reset with /learning_reset.

What it will not touch: the rug checks themselves (mint/freeze authority, holders, LP, bundles...)
and anything outside its rails. Those rails are wide; they only stop nonsense values.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
import time
from dataclasses import dataclass
from typing import Optional

from . import trade_plan as TP

log = logging.getLogger("northstar.learning")

EVAL_DELAY = 2 * 3600          # evaluate a shadow this long after the decision
PATH_MINUTES = 120             # minutes of price path stored per shadow
FEES = 0.03                    # round-trip fees + slippage assumed per trade (3%)
OUTLIER_CAP = 4.0              # cap one trade at +400% so a single moonshot can't drive tuning

SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    ts REAL NOT NULL,
    pool TEXT,
    entry_price REAL,
    entry_mcap REAL,
    liquidity REAL,
    action TEXT,
    call_type TEXT,
    called INTEGER NOT NULL DEFAULT 0,
    features TEXT NOT NULL,
    evaluated INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    path TEXT,
    peak_x REAL,
    trough_x REAL,
    minutes_to_peak REAL
);
CREATE INDEX IF NOT EXISTS ix_shadow_eval ON shadow_calls(evaluated, ts);
CREATE INDEX IF NOT EXISTS ix_shadow_mint ON shadow_calls(mint, ts);
CREATE TABLE IF NOT EXISTS learned_settings (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS learning_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    key TEXT NOT NULL,
    old REAL,
    new REAL,
    detail TEXT
);
"""


# ----------------------------------------------------------------------------- tunables
@dataclass(frozen=True)
class Tunable:
    key: str
    grid: tuple
    kind: str            # "select" (who gets called) | "plan" (standard plan) | "scalp" (scalp plan)
    label: str


TUNABLES = [
    Tunable("min_callout_market_cap", (30e3, 50e3, 75e3, 100e3, 150e3, 200e3, 300e3, 500e3, 1e6), "select",
            "minimum market cap"),
    Tunable("min_opportunity_score", (20, 30, 35, 40, 45, 50, 55, 60, 70, 80), "select", "minimum score"),
    Tunable("min_volume_acceleration", (0.0, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0, 4.0), "select",
            "minimum 1h volume vs 6h pace"),
    Tunable("min_buy_sell_ratio_h1", (0.0, 0.9, 1.0, 1.1, 1.2, 1.35, 1.5, 2.0, 2.5), "select",
            "minimum 1h buys per sell"),
    Tunable("plan_tp1", (1.2, 1.3, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0), "plan", "TP1"),
    Tunable("plan_stop", (0.5, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9), "plan", "stop-loss"),
    Tunable("plan_time_stop_minutes", (5, 10, 15, 20, 30, 45, 60, 90, 120), "plan", "time stop (minutes)"),
    Tunable("plan_trail_pct", (15, 20, 25, 30, 40, 50, 60), "plan", "trailing stop %"),
    Tunable("scalp_tp1", (1.1, 1.15, 1.2, 1.3, 1.4, 1.5, 1.75, 2.0), "scalp", "scalp TP1"),
    Tunable("scalp_tp2", (1.2, 1.3, 1.4, 1.6, 1.8, 2.0, 2.5, 3.0), "scalp", "scalp TP2"),
    Tunable("scalp_stop", (0.7, 0.75, 0.8, 0.85, 0.88, 0.9, 0.93, 0.95), "scalp", "scalp stop-loss"),
    Tunable("scalp_time_stop_minutes", (3, 5, 8, 10, 15, 20, 30), "scalp", "scalp time stop (minutes)"),
]
BY_KEY = {t.key: t for t in TUNABLES}


def init_schema(db) -> None:
    with db._lock:
        db._conn.executescript(SCHEMA)


def apply_learned(db, s) -> dict:
    """Load learned values onto the live settings (call at startup). Returns what was applied."""
    init_schema(db)
    applied = {}
    for row in db.fetchall("SELECT key, value FROM learned_settings"):
        t = BY_KEY.get(row["key"])
        if t is None:
            continue
        cur = getattr(s, t.key)
        val = type(cur)(row["value"]) if isinstance(cur, (int, float)) and not isinstance(cur, bool) else row["value"]
        setattr(s, t.key, val)
        applied[t.key] = val
    return applied


# ----------------------------------------------------------------------------- recording
def features_from(report: dict, pair, surge: Optional[dict]) -> dict:
    ms = report.get("market_structure") or {}
    score = report.get("score") or {}
    gates = [g for g in report.get("gates") or [] if isinstance(g, dict)]
    holders = report.get("holders") or {}
    return {
        "score": score.get("total"),
        "risk": report.get("risk"),
        "confidence": report.get("confidence"),
        "vol_acc": ms.get("volume_acceleration"),
        "bs_h1": ms.get("buy_sell_ratio_h1"),
        "extended": ms.get("extended"),
        "failed": sorted({g.get("name") for g in gates if g.get("status") == "FAIL"}),
        "unknown_critical": sorted({g.get("name") for g in gates if g.get("status") == "UNKNOWN" and g.get("critical")}),
        "top10": holders.get("top10_adj"),
        "surge": bool(surge),
        "surge_spike": (surge or {}).get("spike"),
        "vol_m5": (pair.vol or {}).get("m5") if pair else None,
        "change_m5": (pair.price_change or {}).get("m5") if pair else None,
        "change_h1": (pair.price_change or {}).get("h1") if pair else None,
        "trending": bool(report.get("trending")),
    }


def record(db, mint: str, report: dict, pair, action: str, call_type: Optional[str], called: bool,
           surge: Optional[dict] = None, now: Optional[float] = None) -> bool:
    """Save one shadow (at most one per coin per 30 minutes, unless this one was actually called)."""
    if pair is None or not pair.price_usd or not pair.pair_address:
        return False
    now = now or time.time()
    recent = db.fetchone("SELECT id, called FROM shadow_calls WHERE mint=? AND ts>? ORDER BY ts DESC LIMIT 1",
                         (mint, now - 1800))
    if recent and not called:
        return False
    feats = features_from(report, pair, surge)
    db.execute(
        "INSERT INTO shadow_calls(mint, ts, pool, entry_price, entry_mcap, liquidity, action, call_type, called, "
        "features) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (mint, now, pair.pair_address, pair.price_usd, pair.market_cap, pair.liquidity_usd, action,
         call_type, 1 if called else 0, json.dumps(feats, default=str)))
    return True


def backfill_from_snapshots(db, days: float = 7, now: Optional[float] = None) -> int:
    """Seed the learner from research already on disk (one shadow per coin per 2 hours)."""
    now = now or time.time()
    if db.fetchone("SELECT 1 FROM shadow_calls LIMIT 1"):
        return 0
    added, last = 0, {}
    for row in db.fetchall("SELECT mint, ts, action, report FROM research_snapshots WHERE ts > ? AND ts < ? "
                           "AND score IS NOT NULL ORDER BY ts", (now - days * 86400, now - EVAL_DELAY)):
        if row["ts"] - last.get(row["mint"], 0) < 7200:
            continue
        try:
            rep = json.loads(row["report"])
        except (TypeError, ValueError):
            continue
        if rep.get("lifecycle") == "PRE_MIGRATION_BONDING_CURVE":
            continue
        m = rep.get("market") or {}
        price = (m.get("price_usd") or {}).get("value")
        pool = (rep.get("pool") or {}).get("address")
        if not price or not pool:
            continue
        last[row["mint"]] = row["ts"]
        feats = features_from(rep, None, None)
        db.execute(
            "INSERT INTO shadow_calls(mint, ts, pool, entry_price, entry_mcap, liquidity, action, call_type, called, "
            "features) VALUES (?,?,?,?,?,?,?,?,0,?)",
            (row["mint"], row["ts"], pool, price, (m.get("market_cap") or {}).get("value"),
             (m.get("liquidity_usd") or {}).get("value"), row["action"], None, json.dumps(feats, default=str)))
        added += 1
    return added


# ----------------------------------------------------------------------------- evaluation
async def evaluate_due(db, cg, limit: int = 20, now: Optional[float] = None) -> int:
    """Fetch the real price path for shadows that are old enough. Returns how many were evaluated."""
    now = now or time.time()
    done = 0
    for row in db.fetchall("SELECT * FROM shadow_calls WHERE evaluated=0 AND ts < ? ORDER BY called DESC, ts "
                           "LIMIT ?", (now - EVAL_DELAY, limit)):
        try:
            candles = await cg.candles(row["pool"], row["ts"], row["ts"] + PATH_MINUTES * 60)
        except Exception:  # noqa: BLE001
            candles = []
        path = path_from(candles, row["entry_price"], row["ts"])
        if not path:
            attempts = row["attempts"] + 1
            db.execute("UPDATE shadow_calls SET attempts=?, evaluated=? WHERE id=?",
                       (attempts, -1 if attempts >= 3 else 0, row["id"]))
            continue
        peak_i = max(range(len(path)), key=lambda i: path[i][1])
        db.execute("UPDATE shadow_calls SET evaluated=1, path=?, peak_x=?, trough_x=?, minutes_to_peak=? WHERE id=?",
                   (json.dumps(path), path[peak_i][1], min(p[2] for p in path), path[peak_i][0], row["id"]))
        done += 1
    return done


def path_from(candles, entry_price: float, ts: float) -> list[list[float]]:
    """[[minute, high_x, low_x, close_x], ...] relative to the entry price, from the decision onward."""
    if not entry_price or entry_price <= 0:
        return []
    out = []
    for c_ts, _o, high, low, close, _v in candles:
        if c_ts + 60 <= ts or c_ts > ts + PATH_MINUTES * 60:
            continue
        out.append([round(max(0.0, (c_ts - ts) / 60), 1), round(high / entry_price, 4),
                    round(low / entry_price, 4), round(close / entry_price, 4)])
    return out


# ----------------------------------------------------------------------------- simulation
def simulate(path: list, v: dict) -> Optional[float]:
    """Profit (fraction of stake) of following the plan on a minute path. Worst case inside a candle."""
    if not path:
        return None
    if path[0][1] > 1 + v["max_entry_pct"] / 100 and path[0][2] > 1 + v["max_entry_pct"] / 100:
        return None                                   # never back inside the entry zone: no trade
    held, pnl, peak = 1.0, 0.0, 1.0
    tp1, tp2 = False, False
    s1 = min(1.0, v["tp1_sell_pct"] / 100)
    s2 = min(1.0 - s1, v["tp2_sell_pct"] / 100)
    for minute, high, low, close in path:
        stop = 1.0 if tp1 else v["stop"]              # break-even after TP1
        trail = peak * (1 - v["trail_pct"] / 100) if tp1 else 0.0
        floor = max(stop, trail)
        if low <= floor:                              # worst case: the stop fills before any take-profit
            # a normal dip fills near the stop (minus slippage); a crash candle fills at the
            # bottom of that candle, which is what really happens when a rug dumps through the stop
            fill = floor * 0.97 if low >= floor * 0.85 else max(low, min(close, floor))
            pnl += held * (fill - 1)
            held = 0.0
            break
        if not tp1 and high >= v["tp1"]:
            pnl += s1 * (v["tp1"] - 1)
            held -= s1
            tp1 = True
        if tp1 and not tp2 and high >= v["tp2"]:
            pnl += s2 * (v["tp2"] - 1)
            held -= s2
            tp2 = True
        if held <= 1e-9:
            break
        peak = max(peak, high)
        if not tp1 and minute >= v["time_stop_minutes"] and peak < v["time_stop_multiple"]:
            pnl += held * (close - 1)
            held = 0.0
            break
    if held > 1e-9:
        pnl += held * (path[-1][3] - 1)                # still open after the window: marked at last close
    return min(pnl, OUTLIER_CAP) - FEES


# ----------------------------------------------------------------------------- selection
RISK_OK = {"LOW_DETECTED": 0, "MODERATE": 1}
CONF_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def standard_eligible(f: dict, entry_mcap: Optional[float], s, overrides: dict) -> bool:
    """Would this shadow have been a STANDARD call under these thresholds? (rug checks never relaxed)"""
    g = lambda k: overrides.get(k, getattr(s, k))  # noqa: E731
    if f.get("failed") or f.get("unknown_critical"):
        return False
    if f.get("risk") not in RISK_OK or RISK_OK[f["risk"]] > (1 if s.max_allowed_risk == "MODERATE" else 0):
        return False
    if CONF_RANK.get(f.get("confidence") or "LOW", 0) < CONF_RANK.get(s.min_confidence, 2):
        return False
    if (f.get("score") or 0) < g("min_opportunity_score"):
        return False
    if g("min_callout_market_cap") and (not entry_mcap or entry_mcap < g("min_callout_market_cap")):
        return False
    acc, bs = f.get("vol_acc"), f.get("bs_h1")
    if g("min_volume_acceleration") and (acc is None or acc < g("min_volume_acceleration")):
        return False
    if g("min_buy_sell_ratio_h1") and (bs is None or bs < g("min_buy_sell_ratio_h1")):
        return False
    if f.get("extended") and s.block_extended_moves:
        return False
    return True


def scalp_eligible(f: dict, entry_mcap, liquidity, s) -> bool:
    from .scalp import SCALPABLE
    failed = set(f.get("failed") or [])
    if not failed or failed - SCALPABLE or set(f.get("unknown_critical") or []) & {"lp_lock"}:
        return False
    if not entry_mcap or entry_mcap < s.scalp_min_market_cap or not liquidity or liquidity < s.scalp_min_liquidity:
        return False
    strong = (f.get("vol_acc") or 0) >= 2.0 and (f.get("bs_h1") or 0) >= 1.3
    return bool(f.get("surge")) or (strong and not s.scalp_require_surge)


# ----------------------------------------------------------------------------- tuning
def _rows(db, days: float, now: float) -> list[dict]:
    out = []
    for r in db.fetchall("SELECT * FROM shadow_calls WHERE evaluated=1 AND ts > ? ORDER BY ts", (now - days * 86400,)):
        try:
            r["f"] = json.loads(r["features"])
            r["p"] = json.loads(r["path"])
        except (TypeError, ValueError):
            continue
        out.append(r)
    return out


def _total(rows, s, kind: str, overrides: dict) -> tuple[float, int]:
    """Sum of simulated profit and trade count for one setting combination."""
    profile = "scalp" if kind == "scalp" else "standard"
    v = TP.plan_values(s, profile)
    for k, val in overrides.items():
        pre = "scalp_" if profile == "scalp" else "plan_"
        if k.startswith(pre):
            v[k[len(pre):]] = val
    total, n = 0.0, 0
    for r in rows:
        if kind == "scalp":
            ok = scalp_eligible(r["f"], r["entry_mcap"], r["liquidity"], s)
        else:
            ok = standard_eligible(r["f"], r["entry_mcap"], s, overrides)
        if not ok:
            continue
        res = simulate(r["p"], v)
        if res is None:
            continue
        total += res
        n += 1
    return total, n


def _per_trade(total: float, n: int) -> float:
    return total / n if n else float("-inf")


def tune(db, s, days: float = 14, min_samples: int = 20, now: Optional[float] = None,
         improve_pct: float = 10.0) -> list[dict]:
    """One learning pass. Returns the accepted changes (already applied to ``s`` and saved)."""
    now = now or time.time()
    rows = _rows(db, days, now)
    if len(rows) < min_samples:
        log.info("learning: %d evaluated coins so far (need %d) - nothing to tune yet", len(rows), min_samples)
        return []
    split = int(len(rows) * 0.7)
    fit, check = rows[:split], rows[split:]
    changes = []
    for t in TUNABLES:
        cur = getattr(s, t.key)
        kind = t.kind
        base_fit = _total(fit, s, kind, {})
        base_chk = _total(check, s, kind, {})
        best = None
        for val in t.grid:
            if val == cur:
                continue
            tot_fit, n_fit = _total(fit, s, kind, {t.key: val})
            tot_chk, n_chk = _total(check, s, kind, {t.key: val})
            if n_fit < max(5, min_samples // 3) or n_chk < 3:
                continue
            # selection settings change WHICH coins are called: compare total profit.
            # plan settings change HOW each call is traded: compare profit per trade.
            if kind == "select":
                gain_fit, gain_chk = tot_fit - base_fit[0], tot_chk - base_chk[0]
                scale = max(0.05, abs(base_fit[0]))
            else:
                gain_fit = _per_trade(tot_fit, n_fit) - _per_trade(*base_fit)
                gain_chk = _per_trade(tot_chk, n_chk) - _per_trade(*base_chk)
                scale = max(0.02, abs(_per_trade(*base_fit)) if base_fit[1] else 0.02)
            if gain_fit <= 0 or gain_chk <= 0 or gain_fit / scale * 100 < improve_pct:
                continue
            if best is None or gain_fit > best[1]:
                best = (val, gain_fit, gain_chk, tot_fit, n_fit)
        if best is None:
            continue
        # move one grid step toward the best value (never jump across the range in one go)
        grid = sorted(t.grid)
        here = min(range(len(grid)), key=lambda i: abs(grid[i] - cur))
        step = here + (1 if best[0] > cur else -1)
        new = grid[max(0, min(len(grid) - 1, step))]
        if new == cur:
            continue
        if isinstance(cur, int) and not isinstance(cur, bool):
            new = int(new)
        setattr(s, t.key, new)
        detail = {"label": t.label, "best_seen": best[0], "fit_gain": round(best[1], 3), "check_gain": round(best[2], 3),
                  "samples": best[4], "evaluated_coins": len(rows)}
        db.execute("INSERT INTO learned_settings(key, value, updated_at) VALUES (?,?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                   (t.key, float(new), now))
        db.execute("INSERT INTO learning_log(ts, key, old, new, detail) VALUES (?,?,?,?,?)",
                   (now, t.key, float(cur), float(new), json.dumps(detail)))
        changes.append({"key": t.key, "old": cur, "new": new, **detail})
        log.info("learning: %s %s -> %s (%s)", t.key, cur, new, detail)
    return changes


def summary(db, s, days: float = 14, now: Optional[float] = None) -> dict:
    """What the learner currently sees: samples and simulated results of the live settings."""
    now = now or time.time()
    rows = _rows(db, days, now)
    std = _total(rows, s, "select", {})
    scp = _total(rows, s, "scalp", {})
    peaks = [r["peak_x"] for r in rows if r.get("peak_x")]
    return {"evaluated": len(rows),
            "pending": (db.fetchone("SELECT COUNT(*) n FROM shadow_calls WHERE evaluated=0") or {}).get("n", 0),
            "standard_trades": std[1], "standard_avg": round(_per_trade(*std), 3) if std[1] else None,
            "scalp_trades": scp[1], "scalp_avg": round(_per_trade(*scp), 3) if scp[1] else None,
            "median_peak": round(statistics.median(peaks), 2) if peaks else None,
            "learned": {r["key"]: r["value"] for r in db.fetchall("SELECT key, value FROM learned_settings")},
            "recent_changes": db.fetchall("SELECT ts, key, old, new, detail FROM learning_log ORDER BY ts DESC LIMIT 8")}


def reset(db, s, defaults) -> list[str]:
    """Forget learned values: live settings go back to .env / defaults."""
    keys = [r["key"] for r in db.fetchall("SELECT key FROM learned_settings")]
    for k in keys:
        if hasattr(defaults, k):
            setattr(s, k, getattr(defaults, k))
    db.execute("DELETE FROM learned_settings")
    db.execute("INSERT INTO learning_log(ts, key, old, new, detail) VALUES (?, 'RESET', NULL, NULL, ?)",
               (time.time(), json.dumps({"keys": keys})))
    return keys


def fmt_change(c: dict) -> str:
    sign = "+" if c["check_gain"] >= 0 else ""
    return (f"**{c['label']}**: {c['old']:g} → {c['new']:g}  (on {c['samples']} past coins it improved simulated "
            f"profit; held up on recent unseen coins: {sign}{c['check_gain']:.2f})")


def is_finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)
