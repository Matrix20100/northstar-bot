"""Self-learning: Northstar scores its own decisions against what really happened, then retunes itself.

The loop
  1. RECORD  every coin that got a full research pass (or a radar surge) is saved as a "shadow"
             with the numbers the decision used - called or not. Rejected coins matter as much as
             called ones: they show what a looser or tighter rule would have done.
  2. EVALUATE four hours later the real minute-by-minute price path is pulled from CoinGecko
             (free GeckoTerminal API first, your key as fallback) and stored.
  3. SIMULATE the trade plan is replayed on that path, candle by candle, assuming the worst case
             inside each candle (stop before take-profit), with fees and slippage.
  4. TUNE     every few hours each tunable setting is tried across its range. A change is only
             accepted when it improves results on the older data AND on the most recent data it was
             not fitted on (walk-forward check), with enough samples. Each run moves a setting at most
             one step, so one lucky coin can't swing the bot.
  5. APPLY    accepted values are stored in the database, applied live, announced in Discord with the
             numbers behind them, and can be reset with /learning_reset.

What learns (all with the same walk-forward check):
  * the BRAIN (brain.py): a model of which coin features lead to a RUNNER (2x before -40%);
    it gives every coin learned runner odds and earns a say in calls only once it beats a coin flip
  * who gets called: market cap floor, score bar, momentum bars, and the brain's own odds bars
  * how calls are traded: TP1/TP2, how much to sell at TP1, stop, trailing stop, time stop
  * rug scalps: the plan and the minimum size of a scalpable coin
  * the radar: what counts as a surge (volume spike, buys, buy/sell, 5-min volume, max 5-min move),
    learned from every surge AND near-miss it saw

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

from . import brain as B
from . import trade_plan as TP

log = logging.getLogger("northstar.learning")

EVAL_DELAY = 4 * 3600          # evaluate a shadow this long after the decision
PATH_MINUTES = 240             # minutes of price path stored per shadow (runners need room to run)
FEES = 0.03                    # round-trip fees + slippage assumed per trade (3%)
OUTLIER_CAP = 9.0              # cap one trade at +900%: runners count, but one moonshot can't drive tuning
                               # alone (every change must also hold up on recent unseen coins)

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
CREATE TABLE IF NOT EXISTS radar_obs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    ts REAL NOT NULL,
    pool TEXT,
    price REAL,
    metrics TEXT NOT NULL,
    fired INTEGER NOT NULL DEFAULT 0,
    evaluated INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    path TEXT
);
CREATE INDEX IF NOT EXISTS ix_radar_obs_eval ON radar_obs(evaluated, ts);
CREATE TABLE IF NOT EXISTS flag_outcomes (
    key TEXT PRIMARY KEY,
    n INTEGER NOT NULL,
    crash INTEGER NOT NULL,
    run2x INTEGER NOT NULL,
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
    kind: str            # select | brain (who gets called) | plan | scalp (how it is traded)
                         # scalpselect (which rugs are scalped) | radar (what counts as a surge)
    label: str


TUNABLES = [
    Tunable("min_callout_market_cap", (30e3, 50e3, 75e3, 100e3, 150e3, 200e3, 300e3, 500e3, 1e6), "select",
            "minimum market cap"),
    Tunable("min_opportunity_score", (20, 30, 35, 40, 45, 50, 55, 60, 70, 80), "select", "minimum score"),
    Tunable("min_volume_acceleration", (0.0, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0, 4.0), "select",
            "minimum 1h volume vs 6h pace"),
    Tunable("min_buy_sell_ratio_h1", (0.0, 0.9, 1.0, 1.1, 1.2, 1.35, 1.5, 2.0, 2.5), "select",
            "minimum 1h buys per sell"),
    # runner odds: about 1 in 8 researched coins is a runner, so useful bars sit well below 50%
    Tunable("brain_min_probability", (0.0, 0.04, 0.06, 0.08, 0.1, 0.12, 0.15, 0.2, 0.25), "brain",
            "minimum learned runner odds for a call"),
    Tunable("brain_promote_probability", (0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5, 1.01), "brain",
            "learned runner odds that override the hand-written score bar"),
    Tunable("plan_tp1", (1.2, 1.3, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0), "plan", "TP1"),
    Tunable("plan_tp2", (1.5, 1.75, 2.0, 2.5, 3.0, 4.0, 5.0, 7.0, 10.0), "plan", "TP2"),
    Tunable("plan_tp1_sell_pct", (25, 33, 50, 66, 75), "plan", "% sold at TP1"),
    Tunable("plan_stop", (0.5, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9), "plan", "stop-loss"),
    Tunable("plan_time_stop_minutes", (5, 10, 15, 20, 30, 45, 60, 90, 120), "plan", "time stop (minutes)"),
    Tunable("plan_trail_pct", (15, 20, 25, 30, 40, 50, 60), "plan", "trailing stop %"),
    Tunable("scalp_tp1", (1.1, 1.15, 1.2, 1.3, 1.4, 1.5, 1.75, 2.0), "scalp", "scalp TP1"),
    Tunable("scalp_tp2", (1.2, 1.3, 1.4, 1.6, 1.8, 2.0, 2.5, 3.0), "scalp", "scalp TP2"),
    Tunable("scalp_stop", (0.7, 0.75, 0.8, 0.85, 0.88, 0.9, 0.93, 0.95), "scalp", "scalp stop-loss"),
    Tunable("scalp_time_stop_minutes", (3, 5, 8, 10, 15, 20, 30), "scalp", "scalp time stop (minutes)"),
    Tunable("scalp_min_market_cap", (30e3, 50e3, 75e3, 100e3, 150e3, 250e3), "scalpselect",
            "smallest market cap worth scalping"),
    Tunable("scalp_min_liquidity", (10e3, 15e3, 20e3, 30e3, 50e3), "scalpselect", "smallest liquidity worth scalping"),
    Tunable("radar_volume_spike", (1.5, 2.0, 2.5, 3.0, 4.0, 5.0), "radar", "radar: volume spike needed"),
    Tunable("radar_min_buys_m5", (5, 8, 12, 20, 30), "radar", "radar: buys in 5 min"),
    Tunable("radar_buy_sell_ratio", (1.0, 1.15, 1.3, 1.5, 2.0), "radar", "radar: buys per sell"),
    Tunable("radar_min_volume_m5", (1000, 2000, 3000, 5000, 10000), "radar", "radar: 5-min volume"),
    Tunable("radar_max_change_m5", (20, 30, 45, 60, 90, 150), "radar", "radar: max 5-min price move"),
]
BY_KEY = {t.key: t for t in TUNABLES}


def init_schema(db) -> None:
    with db._lock:
        db._conn.executescript(SCHEMA)
    B.init_schema(db)


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
        "unknown": sorted({g.get("name") for g in gates if g.get("status") == "UNKNOWN"}),
        "passed": sorted({g.get("name") for g in gates if g.get("status") == "PASS"}),
        "top10": holders.get("top10_adj"),
        "surge": bool(surge),
        "surge_spike": (surge or {}).get("spike"),
        "vol_m5": (pair.vol or {}).get("m5") if pair else None,
        "change_m5": (pair.price_change or {}).get("m5") if pair else None,
        "change_h1": (pair.price_change or {}).get("h1") if pair else None,
        "trending": bool(report.get("trending")),
    }


def record(db, mint: str, report: dict, pair, action: str, call_type: Optional[str], called: bool,
           surge: Optional[dict] = None, now: Optional[float] = None, brain_p: Optional[float] = None) -> bool:
    """Save one shadow (at most one per coin per 30 minutes, unless this one was actually called)."""
    if pair is None or not pair.price_usd or not pair.pair_address:
        return False
    now = now or time.time()
    recent = db.fetchone("SELECT id, called FROM shadow_calls WHERE mint=? AND ts>? ORDER BY ts DESC LIMIT 1",
                         (mint, now - 1800))
    if recent and not called:
        return False
    feats = features_from(report, pair, surge)
    vec = B.vector(report, now)
    db.execute(
        "INSERT INTO shadow_calls(mint, ts, pool, entry_price, entry_mcap, liquidity, action, call_type, called, "
        "features, vector, brain_p) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (mint, now, pair.pair_address, pair.price_usd, pair.market_cap, pair.liquidity_usd, action,
         call_type, 1 if called else 0, json.dumps(feats, default=str), json.dumps(vec), brain_p))
    return True


def backfill_from_snapshots(db, days: float = 7, now: Optional[float] = None) -> int:
    """Seed the learner from research already on disk (one shadow per coin per 2 hours of research)."""
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
            "features, vector) VALUES (?,?,?,?,?,?,?,?,0,?,?)",
            (row["mint"], row["ts"], pool, price, (m.get("market_cap") or {}).get("value"),
             (m.get("liquidity_usd") or {}).get("value"), row["action"], None, json.dumps(feats, default=str),
             json.dumps(B.vector(rep, row["ts"]))))
        added += 1
    return added


def backfill_vectors(db, limit: int = 3000) -> int:
    """Give older shadows (recorded before the brain existed) their brain numbers, from the research
    report saved at the same moment. Runs in small batches; returns how many were filled."""
    filled = 0
    for r in db.fetchall("SELECT id, mint, ts FROM shadow_calls WHERE vector IS NULL ORDER BY ts DESC LIMIT ?",
                         (limit,)):
        snap = db.fetchone("SELECT ts, report FROM research_snapshots WHERE mint=? AND ts BETWEEN ? AND ? "
                           "ORDER BY ABS(ts - ?) LIMIT 1", (r["mint"], r["ts"] - 300, r["ts"] + 300, r["ts"]))
        vec = None
        if snap:
            try:
                vec = B.vector(json.loads(snap["report"]), r["ts"])
            except (TypeError, ValueError):
                vec = None
        # "{}" marks "looked, nothing usable" so it is not retried forever
        db.execute("UPDATE shadow_calls SET vector=? WHERE id=?", (json.dumps(vec) if vec else "{}", r["id"]))
        filled += 1 if vec else 0
    return filled


CRASH_X = 0.3          # "crashed" = fell below 30% of the price at the decision


def flag_outcomes(db, now: Optional[float] = None, days: float = 30.0, min_hours: float = 6.0) -> dict:
    """How often each rug-check red flag was followed by a crash (and by a 2x first), measured on
    Northstar's own 24h decision tracking. Stored in flag_outcomes; '_all' is the baseline."""
    now = now or time.time()
    try:
        rows = db.fetchall("SELECT verdict_keys, entry_price, max_price, min_price FROM decision_tracking "
                           "WHERE verdict_keys IS NOT NULL AND samples > 0 AND entry_price > 0 AND decided_at > ? "
                           "AND (done = 1 OR decided_at < ?)", (now - days * 86400, now - min_hours * 3600))
    except Exception:  # noqa: BLE001 - older databases
        return {}
    stats: dict[str, list[int]] = {}
    for r in rows:
        try:
            keys = json.loads(r["verdict_keys"])
        except (TypeError, ValueError):
            continue
        crash = 1 if (r["min_price"] or r["entry_price"]) / r["entry_price"] < CRASH_X else 0
        run = 1 if (r["max_price"] or 0) / r["entry_price"] >= 2 else 0
        for k in list(keys) + ["_all"]:
            s_ = stats.setdefault(k, [0, 0, 0])
            s_[0] += 1
            s_[1] += crash
            s_[2] += run
    for k, (n, c, r2) in stats.items():
        db.execute("INSERT INTO flag_outcomes(key, n, crash, run2x, updated_at) VALUES (?,?,?,?,?) "
                   "ON CONFLICT(key) DO UPDATE SET n=excluded.n, crash=excluded.crash, run2x=excluded.run2x, "
                   "updated_at=excluded.updated_at", (k, n, c, r2, now))
    return {k: {"n": n, "crash": c, "run2x": r2} for k, (n, c, r2) in stats.items()}


