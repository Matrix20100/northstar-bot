"""Shadow testing: live vs proposed decisions, recorded side by side. SYNTHETIC reports only.

What must hold:
  * the live decision, callout and stored report are never touched;
  * with no proposal rules the proposal reproduces the live decision exactly (same scoring code);
  * a proposal never relaxes a proven trap;
  * one row per snapshot, and repeated research of an unchanged coin is not re-recorded;
  * outcomes only come from price data that starts at the decision, and only once the window
    has passed.
"""
from __future__ import annotations

import copy
import json
import time
from types import SimpleNamespace

import pytest

from conftest import addr, make_pair
from northstar import brain as B
from northstar import learning as L
from northstar import shadow_test as SH
from northstar.analysis import unified as U
from northstar.config import Settings
from northstar.engine import Engine
from northstar.final import apply_final
from northstar.models import Action
from northstar.research import ResearchOutcome

from test_unified_engine import gates, report

RULES = SH.parse_rules("lp_unverified_warn,holder_gate_warn")
LP_DANGER = "RugCheck danger: Large Amount of LP Unlocked"


@pytest.fixture
def s() -> Settings:
    # one policy, so row counts below are per decision (multi-policy recording is tested at the end)
    return Settings(_env_file=None, discord_token="t", discord_channel_id=1, discord_vip_channel_id=2,
                    min_liquidity_usd=15_000, min_narrative_evidence_items=0,
                    min_volume_acceleration=0, min_buy_sell_ratio_h1=0,
                    shadow_test_rules="lp_unverified_warn,holder_gate_warn")


def holder_fail() -> dict:
    return report(gates=gates(holder_concentration="FAIL"))


def lp_only(dex="pumpswap", **extra) -> dict:
    g = gates(rugcheck_danger="FAIL")
    for x in g:
        if x["name"] == "rugcheck_danger":
            x["reason"] = LP_DANGER
    r = report(gates=g, **extra)
    r["identity"]["dex"] = dex
    return r


def outcome(r: dict, s, snap=1, mint=None, price=1.0) -> ResearchOutcome:
    r = copy.deepcopy(r)
    r["unified"] = U.evaluate(r, s)
    out = ResearchOutcome(mint or addr("coin"), Action.REJECT, r, pair=make_pair(price_usd=price),
                          snapshot_id=snap)
    apply_final(out)
    return out


# =============================================================================================
# the proposal
# =============================================================================================
@pytest.mark.parametrize("make", [report, holder_fail, lp_only,
                                  lambda: report(market_structure={"status": "OBSERVED", "fading": True}),
                                  lambda: report(gates=gates(mint_authority="FAIL")),
                                  lambda: report(gates=gates(lp_lock="UNKNOWN"))])
def test_without_rules_the_proposal_is_the_live_decision(s, make):
    r = make()
    u = U.evaluate(r, s)
    r["unified"] = u
    p = SH.propose(r, s, [], {})
    assert (p["decision"], p["score"], p["reason_class"], p["risk"]) == \
        (u["decision"], u["score"], u["reason_class"], u["risk"])


def test_parity_holds_with_the_learning_brain_tilt(s):
    r = report()
    r["unified"] = U.apply_model(U.evaluate(r, s), r, 0.4, True, baseline=0.15, s=s)
    p = SH.propose(r, s, [], {})
    assert p["score"] == r["unified"]["score"] and p["model"]["tilt"] == r["unified"]["model"]["tilt"] != 0


def test_holder_gate_becomes_a_warning_not_a_rejection(s):
    r = holder_fail()
    r["unified"] = U.evaluate(r, s)
    assert r["unified"]["decision"] == U.REJECT
    p = SH.propose(r, s, RULES, {})
    assert p["decision"] in (U.WATCH, U.CAUTION) and p["shadow_rules_fired"] == ["holder_gate_warn"]


def test_unverified_lp_danger_on_pumpswap_caps_at_watch(s):
    r = lp_only()
    r["unified"] = U.evaluate(r, s)
    assert r["unified"]["decision"] == U.REJECT
    p = SH.propose(r, s, RULES, {})
    assert p["decision"] == U.WATCH, "a serious warning: watchable, never a call"
    assert p["shadow_rules_fired"] == ["lp_unverified_warn"]


