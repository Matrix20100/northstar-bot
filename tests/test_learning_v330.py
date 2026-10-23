"""v3.3.0 always-on learning: curve learner, training cleanup, self-repair watchdog. SYNTHETIC data only."""
from __future__ import annotations

import asyncio
import json
import math
import struct
import time
import types

import pytest

from conftest import addr
from northstar import cleanup as CU
from northstar import curve_learn as CL
from northstar import curve_watch as CW
from northstar import training_store as TS
from northstar.config import Settings

S = Settings(_env_file=None)


def coin(db, mint, score, peak_x, first_low_x, low_x=None, last_x=None, safe=True, entered=None, followed=3600):
    CW.init_schema(db)
    entered = entered or time.time() - 6 * 3600
    e = 40_000.0
    db.execute("INSERT INTO curve_watch(mint, entered_at, entry_mc, peak_mc, low_mc, low_before_peak, last_mc, last_ts, "
               "done, runner_score, runner_safe, runner_at) VALUES (?,?,?,?,?,?,?,?,1,?,?,?)",
               (mint, entered, e, e * peak_x, e * (low_x or first_low_x), e * first_low_x, e * (last_x or peak_x),
                entered + followed, score, 1 if safe else 0, entered))


# ----------------------------------------------------------------------------- curve learner
def test_trade_result_replays_the_early_plan():
    row = {"entry_mc": 100.0}
    stop = S.early_stop - 1 - CL.FEES
    assert CL.trade_result({**row, "peak_mc": 300, "low_before_peak": 50, "last_mc": 10}, S) == pytest.approx(stop)
    tp = S.early_tp1_sell_pct / 100 * (S.early_tp1 - 1) + (1 - S.early_tp1_sell_pct / 100) * (
        max(1.0, 3.0 * (1 - S.early_trail_pct / 100)) - 1) - CL.FEES
    assert CL.trade_result({**row, "peak_mc": 300, "low_before_peak": 90, "last_mc": 50}, S) == pytest.approx(tp)
    assert CL.trade_result({**row, "peak_mc": 150, "low_before_peak": 90, "last_mc": 80}, S) == pytest.approx(-0.2 - CL.FEES)
    assert CL.trade_result({"entry_mc": None, "peak_mc": 1}, S) is None


def test_the_floor_moves_one_step_toward_what_real_outcomes_reward(db):
    s = Settings(_env_file=None, runner_min_score=45, curve_learn_min_samples=10)
    t0 = time.time() - 3 * 86400
    for i in range(60):                       # scores 45-49 dump through the stop, 50+ run to 3x
        low = i % 2 == 0
        coin(db, addr(f"c{i}"), 46 if low else 58, 1.1 if low else 3.0, 0.5 if low else 0.9, entered=t0 + i * 600)
    coin(db, addr("unsafe"), 90, 9.0, 0.95, safe=False)            # unsafe coins never count
    t = CL.table(db, s)
    assert t["safe_coins"] == 60
    change = CL.learn(db, s)
    assert change and change["from"] == 45 and change["to"] == 50 and s.runner_min_score == 50
    assert change["mean_to"] > change["mean_from"]
    s2 = Settings(_env_file=None, runner_min_score=45)
    assert CL.apply_saved(db, s2) == 50 and s2.runner_min_score == 50
    # already at the best floor: no change
    assert CL.learn(db, s) is None or s.runner_min_score in (50.0, 55.0)


def test_no_change_without_enough_coins_and_never_outside_the_rails(db):
    s = Settings(_env_file=None, runner_min_score=45, curve_learn_min_samples=25)
    for i in range(10):
        coin(db, addr(f"few{i}"), 80, 3.0, 0.9)
    assert CL.learn(db, s) is None
    s3 = Settings(_env_file=None, runner_min_score=70, curve_learn_min_samples=5, curve_learn_max_floor=70)
    for i in range(40):
        coin(db, addr(f"hi{i}"), 75, 3.0, 0.9, entered=time.time() - 86400 + i)
    change = CL.learn(db, s3)
    assert change is None or change["to"] <= 70