def load_flag_stats(db) -> dict:
    try:
        return {r["key"]: {"n": r["n"], "crash": r["crash"], "run2x": r["run2x"]}
                for r in db.fetchall("SELECT key, n, crash, run2x FROM flag_outcomes")}
    except Exception:  # noqa: BLE001
        return {}


def prune(db, now: Optional[float] = None, keep_days: float = 45.0, radar_keep_days: float = 21.0) -> dict:
    """Keep the learning tables bounded (the radar alone can log ~20k near-misses a day)."""
    now = now or time.time()
    out = {}
    for table, days in (("shadow_calls", keep_days), ("radar_obs", radar_keep_days)):
        try:
            with db._lock:
                out[table] = db._conn.execute(f"DELETE FROM {table} WHERE ts < ?", (now - days * 86400,)).rowcount
        except Exception:  # noqa: BLE001 - housekeeping must never break the bot
            out[table] = 0
    return out


# ----------------------------------------------------------------------------- evaluation
async def evaluate_due(db, cg, limit: int = 20, now: Optional[float] = None) -> int:
    """Fetch the real price path for shadows that are old enough. Returns how many were evaluated."""
    now = now or time.time()
    if cg is None:
        return 0                      # no price source: wait, don't spend the coins' attempts
    done = 0
    # calls first, then the newest due coins: learning should follow the market as it is now
    for row in db.fetchall("SELECT * FROM shadow_calls WHERE evaluated=0 AND ts < ? ORDER BY called DESC, ts DESC "
                           "LIMIT ?", (now - EVAL_DELAY, limit)):
        try:
            candles = await cg.candles(row["pool"], row["ts"], row["ts"] + PATH_MINUTES * 60, token=row["mint"])
        except Exception:  # noqa: BLE001
            candles = []
        path = path_from(candles, row["entry_price"], row["ts"])
        if path and not (0.2 <= path[0][3] <= 5):
            path = []          # first candle far from the recorded price: wrong pool/token data, not usable
        if not path:
            attempts = row["attempts"] + 1
            db.execute("UPDATE shadow_calls SET attempts=?, evaluated=? WHERE id=?",
                       (attempts, -1 if attempts >= 3 else 0, row["id"]))
            continue
        peak_i = max(range(len(path)), key=lambda i: path[i][1])
        db.execute("UPDATE shadow_calls SET evaluated=1, path=?, peak_x=?, trough_x=?, minutes_to_peak=?, win=? "
                   "WHERE id=?", (json.dumps(path), path[peak_i][1], min(p[2] for p in path), path[peak_i][0],
                                  B.outcome(path), row["id"]))
        done += 1
    return done