@pytest.mark.parametrize("r", [
    lp_only(dex="raydium"),                                         # outside the audited pool type
    lp_only(lp_onchain_check="verified: 12.0% burned"),             # Northstar READ the LP itself
])
def test_lp_rule_stays_inside_its_scope(s, r):
    r["unified"] = U.evaluate(r, s)
    assert SH.propose(r, s, RULES, {})["decision"] == U.REJECT


def test_lp_rule_never_softens_a_second_rugcheck_danger(s):
    r = lp_only()
    next(g for g in r["gates"] if g["name"] == "rugcheck_danger")["reason"] = LP_DANGER + ", Creator history of rugged tokens"
    r["unified"] = U.evaluate(r, s)
    assert SH.propose(r, s, RULES, {})["decision"] == U.REJECT


@pytest.mark.parametrize("gate", ["mint_authority", "freeze_authority", "token_extensions", "liquidity_removal"])
def test_a_proven_trap_is_never_relaxed(s, gate):
    r = report(gates=gates(**{gate: "FAIL", "holder_concentration": "FAIL"}))
    r["identity"]["dex"] = "pumpswap"
    r["unified"] = U.evaluate(r, s)
    assert SH.propose(r, s, RULES, {"u_watch_min": 1.0})["decision"] == U.REJECT


def test_overrides_are_validated_and_named(s):
    assert SH.parse_overrides('{"u_watch_min": 40, "discord_token": "x", "u_nope": 1, "u_strong_min": "9"}', s) \
        == {"u_watch_min": 40}
    assert SH.parse_overrides("not json", s) == {}
    assert SH.parse_rules("holder_gate_warn, bogus,holder_gate_warn") == ["holder_gate_warn"]
    a, b = SH.policy_name(RULES, {}), SH.policy_name(RULES, {"u_watch_min": 40})
    assert a != b and a.startswith(SH.POLICY_VERSION)


def test_an_override_moves_only_the_proposal(s):
    r = report(market_structure={"status": "OBSERVED", "fading": True})
    r["unified"] = U.evaluate(r, s)
    live = copy.deepcopy(r["unified"])
    high = SH.propose(r, s, [], {"u_watch_min": 99.0})
    assert high["decision"] == U.REJECT != live["decision"]
    assert r["unified"] == live and s.u_watch_min == 48.0


# =============================================================================================
# research goes on for coins rejected ONLY for an unverified PumpSwap LP (live stays REJECT)
# =============================================================================================
def test_continue_research_only_when_every_failure_is_the_lp_question(s):
    r = lp_only()
    g = r["gates"]
    assert SH.continue_research(g, r, s)
    assert not SH.continue_research(g, r, s.model_copy(update={"shadow_test_deep_research": False}))
    assert not SH.continue_research(g, r, s.model_copy(update={"shadow_test_rules": "holder_gate_warn"}))
    assert not SH.continue_research(gates(rugcheck_danger="FAIL", liquidity="FAIL"), r, s)
    assert not SH.continue_research(g, lp_only(dex="meteora"), s)
    assert not SH.continue_research(g, lp_only(lp_onchain_check="verified: 3.0% burned"), s)
    assert not SH.continue_research(gates(), r, s)


async def test_lp_only_rejection_is_fully_analysed_but_stays_rejected(settings, db):
    from test_decision_v25 import _pumpswap_only, _rc_lp_unlocked, dope_providers, research, DOPE
    from conftest import FakeRugCheck

    p = dope_providers(rugcheck_up=True, lp_read_fails=True)
    p.dex.pairs = _pumpswap_only()
    p.rugcheck = FakeRugCheck(_rc_lp_unlocked())
    out = await research(settings, db, p, DOPE)
    assert out.report.get("shadow_deep_research") is True
    assert out.action == Action.REJECT and out.report["final"]["reason_class"] == "SAFETY_FAIL"
    assert out.report["unified"]["hard_vetoes"], "the LP danger is still a hard veto live"
    assert out.report.get("holders"), "the analysis went on past the security stage"
    prop = SH.propose(out.report, settings, RULES, {})
    assert "lp_unverified_warn" in prop["shadow_rules_fired"] and prop["decision"] != U.STRONG
    rid = SH.record(db, out, settings)
    assert rid and rows(db)[0]["live_action"] == "REJECT"


