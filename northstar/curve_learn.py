"""The curve learner: the runner-call rules learn from what young pump.fun coins really did (v3.3.0).

The older learners (learning.py, the two brains) learn from coins that already have a DEX pool. The
runner calls happen earlier, on the bonding curve, and nothing learned from that stage. The curve
watch (curve_watch.py) now records, for every coin in the early band:

  * the runner check's verdict: its score, and whether every safety check passed (`runner_safe`);
  * what the coin did over the next 4 hours: peak, low, the worst drop before the peak, last price.

Every 30 minutes this module replays the early trade plan on those outcomes for each possible score
floor and moves `runner_min_score` one step toward the floor that made the most per trade.

Rules (so one lucky coin can't swing it):
  * only coins whose safety checks all passed count (the floor never decides safety);
  * a floor needs at least `curve_learn_min_samples` coins;
  * walk-forward: the new floor must beat the current one on the older coins AND on the most recent
    third it was not chosen on;
  * one step per run (5 points), always inside `curve_learn_min_floor`-`curve_learn_max_floor`.

The plan replay is an estimate from four numbers per coin (peak, worst drop before the peak, low,
last price), not a minute path: a stop hit before any take-profit loses (stop - 1); otherwise TP1
sells its share at TP1 and the rest exits at the trailing level below the peak (or break-even);
a coin that never reached TP1 exits at its last price, floored at the stop. Fees: 3% a round trip.
"""
from __future__ import annotations

import json
import logging
import statistics
import time
from typing import Optional

from . import curve_watch as CW

log = logging.getLogger("northstar.curve_learn")

FLOORS = (30.0, 35.0, 40.0, 45.0, 50.0, 55.0, 60.0, 65.0, 70.0, 75.0)
FEES = 0.03
META_KEY = "learned:runner_min_score"


def trade_result(row: dict, s) -> Optional[float]:
    """Estimated return of the early plan on one coin (0.25 = +25%), or None without an outcome."""
    entry = row.get("entry_mc")
    if not entry or not row.get("peak_mc"):
        return None
    tp1 = float(getattr(s, "early_tp1", 2.0))
    sell = float(getattr(s, "early_tp1_sell_pct", 40)) / 100
    stop = float(getattr(s, "early_stop", 0.6))
    trail = float(getattr(s, "early_trail_pct", 40.0)) / 100
    peak = row["peak_mc"] / entry
    first_low = (row.get("low_before_peak") or entry) / entry
    last = (row.get("last_mc") or entry) / entry
    if first_low <= stop:
        r = stop - 1
    elif peak >= tp1:
        rest = max(1.0, peak * (1 - trail))           # stop moved to break-even after TP1
        r = sell * (tp1 - 1) + (1 - sell) * (rest - 1)
    else:
        r = max(stop, min(last, peak)) - 1
    return r - FEES


def _rows(db, days: float, now: float) -> list[dict]:
    cols = {r["name"] for r in db.fetchall("PRAGMA table_info(curve_watch)")}
    if "runner_safe" not in cols:
        return []
    return db.fetchall("SELECT * FROM curve_watch WHERE done = 1 AND COALESCE(excluded, 0) = 0 AND runner_safe = 1 "
                       "AND runner_score IS NOT NULL AND entered_at > ? ORDER BY entered_at", (now - days * 86400,))


def table(db, s, days: float = 7.0, now: Optional[float] = None) -> dict:
    """Per floor: coins, mean estimated result, share that hit 2x, share that hit 0.3x."""
    now = now or time.time()
    rows = [dict(r) for r in _rows(db, days, now)]
    out = []
    for f in FLOORS:
        sub = [r for r in rows if r["runner_score"] >= f]
        res = [x for x in (trade_result(r, s) for r in sub) if x is not None]
        out.append({"floor": f, "n": len(res),
                    "mean": round(statistics.fmean(res), 4) if res else None,
                    "hit_2x": round(100 * sum(r["peak_mc"] / r["entry_mc"] >= 2 for r in sub) / len(sub), 1) if sub else None,
                    "fell_0_3x": round(100 * sum((r["low_mc"] or r["entry_mc"]) / r["entry_mc"] <= 0.3 for r in sub)
                                       / len(sub), 1) if sub else None})
    return {"days": days, "safe_coins": len(rows), "floors": out}


