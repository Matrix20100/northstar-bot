"""The learned rug model (v2.6.0): odds that a coin falls to 0.3x of its price at research time.

Why: on the 22 Oct database the hand-written 0-100 score did not separate outcomes (coins scoring
65+ were rugged or pumped-then-rugged 43% of the time; called coins 58%), and the brain's runner
odds did not either on coins with full price paths. Whether a coin RUGS, however, is predictable:
a logistic model on the same research numbers, validated walk-forward (each fold predicted by a
model trained only on earlier coins), reached AUC 0.91 overall and 0.83 on fully tracked coins.

  out-of-fold rug odds   coins   rugged or pumped-then-rugged   clean 2x   any 2x
  under 0.2                54      9%                            11%        13%
  0.2 - 0.4                42     26%                            10%        17%
  0.4 - 0.6                54     33%                            15%        26%
  0.6 and over            217     54%                             8%        30%
(367 fully tracked coins, 22 Oct.) Low odds cut rugs; they do NOT find runners: whether a coin
runs was not predictable from these numbers (walk-forward AUC 0.49).

Data: the same examples the brain learns from (brain.py): minute paths of evaluated shadows, plus
24-hour high/low tracking of decisions. Label: 1 when the low reached `RUG_X` (0.3x) of the price
at the decision, 0 when a complete window never did. The model is only used once it is TRUSTED:
enough examples and walk-forward AUC (overall AND on the most recent coins) at or above
`rug_model_min_auc`.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from . import brain as B

log = logging.getLogger("northstar.rugmodel")

RUG_X = 0.3

SCHEMA = """
CREATE TABLE IF NOT EXISTS rug_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    n INTEGER NOT NULL,
    positives INTEGER NOT NULL,
    auc REAL,
    recent_auc REAL,
    trusted INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL
);
"""


def init_schema(db) -> None:
    with db._lock:
        db._conn.executescript(SCHEMA)


def _path_low(path: list) -> Optional[tuple[float, float]]:
    """(lowest low, minutes covered) of a stored minute path [[minute, high_x, low_x], ...]."""
    if not path:
        return None
    try:
        return min(float(p[2]) for p in path), float(path[-1][0])
    except (TypeError, ValueError, IndexError):
        return None


def training_rows(db, days: float, now: float, rug_x: float = RUG_X, min_path_minutes: float = 120.0) -> list[dict]:
    """v2.6.1: from the compact training store when it has examples (it keeps every rug long after
    the raw tables are pruned); the raw tables otherwise."""
    if rug_x == RUG_X:
        from . import training_store as TS
        stored = TS.examples(db, "rug")
        if stored:
            return stored
    rows, shadow_times = [], {}
    try:
        shadows = db.fetchall("SELECT id, mint, ts, vector, path FROM shadow_calls WHERE evaluated=1 "
                              "AND vector IS NOT NULL AND vector != '{}' AND path IS NOT NULL AND ts > ? ORDER BY ts",
                              (now - days * 86400,))
    except Exception:  # noqa: BLE001 - learning tables not created yet
        shadows = []
    for r in shadows:
        try:
            v, path = json.loads(r["vector"]), json.loads(r["path"])
        except (TypeError, ValueError):
            continue
        low = _path_low(path)
        if not v or low is None:
            continue
        lo, minutes = low
        if lo > rug_x and minutes < min_path_minutes:
            continue                                  # too short to say it did NOT rug
        rows.append({"id": r["id"], "ts": r["ts"], "v": v, "y": int(lo <= rug_x), "src": "path"})
        shadow_times.setdefault(r["mint"], []).append(r["ts"])
    try:
        found = db.fetchall(
            "SELECT snapshot_id, mint, decided_at, entry_price, min_price, samples, done, brain_vector "
            "FROM decision_tracking WHERE entry_price > 0 AND min_price IS NOT NULL AND brain_vector IS NOT NULL "
            "AND decided_at > ? ORDER BY decided_at", (now - days * 86400,))
    except Exception:  # noqa: BLE001 - older databases without the table/column
        found = []
    for r in found:
        lo = r["min_price"] / r["entry_price"]
        complete = bool(r["done"]) and (r["samples"] or 0) >= B.TRACK_MIN_SAMPLES
        if lo > rug_x and not complete:
            continue                                  # not followed long enough to call it safe
        if any(abs(t - r["decided_at"]) < 1800 for t in shadow_times.get(r["mint"], ())):
            continue                                  # the minute path is the better example
        try:
            v = json.loads(r["brain_vector"]) or None
        except (TypeError, ValueError):
            v = None
        if not v:
            continue
        rows.append({"id": -int(r["snapshot_id"]), "ts": r["decided_at"], "v": v, "y": int(lo <= rug_x),
                     "src": "tracking"})
    rows.sort(key=lambda r: r["ts"])
    return rows[-B.MAX_ROWS:]


def train(db, s, now: Optional[float] = None) -> Optional[B.Model]:
    """Walk-forward validate, fit on everything, store. Never raises."""
    now = now or time.time()
    try:
        init_schema(db)
        rows = training_rows(db, max(getattr(s, "learning_days", 14), getattr(s, "brain_days", 21)), now)
        pos = sum(r["y"] for r in rows)
        if len(rows) < 100 or pos < 15:
            log.info("rug model: %d examples (%d rugs) - need at least 100 and 15 rugs", len(rows), pos)
            return None
        feats = list(B.FEATURES)
        model_auc, recent, _ = B.walk_forward(rows, feats)
        final = B.Model.fit([r["v"] for r in rows], [r["y"] for r in rows], features=feats)
        final.auc, final.recent_auc, final.ts = model_auc, recent, now
        bar = float(getattr(s, "rug_model_min_auc", 0.75))
        final.trusted = bool(len(rows) >= int(getattr(s, "rug_model_min_samples", 300)) and pos >= 30
                             and model_auc is not None and recent is not None
                             and model_auc >= bar and recent >= bar)
        db.execute("INSERT INTO rug_models(ts, n, positives, auc, recent_auc, trusted, model) VALUES (?,?,?,?,?,?,?)",
                   (now, final.n, final.positives, model_auc, recent, 1 if final.trusted else 0, final.to_json()))
        db.execute("DELETE FROM rug_models WHERE id NOT IN (SELECT id FROM rug_models ORDER BY ts DESC LIMIT 10)")
        log.info("rug brain: trained on %d coins (%d rugs) - walk-forward AUC %s, recent %s, %s", len(rows), pos,
                 B._fmt(model_auc), B._fmt(recent), "TRUSTED" if final.trusted else "not trusted yet")
        return final
    except Exception:  # noqa: BLE001 - learning must never break the bot
        log.warning("rug model training failed", exc_info=True)
        return None


def latest(db) -> Optional[B.Model]:
    try:
        if hasattr(db, "_lock"):          # the dashboard reads through a read-only adapter: no CREATE there
            init_schema(db)
        row = db.fetchone("SELECT * FROM rug_models ORDER BY ts DESC LIMIT 1")
        return B.Model.from_row(row) if row else None
    except Exception:  # noqa: BLE001
        return None


def score(m: Optional[B.Model], report: dict) -> Optional[dict]:
    """{"p", "trusted", "auc", "recent_auc", "n", "ups", "downs"} for one research report; None when
    there is no model or the report has nothing to score (pre-migration curve coins)."""
    if m is None or report.get("lifecycle") == "PRE_MIGRATION_BONDING_CURVE":
        return None
    try:
        v = B.vector(report)
        p = m.predict(v)
        raises, lowers = m.reasons(v)          # positive coefficient = raises the rug odds
    except Exception:  # noqa: BLE001
        return None
    return {"p": round(p, 4), "trusted": bool(m.trusted), "auc": m.auc, "recent_auc": m.recent_auc, "n": m.n,
            "raises": raises, "lowers": lowers}


def describe(m: Optional[B.Model]) -> str:
    if m is None:
        return "rug model: not trained yet (needs 100 tracked coins incl. 15 rugs)"
    return (f"rug model: {'TRUSTED' if m.trusted else 'not trusted yet'} · {m.n} coins ({m.positives} rugged) · "
            f"walk-forward AUC {B._fmt(m.auc)} (recent {B._fmt(m.recent_auc)})")