async def evaluate_radar_due(db, cg, limit: int = 10, now: Optional[float] = None) -> int:
    """Price paths for radar observations (surges and near-misses), same rules as shadows."""
    now = now or time.time()
    if cg is None:
        return 0                      # no price source: wait, don't spend the coins' attempts
    done = 0
    for row in db.fetchall("SELECT * FROM radar_obs WHERE evaluated=0 AND ts < ? ORDER BY fired DESC, ts DESC LIMIT ?",
                           (now - EVAL_DELAY, limit)):
        try:
            candles = await cg.candles(row["pool"], row["ts"], row["ts"] + PATH_MINUTES * 60, token=row["mint"])
        except Exception:  # noqa: BLE001
            candles = []
        path = path_from(candles, row["price"], row["ts"])
        if path and not (0.2 <= path[0][3] <= 5):
            path = []
        if not path:
            attempts = row["attempts"] + 1
            db.execute("UPDATE radar_obs SET attempts=?, evaluated=? WHERE id=?",
                       (attempts, -1 if attempts >= 3 else 0, row["id"]))
            continue
        db.execute("UPDATE radar_obs SET evaluated=1, path=? WHERE id=?", (json.dumps(path), row["id"]))
        done += 1
    return done


def path_from(candles, entry_price: float, ts: float) -> list[list[float]]:
    """[[minute, high_x, low_x, close_x], ...] relative to the entry price, from the decision onward."""
    if not entry_price or entry_price <= 0:
        return []
    out = []
    for c_ts, _o, high, low, close, _v in candles:
        if c_ts < ts or c_ts > ts + PATH_MINUTES * 60:      # whole candles after the decision only
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
        if not tp1 and minute >= v["time_stop_minutes"] and close < v["time_stop_multiple"]:
            pnl += held * (close - 1)
            held = 0.0
            break
    if held > 1e-9:
        pnl += held * (path[-1][3] - 1)                # still open after the window: marked at last close
    return min(pnl, OUTLIER_CAP) - FEES