async def test_without_deep_research_the_live_path_is_unchanged(settings, db):
    from test_decision_v25 import _pumpswap_only, _rc_lp_unlocked, dope_providers, research, DOPE
    from conftest import FakeRugCheck

    p = dope_providers(rugcheck_up=True, lp_read_fails=True)
    p.dex.pairs = _pumpswap_only()
    p.rugcheck = FakeRugCheck(_rc_lp_unlocked("Copycat token"))     # a second danger: no deep research
    out = await research(settings, db, p, DOPE)
    assert out.action == Action.REJECT and "shadow_deep_research" not in out.report
    assert "unified" not in out.report


# =============================================================================================
# recording
# =============================================================================================
def rows(db):
    return db.fetchall("SELECT * FROM shadow_tests ORDER BY id")


def test_recording_leaves_the_live_decision_untouched(db, s):
    out = outcome(holder_fail(), s)
    before = (out.action, out.score, out.risk, out.confidence, list(out.reasons), copy.deepcopy(out.report))
    rid = SH.record(db, out, s)
    assert rid
    assert (out.action, out.score, out.risk, out.confidence, list(out.reasons), out.report) == before
    row = rows(db)[0]
    assert (row["live_decision"], row["live_action"]) == (U.REJECT, "REJECT")
    assert row["prop_action"] == "WATCHLIST" and row["changed"] == 1
    assert row["live_class"] == "RISK_THRESHOLD"
    comp = json.loads(row["components"])
    assert set(comp["live"]["points"]) == set(U.CATEGORIES) and comp["proposed"]["completeness"] is not None
    assert json.loads(row["rules_fired"]) == ["holder_gate_warn"]
    assert json.loads(row["live_reasons"]) and json.loads(row["prop_reasons"])


def test_disabled_or_without_a_unified_result_records_nothing(db, s):
    out = outcome(report(), s)
    assert SH.record(db, out, s.model_copy(update={"shadow_test_enabled": False})) is None
    bare = ResearchOutcome(addr("pre"), Action.WATCHLIST, {"final": {"action": "WATCHLIST"}}, snapshot_id=9)
    assert SH.record(db, bare, s) is None


def test_one_row_per_snapshot_and_no_repeats_of_an_unchanged_coin(db, s):
    t = time.time()
    assert SH.record(db, outcome(holder_fail(), s, snap=1), s, now=t)
    assert SH.record(db, outcome(holder_fail(), s, snap=1), s, now=t + 1) is None      # same snapshot
    assert SH.record(db, outcome(holder_fail(), s, snap=2), s, now=t + 60) is None     # same decisions, 1 min later
    assert SH.record(db, outcome(report(), s, snap=3), s, now=t + 120)                # decision changed
    assert SH.record(db, outcome(report(), s, snap=4), s, now=t + 120 + 31 * 60)       # window passed
    assert SH.record(db, outcome(report(), s, snap=5, mint=addr("other")), s, now=t + 130)
    assert len(rows(db)) == 4


def test_a_different_proposal_gets_its_own_rows(db, s):
    out = outcome(holder_fail(), s)
    assert SH.record(db, out, s)
    assert SH.record(db, out, s.model_copy(update={"shadow_test_overrides": '{"u_watch_min": 40}'}))
    assert len({r["policy"] for r in rows(db)}) == 2


