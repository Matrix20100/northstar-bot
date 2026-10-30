"""v5.5.0: "why wasn't this called?" from the bot's own records."""
from __future__ import annotations

import json
import time

from conftest import addr
from northstar import curve_watch as CW
from northstar import early_calls as EC
from northstar import why_not as WN

NOW = time.time()


def test_pasted_addresses_are_cleaned():
    m = "EQ7gCEMvQxC48PYfuPWwXVXxYuvGabpPWcCT1okvpump"
    assert WN.clean_mint(m + "\\") == m
    assert WN.clean_mint(f"  https://axiom.trade/t/{m}?ref=x ") == m
    assert WN.clean_mint("hello") is None


def test_a_coin_the_bot_never_saw(db):
    r = WN.explain(db, addr("ghost"), NOW)
    assert r["verdict"] == "NEVER_SEEN" and "never saw" in r["answer"]


def test_a_coin_stopped_by_the_runner_check_says_what_stopped_it(db):
    m = addr("mm")
    db.upsert_discovery(m, "pumpportal_new", 1.0, "MM", "MM")
    CW.init_schema(db)
    db.execute("INSERT INTO curve_watch(mint, symbol, entered_at, age_min, entry_mc, peak_mc) VALUES (?,?,?,?,?,?)",
               (m, "MM", NOW - 3600, 12.0, 25_000, 90_000))
    CW.record_runner(db, m, 70.0, False, NOW - 3500, block="extreme: 40% bundled")
    rep = {"reasons": ["REJECT - score 70"], "runner": {"score": 70, "blockers": ["extreme: 3 serious problems at once "
                                                                                 "(40% bundled; 48% sniped)"]}}
    db.execute("INSERT INTO research_snapshots(mint, ts, action, score, risk, report) VALUES (?,?,?,?,?,?)",
               (m, NOW - 3500, "REJECT", 70.0, "ELEVATED", json.dumps(rep)))
    r = WN.explain(db, m, NOW)
    assert r["verdict"] == "RUNNER_BLOCKED" and "40% bundled" in r["answer"]
    assert "3.6x" in r["after"]
    whats = [e["what"] for e in r["timeline"]]
    assert "Discovered" in whats and "Runner-call check" in whats and "Researched: REJECT" in whats


def test_a_called_coin_says_so(db):
    m = addr("ber")
    EC.init_schema(db)
    db.execute("INSERT INTO early_calls(mint, created_at, status, phase, entry_mcap, report) VALUES (?,?,?,?,?,?)",
               (m, NOW - 600, "SENT", "CURVE", 16_500, json.dumps({"identity": {"symbol": "ber"}})))
    r = WN.explain(db, m, NOW)
    assert r["verdict"] == "CALLED" and "$16.5K" in r["answer"] and r["symbol"] == "ber"