def test_the_runner_verdict_is_recorded_once_or_upgraded_when_it_turns_safe(db):
    CW.init_schema(db)
    db.execute("INSERT INTO curve_watch(mint, entered_at, entry_mc, peak_mc, low_mc, low_before_peak, last_mc, last_ts) "
               "VALUES (?,?,?,?,?,?,?,?)", ("M", 1.0, 1, 1, 1, 1, 1, 1))
    CW.record_runner(db, "M", 40, False, 10)
    CW.record_runner(db, "M", 41, False, 20)                       # still unsafe: the first stays
    r = db.fetchone("SELECT runner_score, runner_safe, runner_at FROM curve_watch WHERE mint='M'")
    assert (r["runner_score"], r["runner_safe"], r["runner_at"]) == (40, 0, 10)
    CW.record_runner(db, "M", 52, True, 30)                        # the moment it could be called
    CW.record_runner(db, "M", 70, True, 40)
    r = db.fetchone("SELECT runner_score, runner_safe, runner_at FROM curve_watch WHERE mint='M'")
    assert (r["runner_score"], r["runner_safe"], r["runner_at"]) == (52, 1, 30)


# ----------------------------------------------------------------------------- cleanup
def ex(db, key, mint, ts, unknown_share=0.0, rug=0, runner=0):
    TS.init_schema(db)
    n = 20
    k = int(round(n * unknown_share))
    vals = [float("nan")] * k + [1.0] * (n - k)
    db.execute("INSERT INTO training_examples(key, rid, mint, ts, src, schema, feats, peak_x, low_x, rug, runner) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (key, 1, mint, ts, "path", 1, struct.pack(f"<{n}f", *vals), 1.0, 1.0,
                                                 rug, runner))


def test_cleanup_keeps_only_real_fully_observed_examples(db):
    now = time.time()
    ex(db, "good", "A", now - 9 * 3600)
    ex(db, "repeat", "A", now - 8 * 3600)                          # same coin 1 h later
    ex(db, "later", "A", now - 1 * 3600)                           # 8 h later: a new decision, kept
    ex(db, "unknown", "B", now - 5000, unknown_share=0.7)
    ex(db, "nolabel", "C", now - 3 * 86400, rug=None, runner=None)
    ex(db, "fresh_nolabel", "D", now - 3600, rug=None, runner=None)  # may still get its label
    coin(db, "never", 50, 1.0, 1.0, followed=30)                     # left the radar within a minute
    coin(db, "fine", 50, 2.0, 0.9)
    dry = CU.run(db, now, dry_run=True)
    assert (dry["unlabelled"], dry["mostly_unknown"], dry["repeats"], dry["curve_not_followed"]) == (1, 1, 1, 1)
    assert db.fetchone("SELECT COUNT(*) n FROM training_examples")["n"] == 6          # dry run changes nothing
    c = CU.run(db, now)
    keys = {r["key"] for r in db.fetchall("SELECT key FROM training_examples")}
    assert keys == {"good", "later", "fresh_nolabel"} and c["examples_left"] == 3
    assert {r["key"] for r in db.fetchall("SELECT key FROM training_removed")} == {"repeat", "unknown", "nolabel"}
    assert db.fetchone("SELECT excluded FROM curve_watch WHERE mint='never'")["excluded"] == 1
    assert CW.evaluate(db)["scored"] == 1                           # the unfollowed coin no longer counts
    assert "removed 1 unlabelled" in CU.describe(c)


def test_harvest_never_copies_a_cleaned_example_back(db, monkeypatch):
    TS.init_schema(db)
    db.execute("INSERT INTO training_removed(key, ts, why) VALUES ('path:7', ?, 'repeat')", (time.time(),))
    seen = {}
    real = db.fetchall

    def spy(sql, params=()):
        rows = real(sql, params)
        if "training_removed" in sql:
            seen["removed"] = [r["key"] for r in rows]
        return rows
    monkeypatch.setattr(db, "fetchall", spy)
    TS.harvest(db)
    assert seen["removed"] == ["path:7"]