def test_engine_hook_links_the_learning_shadow_of_the_same_pass(db, s):
    L.init_schema(db)
    out = outcome(holder_fail(), s)
    old = db.execute("INSERT INTO shadow_calls(mint, ts, features) VALUES (?,?,?)", (out.mint, time.time() - 600, "{}"))
    since = time.time()
    new = db.execute("INSERT INTO shadow_calls(mint, ts, features) VALUES (?,?,?)", (out.mint, since + 0.01, "{}"))
    Engine._record_shadow_test(SimpleNamespace(s=s, db=db), out, since)
    assert rows(db)[0]["shadow_call_id"] == new != old


# =============================================================================================
# outcomes: no future data, only complete windows
# =============================================================================================
RUNNER = [[0, 1.05, 0.95, 1.0], [10, 1.6, 1.2, 1.5], [25, 2.3, 1.8, 2.1], [200, 1.4, 1.0, 1.1]]
RUG = [[0, 1.0, 0.95, 1.0], [5, 0.9, 0.2, 0.25], [200, 0.3, 0.1, 0.2]]
SHORT = [[0, 1.05, 0.9, 1.0], [20, 1.2, 0.85, 1.1]]


def _shadow_call(db, mint, ts, path):
    L.init_schema(db)
    return db.execute("INSERT INTO shadow_calls(mint, ts, features, evaluated, path) VALUES (?,?,?,1,?)",
                      (mint, ts, "{}", json.dumps(path)))


def _tracking(db, snap, mint, ts, peak, trough, done=1, samples=300):
    db.execute("INSERT INTO decision_tracking(snapshot_id, mint, action, decided_at, entry_price, max_price, "
               "min_price, samples, done) VALUES (?,?,?,?,?,?,?,?,?)",
               (snap, mint, "REJECT", ts, 1.0, peak, trough, samples, done))


def test_outcomes_wait_for_the_whole_window(db, s):
    t = time.time() - 3 * 3600                      # 3 hours ago: the 4h path is not complete
    out = outcome(holder_fail(), s)
    sc = _shadow_call(db, out.mint, t, RUNNER)
    SH.record(db, out, s, shadow_call_id=sc, now=t)
    assert SH.evaluate_due(db) == 0 and rows(db)[0]["outcome_label"] is None
    assert SH.evaluate_due(db, now=t + SH.EVAL_AFTER + 60) == 1
    row = rows(db)[0]
    assert (row["outcome_label"], row["outcome_src"], row["peak_x"]) == (B.RUNNER, "path", 2.3)


def test_a_path_that_started_before_the_decision_is_never_used(db, s):
    t = time.time() - 30 * 3600
    out = outcome(holder_fail(), s)
    sc = _shadow_call(db, out.mint, t - 3600, RUNNER)      # an earlier decision's path
    SH.record(db, out, s, shadow_call_id=sc, now=t)
    _tracking(db, out.snapshot_id, out.mint, t, 1.1, 0.2)
    SH.evaluate_due(db)
    row = rows(db)[0]
    assert (row["outcome_label"], row["outcome_src"]) == (B.RUG, "tracking")


def test_tracking_from_another_snapshot_is_never_used(db, s):
    t = time.time() - 30 * 3600
    out = outcome(holder_fail(), s, snap=7)
    SH.record(db, out, s, now=t)
    _tracking(db, 6, out.mint, t - 3600, 3.0, 0.9)          # same coin, earlier research
    SH.evaluate_due(db)
    row = rows(db)[0]
    assert row["outcome_label"] is None and row["outcome_src"] == "none"


def test_incomplete_evidence_is_not_a_label(db, s):
    t = time.time() - 5 * 3600
    out = outcome(holder_fail(), s)
    sc = _shadow_call(db, out.mint, t, SHORT)               # 20 minutes of path: INCOMPLETE
    SH.record(db, out, s, shadow_call_id=sc, now=t)
    _tracking(db, out.snapshot_id, out.mint, t, 1.2, 0.9, done=0, samples=20)
    SH.evaluate_due(db)
    row = rows(db)[0]
    assert row["outcome_label"] is None and row["outcome_src"] is None, "still waiting for the 24h tracking"
    db.execute("UPDATE decision_tracking SET done=1, samples=300 WHERE snapshot_id=?", (out.snapshot_id,))
    assert SH.evaluate_due(db) == 1 and rows(db)[0]["outcome_label"] == B.FLAT


