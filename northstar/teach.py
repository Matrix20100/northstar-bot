"""Teach the learners with real coins (v4.1.0).

Operator: "I want to be able to manually teach the AI with real coins."

On a coin's research drawer (dashboard) there are three buttons:
  👍 runner    this coin ran: learn what it looked like
  👎 rug       this coin rugged / was a scam
  😐 nothing   it went nowhere
and "clear" to take the lesson back.

What a lesson does:
  * the label is stored in `operator_labels` (one per coin);
  * the coin's numbers from its latest research (the same numbers the brains read) become a training
    example `teach:<mint>`, so the brains learn from it even if the bot never measured its outcome;
  * the label OVERRIDES what the bot measured for that coin in every training example
    (training_store.examples), so a coin you know rugged is never learned as a winner;
  * both brains retrain every hour (learning interval), so a lesson counts from the next training.

Nothing is posted and no live decision changes at the moment of teaching.
"""
from __future__ import annotations

import json
import logging
import time
import zlib
from typing import Optional

from . import brain as B
from . import training_store as TS

log = logging.getLogger("northstar.teach")

SCHEMA = """
CREATE TABLE IF NOT EXISTS operator_labels (
    mint TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    ts REAL NOT NULL,
    note TEXT
);
"""
LABELS = {"runner": (0, 1), "rug": (1, 0), "nothing": (0, 0)}       # label -> (rug, runner)


def init_schema(db) -> None:
    with db._lock:
        db._conn.executescript(SCHEMA)


def labels(db) -> dict[str, str]:
    try:
        init_schema(db)
        return {r["mint"]: r["label"] for r in db.fetchall("SELECT mint, label FROM operator_labels")}
    except Exception:  # noqa: BLE001
        return {}


def _report(db, mint: str) -> Optional[dict]:
    row = db.fetchone("SELECT report FROM research_snapshots WHERE mint = ? ORDER BY id DESC LIMIT 1", (mint,))
    if row is None:
        try:
            row = db.fetchone("SELECT report FROM early_calls WHERE mint = ?", (mint,))
        except Exception:  # noqa: BLE001
            row = None
    try:
        return json.loads(row["report"]) if row else None
    except (TypeError, ValueError):
        return None


def teach(db, mint: str, label: str, note: str = "", now: Optional[float] = None) -> dict:
    """Store a lesson. Returns {"mint", "label", "example": bool}. Raises ValueError."""
    now = now or time.time()
    label = label.strip().lower()
    if not mint or len(mint) < 32:
        raise ValueError("a coin's mint address is needed")
    init_schema(db)
    TS.init_schema(db)
    key = f"teach:{mint}"
    if label == "clear":
        db.execute("DELETE FROM operator_labels WHERE mint = ?", (mint,))
        db.execute("DELETE FROM training_examples WHERE key = ?", (key,))
        log.info("teach: lesson for %s cleared", mint)
        return {"mint": mint, "label": None, "example": False}
    if label not in LABELS:
        raise ValueError("label must be runner, rug, nothing or clear")
    db.execute("INSERT INTO operator_labels(mint, label, ts, note) VALUES (?,?,?,?) ON CONFLICT(mint) DO UPDATE "
               "SET label = excluded.label, ts = excluded.ts, note = excluded.note", (mint, label, now, note or None))
    report = _report(db, mint)
    made = False
    if report:
        try:
            v = B.vector(report)
        except Exception:  # noqa: BLE001 - a report too thin for the brain's numbers
            v = None
        if v:
            names = list(B.ALL_FEATURES)
            rug, runner = LABELS[label]
            rid = -(3_000_000_000 + zlib.crc32(mint.encode()))      # never collides with real ids
            db.execute("INSERT INTO training_examples(key, rid, mint, ts, src, schema, feats, peak_x, low_x, rug, runner) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET rug = excluded.rug, "
                       "runner = excluded.runner, ts = excluded.ts, feats = excluded.feats",
                       (key, rid, mint, now, "taught", TS._schema_id(db, names), TS.pack(v, names), None, None,
                        rug, runner))
            made = True
    log.info("teach: %s taught as %s%s", mint, label, "" if made else " (no research numbers yet: label only)")
    return {"mint": mint, "label": label, "example": made}


def stats(db) -> dict:
    out = {"runner": 0, "rug": 0, "nothing": 0}
    for lab in labels(db).values():
        out[lab] = out.get(lab, 0) + 1
    return out
