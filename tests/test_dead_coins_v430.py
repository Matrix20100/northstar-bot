"""v4.3.0: a coin back at $5K (dead_mcap_usd) or lower after being judged at $10K+ is dead/rugged.

Operator, with the live Calls tab: $PLUGGEDGPT called at $17.2K now $2.3K, $FORMATION $16.9K -> $3.4K,
$HACKER $16.4K -> $3.0K, still "OPEN" hours later: "all of these at 2k market cap or 5k should be read
as dead/rugged. and learned from".
"""
from __future__ import annotations

import asyncio
import json

import pytest

import test_early_calls as TE
from northstar import brain as B
from northstar import curve_watch as CW
from northstar import learning as L
from northstar import operator_settings as OS
from northstar import paper as P
from northstar import training_store as TS


@pytest.fixture(autouse=True)
def _default_dead_line():
    old = B.DEAD_MCAP
    B.DEAD_MCAP = 5_000.0
    yield
    B.DEAD_MCAP = old


# ----------------------------------------------------------------------------- the rule
@pytest.mark.parametrize("entry, now, dead", [
    (17_200, 2_300, True),      # $PLUGGEDGPT
    (17_000, 4_500, True),      # $Ba-ha-we-snev
    (16_900, 3_400, True),      # $FORMATION
    (12_000, 4_800, True),      # 0.40x: not a rug by multiple, dead by market cap
    (29_700, 18_000, False),    # $BLUEBEAM: down, not dead
    (6_000, 3_000, False),      # judged near launch value: the market-cap rule does not apply
    (40_000, 11_000, True),     # 0.275x: the multiple rule still applies
])
def test_dead_rule_on_the_live_coins(entry, now, dead):
    assert B.is_dead(now, entry) is dead


def test_dead_line_limits_and_off_switch():
    assert B.rug_line(10_000) == 0.5                 # $10K entry: dead at $5K
    assert B.rug_line(100_000) == B.RUG_X            # big coins: 0.3x is the stricter line
    assert B.rug_line(None) == B.RUG_X
    assert B.rug_line(17_000, dead_mcap=0) == B.RUG_X
    assert not B.is_dead(4_800, 12_000, dead_mcap=0)


# ----------------------------------------------------------------------------- runner calls + paper
def test_dead_runner_call_closes_silently_and_paper_sells(db, monkeypatch):
    s, pub, c = TE.caller(db)
    row = asyncio.run(c.consider(TE.outcome(TE.curve_report())))
    assert row["entry_mcap"] == 40_000
    db.execute("UPDATE early_calls SET created_at=? WHERE mint=?", (TE.NOW, TE.MINT))
    run = P.active_run(db, s, "calls")
    db.execute("UPDATE paper_runs SET started_at = ? WHERE id = ?", (TE.NOW - 10, run["id"]))
    P.PaperTrader(s, db).run_once(TE.NOW + 1)
    n_posts = len(pub.sent)
    st, tr = TE.tracker(db, pub, monkeypatch, cap_sol=11.5)            # 11.5 SOL * $200 = $2,300
    counts = asyncio.run(tr.run_once(TE.NOW + 3600))
    r = db.fetchone("SELECT * FROM early_calls WHERE mint=?", (TE.MINT,))
    assert r["status"] == "CLOSED" and counts["dead"] == 1
    assert "DEAD" in json.loads(r["plan_sent"])
    assert len(pub.sent) == n_posts, "nothing is posted about a dead coin"
    P.PaperTrader(st, db).run_once(TE.NOW + 3601)
    t = db.fetchone("SELECT * FROM paper_trades WHERE ref = ?", (f"early:{TE.MINT}",))
    assert t["status"] == "CLOSED" and t["exit_reason"] in ("DEAD", "STOP")   # the plan's stop may fire first


def test_a_call_that_is_only_down_stays_open(db, monkeypatch):
    s, pub, c = TE.caller(db)
    asyncio.run(c.consider(TE.outcome(TE.curve_report())))
    db.execute("UPDATE early_calls SET created_at=? WHERE mint=?", (TE.NOW, TE.MINT))
    st, tr = TE.tracker(db, pub, monkeypatch, cap_sol=90.0)            # $18K from $40K
    counts = asyncio.run(tr.run_once(TE.NOW + 600))
    r = db.fetchone("SELECT * FROM early_calls WHERE mint=?", (TE.MINT,))
    assert r["status"] != "CLOSED" and not counts.get("dead")


# ----------------------------------------------------------------------------- learning labels
PATH_DIED = [[1, 1.2, 0.9, 1.1], [30, 2.3, 1.1, 2.1], [60, 2.1, 0.4, 0.4], [130, 0.45, 0.38, 0.4]]


def test_training_labels_use_the_dead_line():
    # judged at $12K: 2.3x then down to 0.38x = $4,560. The old 0.3x rule said "runner, not a rug"
    assert TS.path_labels(PATH_DIED)[2:] == (0, 1)
    peak, low, rug, runner = TS.path_labels(PATH_DIED, B.rug_line(12_000))
    assert (rug, runner) == (1, 0)
    assert B.label_path(PATH_DIED[:1] + [[60, 1.1, 0.4, 0.4]], rug_x=B.rug_line(12_000)) == B.RUG


def test_stored_examples_are_relabelled_once(db):
    L.init_schema(db)
    TS.init_schema(db)
    sid = db.execute("INSERT INTO shadow_calls(mint, ts, entry_mcap, features, called) VALUES ('D', 1, 12000, '{}', 1)")
    sid = db.fetchone("SELECT id FROM shadow_calls WHERE mint='D'")["id"]
    db.execute("INSERT INTO training_examples(key, rid, mint, ts, src, schema, feats, peak_x, low_x, rug, runner) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (f"path:{sid}", sid, "D", 1, "path", 1, b"xx", 2.3, 0.38, 0, 1))
    db.execute("INSERT INTO shadow_calls(mint, ts, entry_mcap, features, called) VALUES ('A', 1, 30000, '{}', 1)")
    aid = db.fetchone("SELECT id FROM shadow_calls WHERE mint='A'")["id"]
    db.execute("INSERT INTO training_examples(key, rid, mint, ts, src, schema, feats, peak_x, low_x, rug, runner) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (f"path:{aid}", aid, "A", 1, "path", 1, b"xx", 1.4, 0.6, 0, 0))
    assert TS.relabel_dead(db) == 1
    d = db.fetchone("SELECT rug, runner FROM training_examples WHERE mint='D'")
    a = db.fetchone("SELECT rug, runner FROM training_examples WHERE mint='A'")
    assert (d["rug"], d["runner"]) == (1, 0) and (a["rug"], a["runner"]) == (0, 0)
    assert TS.relabel_dead(db) == 0                  # once per setting


def test_curve_watch_counts_dead_coins_as_fallen():
    assert CW.fell({"entry_mc": 12_000, "low_mc": 4_900})
    assert not CW.fell({"entry_mc": 30_000, "low_mc": 12_000})


def test_dashboard_setting_moves_the_dead_line(db, settings):
    OS.set_value(db, settings, "dead_mcap_usd", 3000)
    assert B.DEAD_MCAP == 3000.0 and not B.is_dead(4_800, 12_000)
    OS.reset(db, settings, "dead_mcap_usd")
    assert B.DEAD_MCAP == 5000.0 and B.is_dead(4_800, 12_000)
