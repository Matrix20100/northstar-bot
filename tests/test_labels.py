"""One outcome label for every learning path (v2.5.1). SYNTHETIC price paths only.

Runner: 2x before 0.6x. Rug: 0.3x before 1.3x. Unknown order -> AMBIGUOUS, too little history ->
INCOMPLETE; neither is ever turned into a binary training label.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import pytest

from conftest import addr
from northstar import brain as B
from northstar import learning as L
from northstar import outcome_audit as OA

from test_brain import report

# path rows: [minute, high_x, low_x, close_x]
RUNNER_PATH = [[0, 1.05, 0.95, 1.0], [10, 1.6, 1.2, 1.5], [25, 2.3, 1.8, 2.1], [200, 1.4, 1.0, 1.1]]
PUMP_THEN_RUG = [[0, 1.05, 0.95, 1.0], [10, 1.7, 1.2, 1.6], [40, 1.0, 0.15, 0.2], [200, 0.2, 0.1, 0.1]]
BIG_PUMP_THEN_RUG = [[0, 1.0, 0.95, 1.0], [10, 2.4, 1.3, 2.2], [40, 1.0, 0.1, 0.1]]
RUG_BEFORE_PUMP = [[0, 1.0, 0.95, 1.0], [5, 0.9, 0.2, 0.25], [90, 2.5, 0.3, 2.2]]
FLAT_PATH = [[0, 1.05, 0.9, 1.0], [60, 1.2, 0.85, 1.1], [180, 1.1, 0.8, 0.95]]
INCOMPLETE_PATH = [[0, 1.05, 0.9, 1.0], [20, 1.2, 0.85, 1.1]]


# =============================================================================================
# the label itself (minute paths)
# =============================================================================================
@pytest.mark.parametrize("path, label, y", [
    (RUNNER_PATH, B.RUNNER, 1),
    (BIG_PUMP_THEN_RUG, B.RUNNER, 1),          # 2x came first: a runner for anyone following the plan
    (PUMP_THEN_RUG, B.FLAT, 0),                # escaped to 1.7x before dumping: not a rug, not a runner
    (RUG_BEFORE_PUMP, B.RUG, 0),               # 0.3x before 1.3x: a rug, even though 2.5x came later
    (FLAT_PATH, B.FLAT, 0),                    # whole window seen, nothing decisive
    (INCOMPLETE_PATH, B.INCOMPLETE, None),     # 20 minutes of history proves nothing
    ([], None, None),
])
def test_path_labels(path, label, y):
    assert B.label_path(path) == label
    assert B.outcome(path) == y


def test_same_candle_conflicts_are_ambiguous_not_forced():
    assert B.label_path([[0, 2.1, 0.5, 1.0]]) == B.AMBIGUOUS            # 2x and the stop in one candle
    assert B.label_path([[0, 1.4, 0.2, 0.3]]) == B.AMBIGUOUS            # escape and rug in one candle
    assert B.outcome([[0, 2.1, 0.5, 1.0]]) is None


def test_stop_first_then_2x_is_not_a_runner():
    assert B.label_path([[0, 1.0, 0.55, 0.6], [30, 2.5, 0.6, 2.4]]) == B.FLAT


# =============================================================================================
# the label from a 24h high/low only (no order)
# =============================================================================================
@pytest.mark.parametrize("peak, trough, complete, label", [
    (2.4, 0.8, True, B.RUNNER),                # 2x and never touched the stop: order irrelevant
    (2.4, 0.8, False, B.RUNNER),
    (2.4, 0.1, True, B.AMBIGUOUS),             # pump-then-rug OR rug-then-pump: cannot tell
    (1.7, 0.15, True, B.AMBIGUOUS),            # escaped AND fell to 0.3x: whether it rugged first is unknown
    (1.1, 0.2, True, B.RUG),                   # never escaped: a rug whatever the order
    (1.5, 0.5, True, B.FLAT),                  # stopped, never 2x, never 0.3x
    (1.2, 0.9, True, B.FLAT),                  # whole window seen, nothing decisive
    (1.2, 0.9, False, B.INCOMPLETE),           # still being followed: no label yet
    (None, 0.9, True, None),
])
def test_extreme_labels(peak, trough, complete, label):
    assert B.label_extremes(peak, trough, complete) == label


def test_a_24h_high_of_2x_alone_never_makes_a_winner():
    """The v2.4 rule (y = 1 if peak >= 2x) gave this coin a winner label."""
    row = {"entry_price": 1.0, "max_price": 2.5, "min_price": 0.05, "samples": 300, "done": 1}
    assert B.tracking_label(row) == B.AMBIGUOUS
    assert B.binary(B.tracking_label(row)) is None


def test_tracking_completeness_needs_done_and_enough_polls():
    base = {"entry_price": 1.0, "max_price": 1.2, "min_price": 0.9}
    assert B.tracking_label({**base, "samples": 300, "done": 1}) == B.FLAT
    assert B.tracking_label({**base, "samples": 300, "done": 0}) == B.INCOMPLETE
    assert B.tracking_label({**base, "samples": 5, "done": 1}) == B.INCOMPLETE


# =============================================================================================
# every training path uses it
# =============================================================================================
def _shadow(db, mint, ts, path):
    db.execute("INSERT INTO shadow_calls(mint, ts, pool, entry_price, action, called, features, evaluated, path, vector) "
               "VALUES (?,?,?,?,?,?,?,1,?,?)", (mint, ts, addr("pool" + mint[:4]), 1.0, "REJECT", 0, "{}",
                                               json.dumps(path), json.dumps(B.vector(report()))))


def _tracked(db, i, mint, ts, peak, trough, samples=300, done=1):
    db.execute("INSERT INTO research_snapshots(id, mint, ts, action, report) VALUES (?,?,?,?,?)",
               (i, mint, ts, "REJECT", json.dumps(report())))
    db.execute("INSERT INTO decision_tracking(snapshot_id, mint, action, decided_at, entry_price, max_price, "
               "min_price, samples, done) VALUES (?,?,?,?,?,?,?,?,?)",
               (i, mint, "REJECT", ts, 1.0, peak, trough, samples, done))


def test_brain_training_rows_drop_ambiguous_and_incomplete(db):
    L.init_schema(db)
    now = time.time()
    t = now - 30 * 3600
    for k, path in enumerate((RUNNER_PATH, PUMP_THEN_RUG, RUG_BEFORE_PUMP, FLAT_PATH, INCOMPLETE_PATH,
                              [[0, 2.1, 0.5, 1.0]])):
        _shadow(db, addr(f"s{k}"), t + k * 60, path)
    _tracked(db, 1, addr("t1"), t, 3.0, 0.8)             # runner
    _tracked(db, 2, addr("t2"), t, 3.0, 0.05)            # 2x high but dumped too: ambiguous
    _tracked(db, 3, addr("t3"), t, 1.1, 0.2)             # rug
    _tracked(db, 4, addr("t4"), t, 1.2, 0.9)             # flat, complete
    _tracked(db, 5, addr("t5"), now - 3600, 1.2, 0.9, samples=20, done=0)   # still being followed
    rows = B.training_rows(db, 7, now)
    got = sorted((r["src"], r["y"]) for r in rows)
    assert got == sorted([("path", 1), ("path", 0), ("path", 0), ("path", 0),
                          ("tracking", 1), ("tracking", 0), ("tracking", 0)])


def test_shadow_evaluation_stores_the_shared_label(db):
    L.init_schema(db)
    now = time.time()
    ts = now - L.EVAL_DELAY - 600

    class CG:
        def __init__(self, rel):
            self.rel = rel

        async def candles(self, pool, start, end, token=None):
            return [(ts + m * 60 + 30, 1.0, h, lo, c, 100.0) for m, h, lo, c in self.rel]

    for name, path in (("run", RUNNER_PATH), ("rug", RUG_BEFORE_PUMP), ("short", INCOMPLETE_PATH)):
        db.execute("INSERT INTO shadow_calls(mint, ts, pool, entry_price, action, called, features) "
                   "VALUES (?,?,?,?,?,?,?)", (addr(name), ts, addr("p" + name), 1.0, "REJECT", 0, "{}"))
        asyncio.run(L.evaluate_due(db, CG(path), limit=5, now=now))
    got = {r["mint"]: (r["label"], r["win"]) for r in db.fetchall("SELECT mint, label, win FROM shadow_calls")}
    assert got[addr("run")] == (B.RUNNER, 1)
    assert got[addr("rug")] == (B.RUG, 0)
    assert got[addr("short")] == (B.INCOMPLETE, None)


def test_flag_statistics_count_runs_and_rugs_by_the_shared_label(db):
    L.init_schema(db)
    now = time.time()
    t = now - 30 * 3600
    for i, (peak, trough) in enumerate(((3.0, 0.8), (3.0, 0.05), (1.1, 0.2), (1.2, 0.9))):
        _tracked(db, 10 + i, addr(f"f{i}"), t, peak, trough)
        db.execute("UPDATE decision_tracking SET verdict_keys=? WHERE snapshot_id=?",
                   (json.dumps(["whale_holder"]), 10 + i))
    st = L.flag_outcomes(db, now)["whale_holder"]
    assert st["run2x"] == 1, "the coin that also dumped to 0.05x is not a 2x run"
    assert st["rug"] == 1 and st["labelled"] == 3
    assert st["crash"] == 2, "crash (24h low under 0.3x) keeps its old meaning: live scoring reads it"


def test_audit_uses_the_same_label():
    assert OA.label_path(PUMP_THEN_RUG) == B.FLAT
    assert OA.label_path(INCOMPLETE_PATH) is None
    assert OA.label_extremes(2.5, 0.05) == B.AMBIGUOUS


# =============================================================================================
# backward compatibility: an existing database keeps its rows and gains the columns
# =============================================================================================
def test_old_database_is_upgraded_in_place(tmp_path):
    from northstar.db import Database

    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE shadow_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, ts REAL NOT NULL,
            pool TEXT, entry_price REAL, entry_mcap REAL, liquidity REAL, action TEXT, call_type TEXT,
            called INTEGER NOT NULL DEFAULT 0, features TEXT NOT NULL, evaluated INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0, path TEXT, peak_x REAL, trough_x REAL, minutes_to_peak REAL,
            win INTEGER);
        CREATE TABLE flag_outcomes (key TEXT PRIMARY KEY, n INTEGER NOT NULL, crash INTEGER NOT NULL,
            run2x INTEGER NOT NULL, updated_at REAL NOT NULL);
        INSERT INTO shadow_calls(mint, ts, features, evaluated, path, win) VALUES ('old', 1, '{}', 1, '[]', 1);
        INSERT INTO flag_outcomes VALUES ('whale_holder', 40, 30, 5, 1);
    """)
    c.commit()
    c.close()
    db = Database(path)
    db.open()
    try:
        L.init_schema(db)
        assert db.fetchone("SELECT win, label FROM shadow_calls WHERE mint='old'") == {"win": 1, "label": None}
        assert db.fetchone("SELECT n, crash, run2x, rug, labelled FROM flag_outcomes") == \
            {"n": 40, "crash": 30, "run2x": 5, "rug": None, "labelled": None}
        assert L.load_flag_stats(db)["whale_holder"] == {"n": 40, "crash": 30, "run2x": 5}
    finally:
        db.close()


