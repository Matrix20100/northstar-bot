"""Feature lab: test whether new signals predict runners on YOUR bot's real history.

    python scripts/feature_lab.py data/northstar.db

For every decision the bot followed for 24 h it rebuilds the brain's numbers, then measures
walk-forward accuracy (AUC on coins the model had not seen; 0.50 = coin flip) for runners at 1.5x,
2x and 3x: first with the active signals, then adding each experimental signal on its own.
Read-only: the database is opened read-only and nothing is changed.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from northstar import brain as B                      # noqa: E402
from northstar.analysis import context as CTX         # noqa: E402


class Ro:
    def __init__(self, c):
        self.c = c

    def fetchone(self, sql, p=()):
        r = self.c.execute(sql, tuple(p)).fetchone()
        return dict(r) if r else None

    def get_candidate(self, mint):
        return self.fetchone("SELECT * FROM candidates WHERE mint=?", (mint,))


def main(path: str) -> int:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    db = Ro(c)
    rows = c.execute("SELECT d.mint, d.decided_at t, d.max_price/d.entry_price pk, r.report FROM decision_tracking d "
                     "JOIN research_snapshots r ON r.id = d.snapshot_id WHERE d.samples > 0 AND d.entry_price > 0 "
                     "ORDER BY d.decided_at").fetchall()
    data = []
    for r in rows:
        rep = json.loads(r["report"])
        if rep.get("lifecycle") == "PRE_MIGRATION_BONDING_CURVE":
            continue
        rep.setdefault("context", CTX.build(db, r["mint"], rep, db.get_candidate(r["mint"]), r["t"]))
        data.append((B.vector(rep, r["t"]), r["pk"], r["t"]))
    print(f"{len(data)} tracked coins")
    if len(data) < 100:
        print("need at least 100 tracked coins")
        return 1
    active = [f for f in B.FEATURES]
    for x in (1.5, 2.0, 3.0):
        rows_x = [{"id": i, "ts": t, "v": v, "y": 1 if pk >= x else 0} for i, (v, pk, t) in enumerate(data)]
        base, recent, _ = B.walk_forward(rows_x, active)
        print(f"\n{x:g}x: {sum(r['y'] for r in rows_x)} runners · active signals AUC {base:.3f} (recent {recent:.3f})")
        for f in B.EXPERIMENTAL_LABELS:
            a, rc, _ = B.walk_forward(rows_x, active + [f])
            if a is not None:
                print(f"   + {B.LABELS[f]:42} {a - base:+.3f}")
    return 0


if __name__ == "__main__":
    t = time.time()
    rc = main(sys.argv[1] if len(sys.argv) > 1 else "data/northstar.db")
    print(f"\n({time.time() - t:.0f} s)")
    sys.exit(rc)
