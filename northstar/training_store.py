"""Compact, long-lived training memory for the two learning brains (v2.6.1).

The brains learn from two raw sources:
  * shadow_calls: a minute-level 4-hour price path per evaluated coin;
  * decision_tracking: the 24-hour high/low of each decision, plus the brain's numbers as JSON.
Those are bulky and grow without bound: decision_tracking alone added about 3 MB a day of JSON
numbers on the 22 Oct database and was never pruned.

This module keeps what the brains actually need, and nothing else, in one small table:

  training_examples(key, mint, ts, src, schema, feats, peak_x, low_x, rug, runner)

  feats    the brain's numbers packed as float32 in a fixed feature order, NaN = unknown
           (about 280 bytes a coin instead of about 1.5 KB of JSON)
  schema   which feature order the row uses (feature lists are kept in `meta`, so a new brain
           signal never invalidates old rows)
  rug      1 when the low reached 0.3x of the decision price; 0 when a complete window never did
  runner   1 for a clean runner (2x before 0.6x, no 0.3x after); 0 when it was not; NULL when
           the order of the moves is unknown

Retention keeps memory where it matters. Every rug and every runner is kept up to
`training_max_examples`. Only plain "nothing happened" coins are thinned once the store is full,
oldest first. So the rare examples the brains learn most from survive long after the raw tables
are pruned.

After harvesting, the raw JSON numbers on finished decision_tracking rows are cleared, and
finished rows older than `tracking_retention_days` are deleted.
"""
from __future__ import annotations

import json
import logging
import math
import struct
import time
from typing import Optional

from . import brain as B

log = logging.getLogger("northstar.training_store")

RUG_X = 0.3

SCHEMA = """
CREATE TABLE IF NOT EXISTS training_examples (
    key TEXT PRIMARY KEY,          -- "path:<shadow id>" or "track:<snapshot id>"
    rid INTEGER NOT NULL,          -- shadow id (positive) or -snapshot id (negative), as the brains use
    mint TEXT NOT NULL,
    ts REAL NOT NULL,
    src TEXT NOT NULL,             -- path / tracking
    schema INTEGER NOT NULL,
    feats BLOB NOT NULL,
    peak_x REAL,
    low_x REAL,
    rug INTEGER,
    runner INTEGER
);
CREATE INDEX IF NOT EXISTS idx_training_ts ON training_examples(ts);
-- v3.3.0: examples the cleanup removed (cleanup.py), so harvest never copies them back in
CREATE TABLE IF NOT EXISTS training_removed (
    key TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    why TEXT
);
"""


def init_schema(db) -> None:
    with db._lock:
        db._conn.executescript(SCHEMA)


# ----------------------------------------------------------------------------- packing
def _schema_id(db, names: list[str]) -> int:
    """The id of this exact feature order, registering it in `meta` the first time."""
    blob = json.dumps(names)
    for r in db.fetchall("SELECT key, value FROM meta WHERE key LIKE 'training_schema:%'"):
        if r["value"] == blob:
            return int(r["key"].split(":")[1])
    ids = [int(r["key"].split(":")[1]) for r in db.fetchall("SELECT key FROM meta WHERE key LIKE 'training_schema:%'")]
    new = (max(ids) + 1) if ids else 1
    db.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (f"training_schema:{new}", blob))
    return new


def _schemas(db) -> dict[int, list[str]]:
    out = {}
    for r in db.fetchall("SELECT key, value FROM meta WHERE key LIKE 'training_schema:%'"):
        try:
            out[int(r["key"].split(":")[1])] = json.loads(r["value"])
        except (ValueError, TypeError):
            continue
    return out


def pack(v: dict, names: list[str]) -> bytes:
    vals = []
    for n in names:
        x = v.get(n)
        try:
            vals.append(float(x) if x is not None and math.isfinite(float(x)) else float("nan"))
        except (TypeError, ValueError):
            vals.append(float("nan"))
    return struct.pack(f"<{len(vals)}f", *vals)


def unpack(blob: bytes, names: list[str]) -> dict:
    vals = struct.unpack(f"<{len(blob) // 4}f", blob)
    return {n: (None if math.isnan(x) else round(x, 6)) for n, x in zip(names, vals)}