# =============================================================================================
# v2.5.6: what the brain learns — a pump-then-rug is a loss
# =============================================================================================
def test_a_pump_then_rug_is_not_a_win_for_the_brain():
    assert B.outcome(BIG_PUMP_THEN_RUG) == 1, "the shared label still says 2x came first"
    assert B.low_after_win(BIG_PUMP_THEN_RUG) == 0.1
    assert B.clean_outcome(BIG_PUMP_THEN_RUG) == 0, "but the brain learns it as a loss"
    assert B.clean_outcome(RUNNER_PATH) == 1, "a runner that held up is still a win"
    assert B.clean_outcome(RUG_BEFORE_PUMP) == 0 and B.clean_outcome(INCOMPLETE_PATH) is None


def test_training_rows_count_pump_then_rugs_as_losses(db):
    L.init_schema(db)
    now = time.time()
    t = now - 30 * 3600
    _shadow(db, addr("held"), t, RUNNER_PATH)
    _shadow(db, addr("dumped"), t + 60, BIG_PUMP_THEN_RUG)
    rows = {r["id"]: r["y"] for r in B.training_rows(db, 7, now) if r["src"] == "path"}
    assert sorted(rows.values()) == [0, 1]


def test_learning_messages_are_off_by_default():
    from northstar.config import Settings
    assert Settings(_env_file=None).learning_announce is False


def test_the_brain_must_also_beat_the_bar_on_the_most_recent_coins(db, monkeypatch):
    from northstar.config import Settings
    from test_brain import seed
    seed(db, n=240)
    s = Settings(_env_file=None, brain_min_samples=50, brain_min_auc=0.58)
    monkeypatch.setattr(B, "search_features", lambda *a, **k: None)
    for recent, trusted in ((0.56, False), (0.58, True)):
        monkeypatch.setattr(B, "walk_forward", lambda rows, active, r=recent: (0.70, r, {}))
        m = B.train(db, s, time.time())
        assert m is not None and m.trusted is trusted, (recent, m and m.trusted)