# ----------------------------------------------------------------------------- selection
RISK_OK = {"LOW_DETECTED": 0, "MODERATE": 1}
CONF_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def standard_eligible(f: dict, entry_mcap: Optional[float], s, overrides: dict, bp: Optional[float] = None) -> bool:
    """Would this shadow have been a STANDARD call under these thresholds? (rug checks never relaxed)

    ``bp`` is the brain's out-of-sample odds for this coin (None when the brain had no trusted
    opinion). Same rules as live: odds below the minimum veto a call; odds at or above the promote
    bar replace ONLY the hand-written score/confidence bars - never a safety or momentum rule."""
    g = lambda k: overrides.get(k, getattr(s, k))  # noqa: E731
    if f.get("failed") or f.get("unknown_critical"):
        return False
    if f.get("risk") not in RISK_OK or RISK_OK[f["risk"]] > (1 if s.max_allowed_risk == "MODERATE" else 0):
        return False
    promoted = bp is not None and bp >= g("brain_promote_probability")
    if bp is not None and bp < g("brain_min_probability"):
        return False
    if not promoted and CONF_RANK.get(f.get("confidence") or "LOW", 0) < CONF_RANK.get(s.min_confidence, 2):
        return False
    if not promoted and (f.get("score") or 0) < g("min_opportunity_score"):
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


