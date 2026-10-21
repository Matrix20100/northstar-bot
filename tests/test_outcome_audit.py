"""Outcome audit (false rejections vs rugs called) and the curve-stage data-gap fixes.

All price paths, reports and outcomes here are SYNTHETIC test data built to the real table
layouts; they say nothing about how Northstar performed live.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

import test_curve_without_dexscreener as C
import test_pump_trades as T
from northstar import early_calls as EC
from northstar import learning as LRN
from northstar import outcome_audit as OA
from northstar.models import Action
from northstar.research import Providers, Researcher

from test_unified_engine import gates as clean_gates
from test_unified_engine import report as clean_report


# =============================================================================================
# labels
# =============================================================================================
def test_path_labels_respect_order():
    assert OA.label_path([[1, 1.2, 0.9, 1.1], [5, 2.3, 1.0, 2.1]]) == OA.RUNNER
    assert OA.label_path([[1, 1.1, 0.25, 0.3]]) == OA.RUG
    # escaped to 1.3x first, then dumped: a buyer had a profitable exit -> not a rug
    assert OA.label_path([[1, 1.4, 1.0, 1.3], [9, 1.3, 0.2, 0.2]]) == OA.NEUTRAL
    # stopped out at 0.6x before 2x -> not a runner, even though 2x came later
    assert OA.label_path([[1, 1.0, 0.55, 0.6], [30, 2.5, 0.6, 2.4]]) == OA.NEUTRAL
    assert OA.label_path([]) is None


def test_extremes_never_guess_the_order():
    assert OA.label_extremes(2.4, 0.5) == OA.MIXED
    assert OA.label_extremes(2.4, 0.8) == OA.RUNNER
    assert OA.label_extremes(1.1, 0.2) == OA.RUG
    assert OA.label_extremes(1.6, 0.2) == OA.NEUTRAL
    assert OA.label_extremes(None, 0.5) is None


def test_reason_families_group_numbers_together():
    assert OA.reason_family(["prefilter: liquidity $1,895 below prefilter $5,000"]) == "prefilter: liquidity"
    assert OA.reason_family(["gate failed: source_consistency — price sources disagree by 119%"]) == \
        "provider price disagreement"
    assert OA.reason_family(["gate failed: rugcheck_danger — RugCheck danger: Large Amount of LP Unlocked"]) == \
        "RugCheck danger"
    assert OA.reason_family([]) == "no reason recorded"


# =============================================================================================
# the audit on a synthetic database
# =============================================================================================
RUN_PATH = [[1, 1.2, 0.95, 1.1], [20, 2.6, 1.0, 2.4]]
RUG_PATH = [[1, 1.05, 0.2, 0.25]]
FLAT_PATH = [[1, 1.1, 0.8, 1.0], [60, 1.15, 0.85, 0.9]]


def _snapshot(db, mint, ts, action, report):
    db.execute("INSERT INTO research_snapshots(mint, ts, action, score, risk, confidence, report) "
               "VALUES (?,?,?,?,?,?,?)", (mint, ts, action, None, "", "", json.dumps(report)))


def _shadow(db, mint, ts, action, path, called=0, call_type=None, failed=()):
    db.execute("INSERT INTO shadow_calls(mint, ts, pool, entry_price, action, call_type, called, features, "
               "evaluated, path, peak_x, trough_x) VALUES (?,?,?,?,?,?,?,?,1,?,?,?)",
               (mint, ts, "pool", 1.0, action, call_type, called, json.dumps({"failed": list(failed)}),
                json.dumps(path), max(p[1] for p in path), min(p[2] for p in path)))


def _rejected_report(reason, gate_fail=None, red=()):
    r = clean_report()
    if gate_fail:
        r["gates"] = clean_gates(**{gate_fail: "FAIL"})
    r["verdict"] = {"level": "CAUTION" if red else "LOOKS CLEAN", "score": 10,
                    "red": [{"key": k, "level": "high", "text": k} for k in red], "green": [], "unknown": []}
    r["reasons"] = [reason]
    return r


@pytest.fixture
def history(db):
    LRN.init_schema(db)
    EC.init_schema(db)
    now = time.time() - 86400
    n = 0

    def add(action, path, reason="", gate=None, red=(), called=0, call_type=None):
        nonlocal n
        n += 1
        mint = f"mint{n:04d}"
        rep = _rejected_report(reason, gate, red) if not called else clean_report()
        _snapshot(db, mint, now + n, action, rep)
        _shadow(db, mint, now + n + 5, action, path, called, call_type, [gate] if gate else [])
        return mint

    # source_consistency rejects runners and lets rugs through equally: a non-discriminating gate
    for _ in range(6):
        add("REJECT", RUN_PATH, "gate failed: source_consistency — price sources disagree by 90%",
            gate="source_consistency")
    for _ in range(2):
        add("REJECT", FLAT_PATH, "gate failed: source_consistency — price sources disagree by 40%",
            gate="source_consistency")
    # mint authority catches rugs
    for _ in range(8):
        add("REJECT", RUG_PATH, "gate failed: mint_authority — active", gate="mint_authority")
    # flat background
    for _ in range(10):
        add("WATCHLIST", FLAT_PATH, "score between the lines")
    # calls: 2 standard runners, 3 scalps that rugged
    add("AUTO_POST", RUN_PATH, called=1, call_type="STANDARD")
    add("AUTO_POST", RUN_PATH, called=1, call_type="STANDARD")
    for _ in range(3):
        add("REJECT", RUG_PATH, called=1, call_type="RUG_SCALP")
    # an early call that rugged, whose holder check never ran
    rep = {"lifecycle": "PRE_MIGRATION_BONDING_CURVE",
           "gates": [{"name": "mint_authority", "status": "PASS"}]}
    db.execute("INSERT INTO early_calls(mint, created_at, status, phase, entry_mcap, peak_mcap, low_mcap, report) "
               "VALUES (?,?,?,?,?,?,?,?)", ("earlyrug", now, "CLOSED", "CURVE", 40_000, 42_000, 8_000, json.dumps(rep)))
    db.execute("INSERT INTO early_calls(mint, created_at, status, phase, entry_mcap, peak_mcap, low_mcap, report) "
               "VALUES (?,?,?,?,?,?,?,?)", ("earlyrun", now, "CLOSED", "CURVE", 40_000, 120_000, 36_000,
                                            json.dumps({"lifecycle": "PRE_MIGRATION_BONDING_CURVE",
                                                        "gates": [{"name": k, "status": "PASS"}
                                                                  for k in EC.EARLY_REQUIRED_PASS]})))
    return db


def test_audit_counts_false_rejections_and_rugs_called(history):
    a = OA.audit(str(history.path))
    assert a["observations"] == 33
    assert a["false_rejections"] == 6, "the six runners rejected over a price disagreement"
    assert a["rugs_called"] == 4, "three scalps and one early call rugged"
    assert a["calls_by_type"]["RUG_SCALP"]["rug_pct"] == 100.0
    assert a["calls_by_type"]["STANDARD"]["runner_pct"] == 100.0
    fams = a["rejections_by_reason"]
    assert fams["provider price disagreement"]["runner"] == 6
    assert fams["mint authority"]["rug"] == 8


def test_audit_separates_signals_that_catch_rugs_from_ones_that_block_runners(history):
    a = OA.audit(str(history.path))
    gates_ = {d["signal"]: d for d in a["gate_discrimination"]}
    assert gates_["source_consistency"]["verdict"] == "blocks runners, does not catch rugs"
    assert gates_["mint_authority"]["verdict"] == "catches rugs"


def test_replay_with_current_engine_keeps_traps_and_frees_provider_conflicts(history):
    a = OA.audit(str(history.path))
    rp = a["replay"]
    assert rp["replayed"] >= 26
    assert rp["new"]["runners_rejected"] < rp["old"]["runners_rejected"]
    moved = rp["moves"]
    assert "REJECT -> REJECT" in moved and moved["REJECT -> REJECT"]["rug"] >= 8, \
        "every mint-authority rug is still rejected"


def test_early_gate_replay_holds_the_unverified_rug_and_keeps_the_verified_runner(history):
    eg = OA.audit(str(history.path))["early_gate_replay"]
    assert eg["would_hold"]["rug"] == 1 and eg["would_hold"]["n"] == 1
    assert eg["would_keep"]["runner"] == 1


def test_report_text_is_complete(history):
    text = OA.format_audit(OA.audit(str(history.path)))
    for part in ("CALLS", "REJECTIONS BY REASON", "FAILED GATES", "REPLAY", "EARLY NARRATIVE"):
        assert part in text


def test_audit_survives_an_empty_or_old_database(db):
    a = OA.audit(str(db.path))
    assert a["observations"] == 0 and a["rugs_called"] == 0
    assert "CALLS" in OA.format_audit(a)


# =============================================================================================
# curve stage: unmeasured is not the same as missed
# =============================================================================================
class _Curve(C.ResearchWithoutDexScreener):
    def runTest(self):  # pragma: no cover - unittest needs one
        pass


@pytest.fixture
def curve():
    t = _Curve()
    t.setUp()
    yield t
    t.doCleanups()


def _txs(mint, n_buy=18, n_sell=6, sol=300_000_000):
    now = int(time.time())
    out = [T.pump_tx(sig=f"b{i}", user=T.addr(f"buyer{i}"), side="buy", mint=mint, block_time=now - 2 - i, sol=sol)
           for i in range(n_buy)]
    out += [T.pump_tx(sig=f"s{i}", user=T.addr(f"seller{i}"), side="sell", ix_name="sell", mint=mint,
                      block_time=now - 40 - i, sol=200_000_000) for i in range(n_sell)]
    return out


def test_curve_with_no_market_cap_history_is_rechecked_not_rejected(curve):
    out = curve.research(T.FakeDex(), C.ChainRpc(_txs(C.UNSEEN_MINT), curve_account=C.curve_with_creator()))
    assert out.action == Action.INSUFFICIENT_DATA, out.reasons
    assert "market-cap trend not measured" in out.reasons[0]


def test_buys_without_sol_amounts_are_unmeasured_not_zero(curve):
    out = curve.research(T.FakeDex(), C.ChainRpc(_txs(C.UNSEEN_MINT, sol=0), curve_account=C.curve_with_creator()))
    pm = out.report["pre_migration"]
    assert pm["buy_sol_5m"] is None and pm["buy_flow_status"].startswith("UNMEASURED")
    assert out.action == Action.INSUFFICIENT_DATA
    assert "buy flow not measured" in out.reasons[0]
    assert "0.00 SOL" not in " ".join(out.reasons)


def test_a_real_threshold_miss_still_rejects_and_names_the_gaps(curve):
    out = curve.research(T.FakeDex(), C.ChainRpc(_txs(C.UNSEEN_MINT, n_buy=3, n_sell=12),
                                                 curve_account=C.curve_with_creator()))
    assert out.action == Action.REJECT
    text = " ".join(out.reasons)
    assert "buy/sell ratio" in text and "market-cap trend not measured" in text