# ----------------------------------------------------------------------------- self-repair
async def test_watchdog_restarts_a_hung_loop():
    from northstar.engine import Engine
    f = types.SimpleNamespace()
    f.s = Settings(_env_file=None, watchdog_stuck_minutes=0.0)
    f.stop, f.stats = asyncio.Event(), {"errors": 0}
    f._loop = types.MethodType(Engine._loop, f)
    f._watchdog = types.MethodType(Engine._watchdog, f)
    calls = []

    async def work():
        calls.append(time.monotonic())
        if len(calls) == 1:
            await asyncio.sleep(3600)          # the first iteration hangs

    f._tasks = [asyncio.create_task(f._loop("worker", 0.01, work))]
    await asyncio.sleep(0.1)
    await f._watchdog()
    assert f.stats["self_repairs"] == 1 and len(f._tasks) == 2
    await asyncio.sleep(0.1)
    assert len(calls) >= 2                     # restarted and running again
    f.stop.set()
    for t in f._tasks:
        t.cancel()
    await asyncio.gather(*f._tasks, return_exceptions=True)


async def test_watchdog_leaves_a_healthy_loop_alone():
    from northstar.engine import Engine
    f = types.SimpleNamespace()
    f.s = Settings(_env_file=None, watchdog_stuck_minutes=0.0)
    f.stop, f.stats = asyncio.Event(), {"errors": 0}
    f._loop = types.MethodType(Engine._loop, f)
    f._watchdog = types.MethodType(Engine._watchdog, f)

    async def quick():
        return None

    f._tasks = [asyncio.create_task(f._loop("ok", 0.05, quick))]
    await asyncio.sleep(0.02)
    await f._watchdog()
    assert "self_repairs" not in f.stats
    f.stop.set()
    for t in f._tasks:
        t.cancel()
    await asyncio.gather(*f._tasks, return_exceptions=True)


def test_dashboard_learning_tab_shows_the_curve_learner(db):
    from test_dashboard import serve
    from northstar import learning as L
    L.init_schema(db)
    for i in range(3):
        coin(db, addr(f"d{i}"), 55, 2.5, 0.9)
    srv, c = serve(db, "pw")
    c.req("/api/login", {"password": "pw"})
    d = json.loads(c.req("/api/learning")[1])
    assert d["curve"]["safe_coins"] == 3 and d["curve"]["floor"] == 45
    assert next(f for f in d["curve"]["floors"] if f["floor"] == 50)["n"] == 3
    srv.server.shutdown()


def test_blocked_coins_report_what_each_rule_cost(db):
    """v3.3.2: the runner check's first safety blocker is stored per coin, so the curve report shows
    whether a rule blocks rugs or blocks runners."""
    now = time.time()
    for i in range(4):                                           # bundled launches: 3 of 4 rugged
        coin(db, addr(f"b{i}"), 60, 1.2, 0.25 if i < 3 else 0.9, low_x=0.2 if i < 3 else 0.9, safe=False)
        db.execute("UPDATE curve_watch SET runner_block='launch buys not passed', bundle_pct=20 WHERE mint=?",
                   (addr(f"b{i}"),))
    coin(db, addr("ok"), 60, 3.0, 0.9)
    t = CL.blocked_table(db, S)
    g = {x["group"]: x for x in t["groups"]}
    assert g["launch buys not passed"]["n"] == 4 and g["launch buys not passed"]["fell_0_3x"] == 75.0
    assert g["passed every safety check"]["hit_2x"] == 100.0
    assert "launch buys not passed" in CL.format_blocked(t)


def test_record_runner_keeps_the_blocker_and_bundle(db):
    CW.init_schema(db)
    db.execute("INSERT INTO curve_watch(mint, entered_at, entry_mc, peak_mc, low_mc, low_before_peak, last_mc, last_ts) "
               "VALUES ('Q', 1, 1, 1, 1, 1, 1, 1)")
    CW.record_runner(db, "Q", 59, False, 5, block="launch buys not passed", bundle_pct=15.0)
    r = db.fetchone("SELECT runner_block, bundle_pct FROM curve_watch WHERE mint='Q'")
    assert (r["runner_block"], r["bundle_pct"]) == ("launch buys not passed", 15.0)