def scalp_eligible(f: dict, entry_mcap, liquidity, s, overrides: Optional[dict] = None) -> bool:
    from .scalp import MUST_PASS, SCALPABLE, TRAPS
    failed = set(f.get("failed") or [])
    unknown = set(f.get("unknown") or []) | set(f.get("unknown_critical") or [])
    if not failed or failed - SCALPABLE or unknown & TRAPS or not MUST_PASS <= set(f.get("passed") or []):
        return False                       # same rules as live posting (scalp.check)
    g = lambda k: (overrides or {}).get(k, getattr(s, k))  # noqa: E731
    if not entry_mcap or entry_mcap < g("scalp_min_market_cap") or not liquidity or liquidity < g("scalp_min_liquidity"):
        return False
    strong = (f.get("vol_acc") or 0) >= 2.0 and (f.get("bs_h1") or 0) >= 1.3
    return bool(f.get("surge")) or (strong and not s.scalp_require_surge)


# ----------------------------------------------------------------------------- tuning
def _rows(db, days: float, now: float, brain_trusted: bool = False) -> list[dict]:
    out = []
    for r in db.fetchall("SELECT * FROM shadow_calls WHERE evaluated=1 AND ts > ? ORDER BY ts", (now - days * 86400,)):
        try:
            r["f"] = json.loads(r["features"])
            r["p"] = json.loads(r["path"])
        except (TypeError, ValueError):
            continue
        r["bp"] = r.get("brain_oof") if brain_trusted else None
        out.append(r)
    return out


def _radar_rows(db, days: float, now: float) -> list[dict]:
    out = []
    for r in db.fetchall("SELECT * FROM radar_obs WHERE evaluated=1 AND ts > ? ORDER BY ts", (now - days * 86400,)):
        try:
            r["m"] = json.loads(r["metrics"])
            r["p"] = json.loads(r["path"])
        except (TypeError, ValueError):
            continue
        out.append(r)
    return out


def _total(rows, s, kind: str, overrides: dict) -> tuple[float, int]:
    """Sum of simulated profit and trade count for one setting combination."""
    profile = "scalp" if kind in ("scalp", "scalpselect") else "standard"
    v = TP.plan_values(s, profile)
    for k, val in overrides.items():
        pre = "scalp_" if profile == "scalp" else "plan_"
        if k.startswith(pre):
            v[k[len(pre):]] = val
    total, n = 0.0, 0
    for r in rows:
        if kind == "radar":
            from .radar import passes
            ok = passes(r["m"], s, overrides)
        elif kind in ("scalp", "scalpselect"):
            ok = scalp_eligible(r["f"], r["entry_mcap"], r["liquidity"], s, overrides)
        else:
            ok = standard_eligible(r["f"], r["entry_mcap"], s, overrides, r.get("bp"))
        if not ok:
            continue
        res = simulate(r["p"], v)
        if res is None:
            continue
        total += res
        n += 1
    return total, n


def _plan_ok(s, key: str, value) -> bool:
    profile = "scalp" if key.startswith("scalp_") else "standard"
    v = TP.plan_values(s, profile)
    v[key.split("_", 1)[1]] = value
    return TP.valid_values(v)