def _mean(rows: list[dict], floor: float, s) -> tuple[Optional[float], int]:
    res = [x for x in (trade_result(r, s) for r in rows if r["runner_score"] >= floor) if x is not None]
    return (statistics.fmean(res) if res else None), len(res)


def learn(db, s, now: Optional[float] = None) -> Optional[dict]:
    """One learning step. Returns the change made ({"from", "to", ...}) or None."""
    now = now or time.time()
    if not getattr(s, "curve_learn_enabled", True):
        return None
    rows = [dict(r) for r in _rows(db, float(getattr(s, "curve_learn_days", 7.0)), now)]
    need = int(getattr(s, "curve_learn_min_samples", 25))
    if len(rows) < need:
        return None
    lo = float(getattr(s, "curve_learn_min_floor", 35.0))
    hi = float(getattr(s, "curve_learn_max_floor", 70.0))
    cur = float(getattr(s, "runner_min_score", 45.0))
    cut = len(rows) - max(1, len(rows) // 3)
    old, recent = rows[:cut], rows[cut:]
    best, best_mean = None, None
    for f in FLOORS:
        if not (lo <= f <= hi) or f == cur:
            continue
        m_all, n_all = _mean(rows, f, s)
        if m_all is None or n_all < need:
            continue
        m_old, _ = _mean(old, f, s)
        m_new, n_new = _mean(recent, f, s)
        c_old, _ = _mean(old, cur, s)
        c_new, _ = _mean(recent, cur, s)
        if None in (m_old, m_new) or n_new < 3:
            continue
        # must beat the current floor on the older coins AND on the recent ones
        if c_old is not None and m_old <= c_old:
            continue
        if c_new is not None and m_new <= c_new:
            continue
        if best_mean is None or m_all > best_mean:
            best, best_mean = f, m_all
    if best is None:
        return None
    step = 5.0 if best > cur else -5.0
    new = min(hi, max(lo, cur + step))
    if new == cur:
        return None
    cur_mean, cur_n = _mean(rows, cur, s)
    new_mean, new_n = _mean(rows, new, s)
    change = {"key": "runner_min_score", "from": cur, "to": new, "target": best, "coins": len(rows),
              "mean_from": None if cur_mean is None else round(cur_mean, 4), "n_from": cur_n,
              "mean_to": None if new_mean is None else round(new_mean, 4), "n_to": new_n, "at": now}
    s.runner_min_score = new
    db.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               (META_KEY, json.dumps(change)))
    log.info("curve learner: runner score floor %.0f -> %.0f (best floor on %d safe coins: %.0f; est. per trade "
             "%s -> %s)", cur, new, len(rows), best, _pct(change["mean_from"]), _pct(change["mean_to"]))
    return change


def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def apply_saved(db, s) -> Optional[float]:
    """At start: put back the floor the learner chose last time (if learning is on)."""
    if not getattr(s, "curve_learn_enabled", True):
        return None
    try:
        row = db.fetchone("SELECT value FROM meta WHERE key = ?", (META_KEY,))
        if not row:
            return None
        v = float(json.loads(row["value"])["to"])
    except Exception:  # noqa: BLE001
        return None
    lo = float(getattr(s, "curve_learn_min_floor", 35.0))
    hi = float(getattr(s, "curve_learn_max_floor", 70.0))
    v = min(hi, max(lo, v))
    s.runner_min_score = v
    return v


def format_table(t: dict) -> str:
    lines = [f"Runner score floors on {t['safe_coins']} curve coins that passed every safety check "
             f"(last {t['days']:g} days)", "",
             f"{'floor':>6} {'coins':>6} {'est/trade':>10} {'2x':>6} {'0.3x':>6}"]
    for f in t["floors"]:
        lines.append(f"{f['floor']:>6.0f} {f['n']:>6} {_pct(f['mean']):>10} "
                     f"{('' if f['hit_2x'] is None else str(f['hit_2x']) + '%'):>6} "
                     f"{('' if f['fell_0_3x'] is None else str(f['fell_0_3x']) + '%'):>6}")
    lines += ["", "est/trade: the early plan replayed on each coin's peak / first drop / last price, 3% fees."]
    return "\n".join(lines)


def ensure(db) -> None:
    CW.init_schema(db)