def test_decision_columns_never_change_after_recording(db, s):
    t = time.time() - 30 * 3600
    out = outcome(holder_fail(), s)
    SH.record(db, out, s, shadow_call_id=_shadow_call(db, out.mint, t, RUG), now=t)
    keys = ("live_decision", "live_action", "live_score", "prop_decision", "prop_action", "prop_score", "components")
    before = {k: rows(db)[0][k] for k in keys}
    SH.evaluate_due(db)
    assert {k: rows(db)[0][k] for k in keys} == before and rows(db)[0]["outcome_label"] == B.RUG


# =============================================================================================
# the report
# =============================================================================================
def test_summary_compares_and_counts_each_coin_once(db, s):
    t = time.time() - 30 * 3600
    for i, (make, path) in enumerate(((holder_fail, RUNNER), (holder_fail, RUG), (report, RUNNER))):
        out = outcome(make(), s, snap=10 + i, mint=addr(f"c{i}"))
        SH.record(db, out, s, shadow_call_id=_shadow_call(db, out.mint, t, path), now=t)
    # the same coin again, an hour later, same decisions: one coin, one comparison
    again = outcome(holder_fail(), s, snap=20, mint=addr("c0"))
    SH.record(db, again, s, now=t + 3600)
    SH.evaluate_due(db)
    sm = SH.summary(db, days=7)
    assert sm["rows"] == 4 and sm["coins"] == 3 and sm["compared"] == 3
    assert sum(sm["matrix"][U.REJECT].values()) == 2 and U.REJECT not in sm["matrix"][U.REJECT]
    lifted = sm["live_reject_proposed_watch_or_better"]
    assert (lifted["n"], lifted[B.RUNNER], lifted[B.RUG]) == (2, 1, 1)
    assert sm["rules"]["holder_gate_warn"]["n"] == 2
    text = SH.format_summary(sm)
    assert "live REJECT -> proposed WATCH/call" in text and "live callouts are unchanged" in text


def test_empty_summary_is_readable(db):
    assert "nothing recorded yet" in SH.format_summary(SH.summary(db))


# =============================================================================================
# v2.5.5: call profiles, recorded next to the live decision as separate policies
# =============================================================================================
def test_policies_parse_and_each_gets_its_own_row(db, s):
    assert SH.parse_policies(SH.DEFAULT_RULES) == [["holder_gate_warn", "lp_unverified_warn"], ["pool_age_5m"],
                                                   ["liquidity_50k", "pool_age_5m"], ["deep_liquidity"]]
    assert SH.parse_policies("deep_liquidity; ;deep_liquidity,bogus") == [["deep_liquidity"]]
    multi = s.model_copy(update={"shadow_test_rules": SH.DEFAULT_RULES})
    assert SH.record(db, outcome(report(), multi), multi)
    assert len({r["policy"] for r in rows(db)}) == 4
    assert SH.record(db, outcome(report(), multi), multi) is None, "same snapshot: no second set of rows"


def test_a_call_below_the_profile_becomes_watch_and_nothing_else_changes(s):
    r = report()
    r["market"]["liquidity_usd"] = {"value": 60_000}
    r["security"]["pool_age_hours"] = 0.4
    r["unified"] = U.evaluate(r, s)
    assert r["unified"]["decision"] == U.STRONG
    live = copy.deepcopy(r["unified"])
    p = SH.propose(r, s, ["deep_liquidity", "pool_age_1h"], {})
    assert p["decision"] == U.WATCH and p["reason_class"] == "CALL_PROFILE"
    assert p["shadow_rules_fired"] == ["deep_liquidity", "pool_age_1h"]
    assert p["why"][0].startswith("call profile: liquidity $60,000")
    assert p["score"] == live["score"] and r["unified"] == live, "the live result is untouched"