def _per_trade(total: float, n: int) -> float:
    return total / n if n else float("-inf")


def tune(db, s, days: float = 14, min_samples: int = 20, now: Optional[float] = None,
         improve_pct: float = 10.0) -> list[dict]:
    """One learning pass. Returns the accepted changes (already applied to ``s`` and saved)."""
    now = now or time.time()
    try:
        B.store_tracking_vectors(db)
        flag_outcomes(db, now)
    except Exception:  # noqa: BLE001 - flag statistics are optional
        log.warning("flag outcome statistics failed", exc_info=True)
    model = B.train(db, s, now) if getattr(s, "brain_enabled", True) else None
    trusted = bool(model and model.trusted)
    rows = _rows(db, days, now, trusted)
    radar_rows = _radar_rows(db, days, now)
    sets = {}
    for name, data in (("coins", rows), ("radar", radar_rows)):
        split = int(len(data) * 0.7)
        sets[name] = (data[:split], data[split:], len(data))
    if len(rows) < min_samples and len(radar_rows) < min_samples:
        log.info("learning: %d evaluated coins and %d radar observations so far (need %d) - nothing to tune yet",
                 len(rows), len(radar_rows), min_samples)
        return []
    changes = []
    for t in TUNABLES:
        cur = getattr(s, t.key)
        kind = t.kind
        if kind == "brain" and not trusted:
            continue                                       # the brain has no say until it is trusted
        fit, check, n_all = sets["radar" if kind == "radar" else "coins"]
        if n_all < min_samples:
            continue
        base_fit = _total(fit, s, kind, {})
        base_chk = _total(check, s, kind, {})
        best = None
        for val in t.grid:
            if val == cur:
                continue
            if kind in ("plan", "scalp") and not _plan_ok(s, t.key, val):
                continue                                   # never learn a plan that contradicts itself
            tot_fit, n_fit = _total(fit, s, kind, {t.key: val})
            tot_chk, n_chk = _total(check, s, kind, {t.key: val})
            if n_fit < max(5, min_samples // 3) or n_chk < 3:
                continue
            # selection settings change WHICH coins are called: compare total profit.
            # plan settings change HOW each call is traded: compare profit per trade.
            if kind in ("select", "brain", "scalpselect", "radar"):
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
        # move to the NEXT grid value toward the best one (never jump across the range in one go,
        # and if the current value sits between grid points, stop at the nearest one on that side)
        grid = sorted(t.grid)
        if best[0] > cur:
            new = min((g for g in grid if g > cur), default=None)
        else:
            new = max((g for g in grid if g < cur), default=None)
        if new is None or new == cur or (kind in ("plan", "scalp") and not _plan_ok(s, t.key, new)):
            continue
        if isinstance(cur, int) and not isinstance(cur, bool):
            new = int(new)
        setattr(s, t.key, new)
        detail = {"label": t.label, "best_seen": best[0], "fit_gain": round(best[1], 3), "check_gain": round(best[2], 3),
                  "samples": best[4], "evaluated_coins": n_all}
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
    model = B.latest(db)
    rows = _rows(db, days, now, bool(model and model.trusted))
    std = _total(rows, s, "select", {})
    scp = _total(rows, s, "scalp", {})
    peaks = [r["peak_x"] for r in rows if r.get("peak_x")]
    return {"evaluated": len(rows),
            "pending": (db.fetchone("SELECT COUNT(*) n FROM shadow_calls WHERE evaluated=0") or {}).get("n", 0),
            "standard_trades": std[1], "standard_avg": round(_per_trade(*std), 3) if std[1] else None,
            "scalp_trades": scp[1], "scalp_avg": round(_per_trade(*scp), 3) if scp[1] else None,
            "median_peak": round(statistics.median(peaks), 2) if peaks else None,
            "learned": {r["key"]: r["value"] for r in db.fetchall("SELECT key, value FROM learned_settings")},
            "brain": B.describe(model),
            "brain_signals": model.importance(6) if model else [],
            "radar_observations": (db.fetchone("SELECT COUNT(*) n FROM radar_obs WHERE evaluated=1") or {}).get("n", 0),
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