# ----------------------------------------------------------------------------- labels
def path_labels(path: list) -> Optional[tuple[float, float, Optional[int], Optional[int]]]:
    """(peak_x, low_x, rug, runner) from a minute path; None when unusable."""
    if not path:
        return None
    try:
        peak = max(float(p[1]) for p in path)
        low = min(float(p[2]) for p in path)
        minutes = float(path[-1][0])
    except (TypeError, ValueError, IndexError):
        return None
    rug = 1 if low <= RUG_X else (0 if minutes >= 120 else None)
    runner = B.clean_outcome(path)
    return peak, low, rug, runner


def tracking_labels(r: dict) -> Optional[tuple[float, Optional[float], Optional[int], Optional[int]]]:
    entry = r.get("entry_price")
    if not entry or entry <= 0 or r.get("max_price") is None:
        return None
    peak = r["max_price"] / entry
    low = (r["min_price"] / entry) if r.get("min_price") is not None else None
    complete = bool(r.get("done")) and (r.get("samples") or 0) >= B.TRACK_MIN_SAMPLES
    rug = 1 if (low is not None and low <= RUG_X) else (0 if complete else None)
    runner = B.binary(B.tracking_label(r))
    return peak, low, rug, runner


# ----------------------------------------------------------------------------- harvest
def harvest(db, now: Optional[float] = None, limit: int = 5000) -> dict:
    """Copy newly finished examples into the store. Idempotent. Never raises."""
    now = now or time.time()
    out = {"paths": 0, "tracking": 0}
    try:
        init_schema(db)
        names = list(B.ALL_FEATURES)
        sid = _schema_id(db, names)
        have = {r["key"] for r in db.fetchall("SELECT key FROM training_examples WHERE ts > ?", (now - 60 * 86400,))}
        have |= {r["key"] for r in db.fetchall("SELECT key FROM training_removed")}
        try:
            shadows = db.fetchall("SELECT id, mint, ts, vector, path FROM shadow_calls WHERE evaluated=1 "
                                  "AND vector IS NOT NULL AND vector != '{}' AND path IS NOT NULL "
                                  "ORDER BY ts DESC LIMIT ?", (limit,))
        except Exception:  # noqa: BLE001 - learning tables not created yet
            shadows = []
        path_times: dict[str, list] = {}
        for r in shadows:
            path_times.setdefault(r["mint"], []).append(r["ts"])
            key = f"path:{r['id']}"
            if key in have:
                continue
            try:
                v, path = json.loads(r["vector"]), json.loads(r["path"])
            except (TypeError, ValueError):
                continue
            lab = path_labels(path)
            if not v or lab is None or (lab[2] is None and lab[3] is None):
                continue
            db.execute("INSERT OR IGNORE INTO training_examples(key, rid, mint, ts, src, schema, feats, peak_x, low_x, "
                       "rug, runner) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (key, int(r["id"]), r["mint"], r["ts"], "path", sid, pack(v, names), *lab))
            out["paths"] += 1
        try:
            tracked = db.fetchall(
                "SELECT snapshot_id, mint, decided_at, entry_price, max_price, min_price, samples, done, brain_vector "
                "FROM decision_tracking WHERE done = 1 AND entry_price > 0 AND max_price IS NOT NULL "
                "AND brain_vector IS NOT NULL AND brain_vector != '{}' ORDER BY decided_at DESC LIMIT ?", (limit,))
        except Exception:  # noqa: BLE001
            tracked = []
        for r in tracked:
            key = f"track:{r['snapshot_id']}"
            if key in have:
                continue
            if any(abs(t - r["decided_at"]) < 1800 for t in path_times.get(r["mint"], ())):
                continue                                   # the minute path is the better example
            try:
                v = json.loads(r["brain_vector"]) or None
            except (TypeError, ValueError):
                v = None
            lab = tracking_labels(r)
            if not v or lab is None or (lab[2] is None and lab[3] is None):
                continue
            db.execute("INSERT OR IGNORE INTO training_examples(key, rid, mint, ts, src, schema, feats, peak_x, low_x, "
                       "rug, runner) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (key, -int(r["snapshot_id"]), r["mint"], r["decided_at"], "tracking", sid, pack(v, names), *lab))
            out["tracking"] += 1
    except Exception:  # noqa: BLE001 - learning must never break the bot
        log.warning("training store harvest failed", exc_info=True)
    return out


def compact(db, s, now: Optional[float] = None) -> dict:
    """Bound the store and shrink the raw tables it replaces. Never raises."""
    now = now or time.time()
    out = {"plain_thinned": 0, "oldest_dropped": 0, "vectors_cleared": 0, "tracking_deleted": 0}
    try:
        init_schema(db)
        cap = int(getattr(s, "training_max_examples", 30000) or 30000)
        n = db.fetchone("SELECT COUNT(*) AS n FROM training_examples")["n"]
        if n > cap:
            # plain coins (neither rug nor runner) go first, oldest first
            with db._lock:
                out["plain_thinned"] = db._conn.execute(
                    "DELETE FROM training_examples WHERE key IN (SELECT key FROM training_examples "
                    "WHERE COALESCE(rug, 0) = 0 AND COALESCE(runner, 0) = 0 ORDER BY ts LIMIT ?)", (n - cap,)).rowcount
            n -= out["plain_thinned"]
        if n > cap:
            with db._lock:
                out["oldest_dropped"] = db._conn.execute(
                    "DELETE FROM training_examples WHERE key IN (SELECT key FROM training_examples ORDER BY ts LIMIT ?)",
                    (n - cap,)).rowcount
        # raw numbers already in the store are not needed on finished tracking rows
        stored = "SELECT CAST(substr(key, 7) AS INTEGER) FROM training_examples WHERE src = 'tracking'"
        with db._lock:
            out["vectors_cleared"] = db._conn.execute(
                f"UPDATE decision_tracking SET brain_vector = '{{}}' WHERE done = 1 AND brain_vector != '{{}}' "
                f"AND decided_at < ? AND snapshot_id IN ({stored})", (now - 2 * 86400,)).rowcount
        days = float(getattr(s, "tracking_retention_days", 21.0) or 0)
        if days > 0:
            with db._lock:
                out["tracking_deleted"] = db._conn.execute(
                    "DELETE FROM decision_tracking WHERE done = 1 AND decided_at < ? "
                    "AND mint NOT IN (SELECT mint FROM alerts)",       # calls keep their tracking
                    (now - max(days, 7.0) * 86400,)).rowcount
    except Exception:  # noqa: BLE001
        log.warning("training store compaction failed", exc_info=True)
    return out


# ----------------------------------------------------------------------------- reading
def examples(db, label: str, limit: int = B.MAX_ROWS) -> list[dict]:
    """The newest `limit` examples with a known `label` ("rug" or "runner"), oldest first, as the
    brains expect them: {"id", "ts", "v", "y", "src"}."""
    if label not in ("rug", "runner"):
        raise ValueError(label)
    try:
        init_schema(db)
        schemas = _schemas(db)
        rows = db.fetchall(f"SELECT rid, ts, src, schema, feats, {label} AS y FROM training_examples "
                           f"WHERE {label} IS NOT NULL ORDER BY ts DESC LIMIT ?", (limit,))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in reversed(rows):
        names = schemas.get(r["schema"])
        if not names:
            continue
        out.append({"id": r["rid"], "ts": r["ts"], "v": unpack(r["feats"], names), "y": int(r["y"]),
                    "src": r["src"]})
    return out


def stats(db) -> dict:
    try:
        init_schema(db)
        r = db.fetchone("SELECT COUNT(*) AS n, SUM(rug = 1) AS rugs, SUM(runner = 1) AS runners, "
                        "SUM(LENGTH(feats)) AS bytes, MIN(ts) AS since FROM training_examples")
        return {"examples": r["n"] or 0, "rugs": r["rugs"] or 0, "runners": r["runners"] or 0,
                "mb": round((r["bytes"] or 0) / 1e6, 2), "since": r["since"]}
    except Exception:  # noqa: BLE001
        return {"examples": 0, "rugs": 0, "runners": 0, "mb": 0.0, "since": None}