def test_a_deep_old_call_passes_the_profile_and_profiles_never_lift_a_coin(s):
    r = report()
    r["market"]["liquidity_usd"] = {"value": 250_000}
    r["security"]["pool_age_hours"] = 5.0
    r["unified"] = U.evaluate(r, s)
    p = SH.propose(r, s, ["deep_liquidity", "pool_age_1h"], {})
    assert p["decision"] == U.STRONG and p["shadow_rules_fired"] == []
    weak = holder_fail()
    weak["unified"] = U.evaluate(weak, s)
    assert SH.propose(weak, s, ["deep_liquidity"], {})["decision"] == U.REJECT


def test_profile_fit_is_recorded_for_every_decision_without_changing_non_calls(s):
    r = holder_fail()                                         # a rejection
    r["market"]["liquidity_usd"] = {"value": 20_000}
    r["security"]["pool_age_hours"] = 0.02                    # about a minute old
    r["unified"] = U.evaluate(r, s)
    p = SH.propose(r, s, ["liquidity_50k", "pool_age_5m"], {})
    assert p["decision"] == r["unified"]["decision"] == U.REJECT
    assert p["shadow_profile"] == {"fits": False, "misses": ["liquidity_50k", "pool_age_5m"]}
    assert p["shadow_rules_fired"] == []                      # nothing was downgraded
    r["security"].pop("pool_age_hours")
    assert [k for k, _ in SH.profile_misses(r, ["pool_age_5m"])] == ["pool_age_5m"], "unknown age is not old enough"
    deep = report()
    deep["market"]["liquidity_usd"] = {"value": 80_000}
    deep["security"]["pool_age_hours"] = 0.5
    assert SH.profile_misses(deep, ["liquidity_50k", "pool_age_5m"]) == []


def test_pump_then_rug_is_not_a_clean_runner(db, s):
    pump_dump = [[0, 1.0, 0.95, 1.0], [10, 2.3, 1.5, 2.1], [30, 1.2, 0.2, 0.25], [200, 0.3, 0.1, 0.2]]
    assert SH.after_2x_low(pump_dump) == 0.1 and SH.after_2x_low(RUG) is None
    t = time.time() - 30 * 3600
    for i, (path, mint) in enumerate(((RUNNER, addr("clean")), (pump_dump, addr("dump")), (RUG, addr("rug")))):
        out = outcome(report(), s, snap=40 + i, mint=mint)
        SH.record(db, out, s, shadow_call_id=_shadow_call(db, out.mint, t, path), now=t)
    SH.evaluate_due(db)
    sm = SH.summary(db, days=7)
    allc = sm["live_calls"]
    assert (allc["n"], allc[B.RUNNER], allc["clean_runner"], allc["pump_then_rug"], allc[B.RUG]) == (3, 2, 1, 1, 1)
    assert "pump-then-rug" in SH.format_summary(sm) and sm["calls_per_day"]["live"] > 0


def test_profile_report_compares_admitted_and_excluded_coins(db, s):
    multi = s.model_copy(update={"shadow_test_rules": "liquidity_50k"})
    t = time.time() - 30 * 3600
    for i, (liq, path) in enumerate(((80_000, RUNNER), (20_000, RUG), (25_000, RUG))):
        r = report()
        r["market"]["liquidity_usd"] = {"value": liq}
        out = outcome(r, multi, snap=60 + i, mint=addr(f"p{i}"))
        SH.record(db, out, multi, shadow_call_id=_shadow_call(db, out.mint, t, path), now=t)
    SH.evaluate_due(db)
    sm = SH.summary(db, days=7)
    assert (sm["profile_fits"]["n"], sm["profile_fits"][B.RUNNER]) == (1, 1)
    assert (sm["profile_excludes"]["n"], sm["profile_excludes"][B.RUG]) == (2, 2)
    assert "excluded by the profile" in SH.format_summary(sm)


def test_old_shadow_table_gains_the_new_column(db):
    db.execute("CREATE TABLE shadow_tests (id INTEGER PRIMARY KEY, ts REAL, mint TEXT, snapshot_id INTEGER, "
               "policy TEXT, outcome_label TEXT)")
    SH.init_schema(db)
    assert "after_2x_low" in {r["name"] for r in db.fetchall("PRAGMA table_info(shadow_tests)")}
