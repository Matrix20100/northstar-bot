"""v5.4.0: the call review. 10 of 16 runner calls on 24 Oct never went above about 1x and died; the review shows which
signals at the moment of the call separate those from the calls that ran."""
from __future__ import annotations

import json
import time

from conftest import addr
from northstar import call_review as CR
from northstar import early_calls as EC

NOW = time.time()


def call(db, i, *, peak_x, low_x=0.9, dead=False, wave=False, bundle=5.0, age_h=5.0, status="CLOSED"):
    EC.init_schema(db)
    rep = {"identity": {"symbol": f"C{i}"}, "launch_buys": {"status": "OBSERVED", "bundle_pct": bundle,
                                                             "early_buy_pct": 10.0, "verdict": "ORGANIC"},
           "attention": {"reasons": ['launch wave "dog": 9 launches by 6 creators in 30 min'] if wave else []},
           "runner": {"score": 75, "metrics": {"market_cap": 20_000, "vol_m5": 8_000, "buys_m5": 60, "sells_m5": 30,
                                               "chg_m5": 20}}}
    db.execute("INSERT INTO early_calls(mint, created_at, status, phase, entry_mcap, peak_mcap, low_mcap, plan_sent, "
               "report) VALUES (?,?,?,?,?,?,?,?,?)",
               (addr(f"c{i}"), NOW - age_h * 3600, status, "CURVE", 20_000, 20_000 * peak_x, 20_000 * low_x,
                json.dumps(["DEAD"] if dead else []), json.dumps(rep)))


def test_outcome_groups():
    o = CR.outcome
    assert o({"entry_mcap": 1, "peak_mcap": 2.5, "low_mcap": 0.2, "plan_sent": "[]", "created_at": 0}, NOW) == CR.RAN
    assert o({"entry_mcap": 1, "peak_mcap": 1.1, "low_mcap": 0.2, "plan_sent": "[]", "created_at": 0}, NOW) == CR.DIED
    assert o({"entry_mcap": 1, "peak_mcap": 1.1, "low_mcap": 0.9, "plan_sent": '["DEAD"]', "created_at": 0},
             NOW) == CR.DIED
    assert o({"entry_mcap": 1, "peak_mcap": 1.6, "low_mcap": 0.4, "plan_sent": "[]", "created_at": 0}, NOW) == CR.OTHER
    # a young open call that has neither died nor run is left out
    assert o({"entry_mcap": 1, "peak_mcap": 1.1, "low_mcap": 0.9, "plan_sent": "[]", "created_at": NOW - 600,
              "status": "SENT"}, NOW) is None


def test_the_review_finds_the_signal_that_separates_the_losers(db):
    for i in range(8):                                    # launch-wave calls: 7 of 8 died
        call(db, i, peak_x=1.05, low_x=0.2, dead=True, wave=True) if i < 7 else call(db, i, peak_x=2.5, wave=True)
    for i in range(8, 16):                                # the others: 2 died, 4 ran
        if i < 10:
            call(db, i, peak_x=1.1, low_x=0.3, dead=True)
        elif i < 14:
            call(db, i, peak_x=3.0)
        else:
            call(db, i, peak_x=1.5, low_x=0.8)
    call(db, 99, peak_x=1.0, low_x=0.95, age_h=0.2, status="SENT")     # too young: left out
    r = CR.review(db, NOW)
    assert r["calls"] == 16 and r["counts"] == {"DIED": 9, "RAN": 5, "OTHER": 2}
    wave = next(f for f in r["flags"] if f["signal"].startswith("launch wave"))
    assert wave["with"] == 8 and wave["died_with"] == 87.5 and wave["died_without"] == 25.0 and wave["enough"]
    assert r["flags"][0] is wave                                       # the biggest difference comes first
    bundled = next(f for f in r["flags"] if f["signal"].startswith("bundled"))
    assert bundled["with"] == 0 and not bundled["enough"]


def test_the_typical_numbers_per_group(db):
    for i in range(6):
        call(db, i, peak_x=1.0, low_x=0.2, dead=True, bundle=30.0)
    for i in range(6, 12):
        call(db, i, peak_x=3.0, bundle=4.0)
    nums = {n["key"]: n for n in CR.review(db, NOW)["numbers"]}
    assert nums["bundle_pct"]["died_median"] == 30.0 and nums["bundle_pct"]["ran_median"] == 4.0
    assert nums["bundle_pct"]["died_n"] == 6


def test_no_calls_yet(db):
    assert CR.review(db, NOW)["calls"] == 0
