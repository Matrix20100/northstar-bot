"""Training-data cleanup: the learners train only on real, fully observed outcomes (v3.3.0).

Runs a minute after start and then once a day (`python main.py cleanup` runs it by hand; add
`--dry-run` to only count). Each rule removes or sets aside rows that would teach the learners
something false:

training_examples (both brains)
  * no label at all (neither rug nor runner known) after 2 days: the outcome will never arrive
  * mostly unknown numbers: 60%+ of the coin's features were never measured (research failed or
    the data source was down), so the example is mostly the brain's own defaults
  * repeats: the same coin recorded again within 6 hours. The first decision is kept; repeats made
    one coin count several times (a coin researched 40 times an hour looked like 40 coins)

curve_watch (the curve learner and `python main.py curve`)
  * never really followed: the coin left the radar's view within 10 minutes of entering the band,
    so its "peak" and "low" are just its entry price. Those rows are flagged `excluded` (kept, but
    left out of every count); they used to look like coins where nothing happened
  * impossible numbers: no entry market cap, or a peak below the entry

Nothing else is touched: research history, calls, paper trades and settings stay as they are.
"""
from __future__ import annotations

import logging
import math
import struct
import time
from typing import Optional

from . import curve_watch as CW
from . import training_store as TS

log = logging.getLogger("northstar.cleanup")

UNKNOWN_SHARE = 0.6
REPEAT_SECONDS = 6 * 3600
UNLABELLED_DAYS = 2.0
FOLLOW_MIN_SECONDS = 600


def _unknown_share(blob: bytes) -> float:
    n = len(blob) // 4
    if not n:
        return 1.0
    vals = struct.unpack(f"<{n}f", blob)
    return sum(1 for x in vals if math.isnan(x)) / n


def run(db, now: Optional[float] = None, dry_run: bool = False) -> dict:
    """Clean the training data. Returns counts per rule. Never raises."""
    now = now or time.time()
    out = {"unlabelled": 0, "mostly_unknown": 0, "repeats": 0, "curve_not_followed": 0, "curve_impossible": 0,
           "examples_left": 0}
    try:
        TS.init_schema(db)
        CW.init_schema(db)
        drop: dict[str, str] = {}
        for r in db.fetchall("SELECT key FROM training_examples WHERE rug IS NULL AND runner IS NULL AND ts < ?",
                             (now - UNLABELLED_DAYS * 86400,)):
            drop[r["key"]] = "unlabelled"
        out["unlabelled"] = len(drop)
        last_seen: dict[str, float] = {}
        for r in db.fetchall("SELECT key, mint, ts, feats FROM training_examples ORDER BY ts"):
            if r["key"] in drop:
                continue
            if _unknown_share(r["feats"]) >= UNKNOWN_SHARE:
                drop[r["key"]] = "mostly unknown"
                out["mostly_unknown"] += 1
                continue
            prev = last_seen.get(r["mint"])
            if prev is not None and r["ts"] - prev < REPEAT_SECONDS:
                drop[r["key"]] = "repeat"
                out["repeats"] += 1
                continue
            last_seen[r["mint"]] = r["ts"]
        out["curve_not_followed"] = db.fetchone(
            "SELECT COUNT(*) AS n FROM curve_watch WHERE done = 1 AND COALESCE(excluded, 0) = 0 "
            "AND COALESCE(last_ts, entered_at) - entered_at < ?", (FOLLOW_MIN_SECONDS,))["n"]
        out["curve_impossible"] = db.fetchone(
            "SELECT COUNT(*) AS n FROM curve_watch WHERE COALESCE(excluded, 0) = 0 AND "
            "(entry_mc IS NULL OR entry_mc <= 0 OR (peak_mc IS NOT NULL AND peak_mc < entry_mc * 0.999))")["n"]
        if not dry_run:
            keys = sorted(drop)
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                db.execute(f"DELETE FROM training_examples WHERE key IN ({','.join('?' * len(chunk))})", tuple(chunk))
                for k in chunk:          # remembered, so harvest never copies it back from the raw tables
                    db.execute("INSERT OR IGNORE INTO training_removed(key, ts, why) VALUES (?,?,?)", (k, now, drop[k]))
            db.execute("DELETE FROM training_removed WHERE ts < ?", (now - 90 * 86400,))
            db.execute("UPDATE curve_watch SET excluded = 1 WHERE done = 1 AND COALESCE(excluded, 0) = 0 "
                       "AND COALESCE(last_ts, entered_at) - entered_at < ?", (FOLLOW_MIN_SECONDS,))
            db.execute("UPDATE curve_watch SET excluded = 1 WHERE COALESCE(excluded, 0) = 0 AND "
                       "(entry_mc IS NULL OR entry_mc <= 0 OR (peak_mc IS NOT NULL AND peak_mc < entry_mc * 0.999))")
        out["examples_left"] = db.fetchone("SELECT COUNT(*) AS n FROM training_examples")["n"] - (
            len(drop) if dry_run else 0)
    except Exception:  # noqa: BLE001 - cleanup must never break the bot
        log.warning("training cleanup failed", exc_info=True)
        out["error"] = True
    return out


def describe(c: dict, dry_run: bool = False) -> str:
    verb = "would remove" if dry_run else "removed"
    return (f"training cleanup: {verb} {c['unlabelled']} unlabelled, {c['mostly_unknown']} mostly-unknown and "
            f"{c['repeats']} repeated example(s) ({c['examples_left']} left); set aside {c['curve_not_followed']} "
            f"curve coin(s) never really followed and {c['curve_impossible']} with impossible numbers")
