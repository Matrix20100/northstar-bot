"""v4.2.1: the dashboard's Learning tab read through its read-only connection.

Live report: the tab said "Rug brain: not trained yet", "Training memory: 0 coins" and
"Coins recorded 0" while the log showed a trusted rug brain on 5,000 coins. Causes:
  * rugmodel.latest / training_store.stats ran CREATE TABLE first, which the read-only
    adapter cannot do, so they fell back to "nothing";
  * the flag table reused the name `stats`, overwriting the shadow-call counts;
  * coins recorded before v4.2.0 have no stored theme, so every one showed as "other";
  * the blocked table split one rule into a row per measured value.
"""
from __future__ import annotations

import sqlite3
import time

from northstar import curve_learn as CL
from northstar import curve_watch as CW
from northstar import learning as L
from northstar import rugmodel as RM
from northstar import themes as TH
from northstar import training_store as TS
from northstar.dashboard import _Ro, view_learning


def _ro(db) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{db.path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _seed(db, now):
    L.init_schema(db)
    RM.init_schema(db)
    TS.init_schema(db)
    CW.init_schema(db)
    for i in range(7):
        db.execute("INSERT INTO shadow_calls(mint, ts, called, features, evaluated, peak_x) VALUES (?,?,?,?,?,?)",
                   (f"M{i}", now - 3600, 1 if i < 2 else 0, "{}", 1 if i < 5 else 0, 3.0 if i == 0 else 1.1))
    db.execute("INSERT INTO rug_models(ts, n, positives, auc, recent_auc, trusted, model) VALUES (?,?,?,?,?,?,?)",
               (now - 600, 5000, 900, 0.93, 0.91, 1,
                '{"features": ["a"], "mean": {"a": 0}, "std": {"a": 1}, "coef": {"a": 0.5}, "bias": 0}'))
    for i in range(3):
        db.execute("INSERT INTO training_examples(key, rid, mint, ts, src, schema, feats, rug, runner) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", (f"path:{i}", i, f"M{i}", now, "path", 1, b"xx", 1 if i == 0 else 0,
                                                 1 if i == 1 else 0))
    db.execute("INSERT INTO flag_outcomes(key, n, crash, run2x, updated_at) VALUES ('_all', 100, 60, 10, 0)")
    db.execute("INSERT INTO flag_outcomes(key, n, crash, run2x, updated_at) VALUES ('bundle', 54, 51, 1, 0)")


def _shadow_cols_ok(db) -> bool:
    cols = {r["name"] for r in db.fetchall("PRAGMA table_info(shadow_calls)")}
    return {"mint", "ts", "called", "evaluated", "peak_x"} <= cols


def test_learning_tab_reads_rug_brain_memory_and_counts_read_only(db, settings):
    now = time.time()
    if not _shadow_cols_ok(db):
        L.init_schema(db)
    _seed(db, now)
    out = view_learning(_ro(db), settings, now)
    assert out["ready"]
    assert out["rug_brain"] is not None and out["rug_brain"]["n"] == 5000 and out["rug_brain"]["trusted"]
    assert out["memory"]["examples"] == 3 and out["memory"]["rugs"] == 1 and out["memory"]["runners"] == 1
    assert out["shadows"]["total"] == 7 and out["shadows"]["called"] == 2 and out["shadows"]["evaluated"] == 5
    assert out["flags"]["rows"][0]["key"] == "bundle"


def test_read_only_helpers_do_not_try_to_create_tables(db):
    RM.init_schema(db)
    TS.init_schema(db)
    ro = _Ro(_ro(db))
    assert RM.latest(ro) is None                     # no model yet, but no error swallowed as "nothing" either
    assert TS.stats(ro)["examples"] == 0


def test_old_coins_without_a_theme_are_classified_from_their_ticker(db):
    now = time.time()
    CW.init_schema(db)
    for i, sym in enumerate(["PEPEDOG", "TRUMPWIN", "ZZQX"]):
        db.execute("INSERT INTO curve_watch(mint, symbol, entered_at, entry_mc, peak_mc, low_mc, done) "
                   "VALUES (?,?,?,?,?,?,1)", (f"T{i}", sym, now - 3600, 10_000, 25_000, 9_000))
    db.execute("UPDATE curve_watch SET theme = NULL")
    st = TH.theme_stats(_Ro(_ro(db)), 7.0, now)
    assert set(st["themes"]) == {"animal", "politics", "other"}
    assert st["all"]["n"] == 3


def test_blocked_rows_group_by_rule_not_by_number():
    assert CL.block_key("5-min volume $5,340 under $7,500") == "5-min volume under $7,500"
    assert CL.block_key("5-min volume $6,100 under $7,500") == "5-min volume under $7,500"
    assert CL.block_key("extreme: 52% bought in the creation block (bundled; reject at 40%)") == "extreme: bundled launch"
    assert CL.block_key("extreme: 4 serious problems at once (a; b)") == "extreme: 3+ serious problems"
    assert CL.block_key("trap check: mint authority not passed: still active") == "trap check: mint authority not passed"
    assert CL.block_key("market cap $91,000 outside $10,000-$80,000") == "market cap outside the band"
    assert CL.block_key("launch buys not passed") == "launch buys not passed"
    assert CL.block_key(None) == "other"
    assert CL.block_key("") == "other"


def test_blocked_table_merges_volume_rows(db, settings):
    now = time.time()
    CW.init_schema(db)
    for i, vol in enumerate([5340, 6100, 2200]):
        db.execute("INSERT INTO curve_watch(mint, symbol, entered_at, entry_mc, peak_mc, low_mc, done, runner_at, "
                   "runner_safe, runner_block) VALUES (?,?,?,?,?,?,1,?,0,?)",
                   (f"V{i}", "X", now - 3600, 10_000, 12_000, 8_000, now - 3500,
                    f"5-min volume ${vol:,} under $7,500"))
    groups = CL.blocked_table(db, settings, 7.0, now)["groups"]
    assert [(g["group"], g["n"]) for g in groups] == [("5-min volume under $7,500", 3)]
