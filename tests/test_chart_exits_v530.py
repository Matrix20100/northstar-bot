"""v5.3.0: chart-aware runner. Operator: "a trailing stop should be disabled for a retest and second pump... the bot
sold on BIBI which is doing higher highs, called at 20k now at 200k, but the trailing stop activated at a dip".
After take profit 1, what is left rides the chart's higher lows; with no higher low on the chart it keeps the old
percentage trail, so a straight pump-and-dump is sold exactly as before."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from conftest import addr, make_pair
from northstar import exits as EX
from northstar import paper as PT
from northstar.config import Settings

S = Settings(_env_file=None)
ENTRY = 20_000.0


def ticks(xs, step=60.0, buys=60, sells=40, chg=0.0):
    return [EX.Tick(mcap=ENTRY * x, at=step * i, buys_m5=buys, sells_m5=sells, vol_m5=20_000.0, vol_h1=100_000.0,
                    chg_m5=chg) for i, x in enumerate(xs)]


# a $BIBI-shaped run: higher highs and higher lows, a deep retest to 4.3x that holds above the last higher low
# (3.8x), then a second leg to 10x
BIBI = [1.0, 1.3, 1.6, 1.4, 1.9, 2.6, 2.2, 3.2, 4.5, 3.8, 5.0, 6.07, 5.2, 4.3, 4.8, 5.5, 7.0, 9.0, 10.0, 5.4]


def test_swing_lows_and_the_last_higher_low():
    path = [[i * 60, x] for i, x in enumerate(BIBI[:15])]
    lows = [x for _t, x in EX.swing_lows(path)]
    assert lows == [1.4, 2.2, 3.8, 4.3]
    assert EX.higher_low(path) == 4.3
    assert EX.higher_low([[0, 1.0], [60, 2.0], [120, 3.0]]) is None          # a straight line has none


def test_bibi_the_runner_rides_the_retest_the_old_trail_sold():
    old = EX.replay(ENTRY, 0.0, ticks(BIBI), S, structure=False)
    new = EX.replay(ENTRY, 0.0, ticks(BIBI), S, structure=True)
    assert old["exit"] == "TRAIL" and old["x"] == pytest.approx(0.35 * 1.6 + 0.35 * 2.6 + 0.30 * 4.3)
    assert new["exit"] == "GIVEBACK" and new["x"] == pytest.approx(0.35 * 1.6 + 0.35 * 2.6 + 0.30 * 5.4)
    assert new["x"] > old["x"]


def test_a_straight_pump_and_dump_is_sold_exactly_as_before():
    path = [1.0, 1.6, 2.6, 3.0, 2.2, 1.0]
    old = EX.replay(ENTRY, 0.0, ticks(path), S, structure=False)
    new = EX.replay(ENTRY, 0.0, ticks(path), S, structure=True)
    assert new["exit"] == old["exit"] == "TRAIL" and new["x"] == pytest.approx(old["x"])


def test_breaking_the_higher_low_sells():
    path = [1.0, 1.6, 1.4, 2.0, 2.8, 2.3, 3.4, 2.1]          # higher low 2.3x, then a break to 2.1x
    r = EX.replay(ENTRY, 0.0, ticks(path), S, structure=True)
    assert r["exit"] == "STRUCTURE" and "higher low (2.30x)" in r["reason"]


def test_a_crash_sells_at_once_whatever_the_chart():
    pos = EX.Position(entry_mcap=ENTRY, opened_at=0.0, peak_mcap=ENTRY, done={"TP1"},
                      path=[[i * 60, x] for i, x in enumerate([1.0, 1.6, 1.4, 2.0, 2.8])])
    t = EX.Tick(mcap=ENTRY * 2.5, at=600, buys_m5=10, sells_m5=80, chg_m5=-40.0)
    assert EX.decide(pos, t, S)[0].kind == "CRASH"


def test_a_runner_with_no_new_high_for_an_hour_is_sold():
    path = [1.0, 1.6, 1.4, 2.0, 2.8, 2.5] + [2.6] * 70        # stalls after 2.8x
    r = EX.replay(ENTRY, 0.0, ticks(path), S, structure=True)
    assert r["exit"] == "TIME" and "no new high" in r["reason"]


def test_switched_off_is_the_old_trail():
    s = Settings(_env_file=None, exit_structure_enabled=False)
    assert EX.replay(ENTRY, 0.0, ticks(BIBI), s)["exit"] == "TRAIL"


def test_paper_keeps_the_chart_and_compares_with_the_old_trail(db):
    run = PT.active_run(db, S, "calls")
    m = addr("bibi")
    t0 = time.time() - 3600
    PT._buy(db, run, "early:bibi", m, "BIBI", "runner", ENTRY, None, t0)

    class Dex:
        x = 1.0

        async def tokens_batch(self, mints):
            return [make_pair(base_mint=m, dex_id="pumpfun", market_cap=ENTRY * self.x, fdv=ENTRY * self.x,
                              liquidity_usd=None)]
    dex = Dex()
    mgr = PT.ExitManager(S, db, dex)
    for i, x in enumerate(BIBI):
        dex.x = x
        asyncio.run(mgr.run_once(now=t0 + 60 * i))
    row = dict(db.fetchone("SELECT * FROM paper_trades"))
    assert row["status"] == "CLOSED" and row["exit_reason"] == "GIVEBACK"
    assert len(json.loads(row["chart"])["p"]) >= len(BIBI) - 1
    d = PT.summary(db, S)
    vo = d["vs_old_rules"]
    assert vo["trades"] == 1 and vo["better"] == 1 and vo["new_pnl"] > vo["old_pnl"]
    t = d["trades"][0]
    assert t["old_rules"]["exit"] == "TRAIL" and t["old_rules"]["x"] == pytest.approx(0.35 * 1.6 + 0.35 * 2.6 + 0.3 * 4.3)
