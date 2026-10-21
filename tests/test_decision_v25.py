"""v2.5 scoring/decision audit: regressions for every defect found, plus the two real tokens.

Real-token cases
----------------
$DOPE  33jENUQnjoitkMFpvNJcSHyLAHFWJuHwT6E3E8CNpump
$WORTHLESS  GicxSBiZPMuGEUBmDDqhg2yrxgjDCY1Euof83Gi8pump

The pool numbers are DexScreener's public pair data as fetched on 2026-10-19 (pool addresses,
dex ids, DLMM labels, liquidity, volumes, transaction counts, price changes). Northstar's stored
reports for these coins were not available to this audit, so pool AGES are set relative to the
test clock (the coins are older now than when Northstar researched them), and the on-chain /
RugCheck / Jupiter parts use the MOCK providers from conftest. Where a $WORTHLESS case uses
numbers "at research time" they are a reconstruction of the reported situation (severe decline,
possible wash trading, ~9% impact on a $1,000 sell) and are labelled as such. Nothing here is
live validation.
"""
from __future__ import annotations

import asyncio
import base64
import importlib
import sys

import pytest

from conftest import (FakeRpc, FakeRugCheck, NOW, addr, make_pair, mint_account, rc_report,
                      seed_profile, strong_providers, swap_tx)
from northstar.analysis import unified as U
from northstar.analysis.lp_onchain import PUMPSWAP_PROGRAM
from northstar.analysis.market import analyze_market
from northstar.analysis.security import lp_gate, resolve_lp_floor
from northstar.config import Settings
from northstar.final import final_view
from northstar.models import Action, GateStatus, RiskLevel
from northstar.net import ProviderError
from northstar.research import Researcher
from northstar import telemetry as T

from test_lp_onchain import LP_MINT, T22, pool_bytes
from test_unified_engine import gates, report

WSOL = "So11111111111111111111111111111111111111112"


@pytest.fixture
def s() -> Settings:
    return Settings(_env_file=None, discord_token="t", discord_channel_id=1, discord_vip_channel_id=2,
                    min_liquidity_usd=15_000, min_narrative_evidence_items=0,
                    min_volume_acceleration=0, min_buy_sell_ratio_h1=0)


# =============================================================================================
# 1. One source of truth for the score (the "41.2 vs 58/100" report)
# =============================================================================================
def test_final_view_is_the_only_score_risk_and_action(s):
    u = U.evaluate(report(), s)
    v = final_view(u)
    assert v["score"] == u["score"]
    assert v["action"] == Action.AUTO_POST
    assert v["risk"] == RiskLevel.LOW_DETECTED
    assert v["reasons"][0].startswith(f"{U.STRONG} — score {u['score']:.0f}/100")


def test_publication_policy_holds_a_call_but_never_creates_one(s):
    u = U.evaluate(report(), s)
    held = final_view(u, ["daily alert cap reached"])
    assert held["action"] == Action.WATCHLIST and held["held_by_policy"]
    assert held["reason_class"] == "POLICY_HOLD"
    weak = U.evaluate(report(market_structure={"status": "OBSERVED", "fading": True}), s)
    assert final_view(weak, [])["action"] != Action.AUTO_POST


def test_rug_check_no_longer_prints_a_second_out_of_100_number(s):
    from northstar.analysis.verdict import embed_text
    text = embed_text({"level": "CAUTION", "score": 41, "headline": "x", "red": [], "green": []})
    assert "/100" not in text and "rug-risk points 41" in text


# =============================================================================================
# 2. Missing optional data restricts; it does not reject or raise risk
# =============================================================================================
def test_too_few_narrative_items_is_a_watch_not_a_reject(s):
    s.min_narrative_evidence_items = 3
    r = report(narrative={"evidence_items": 2, "meta_status": "OBSERVED", "meta_match": True,
                          "social_status": "OBSERVED", "competing_tokens": 0})
    u = U.evaluate(r, s)
    assert u["decision"] == U.WATCH, "missing narrative evidence is not a reason to reject"
    assert u["reason_class"] == U.RC_GAP
    assert u["risk"] == U.RISK_LOW, "a data gap must not raise the risk class"
    assert u["confidence"] != "HIGH"
    assert not u["warnings"] and u["evidence_gaps"]


def test_narrative_blocks_switch_restores_v24_behaviour(s):
    s.min_narrative_evidence_items = 3
    s.u_narrative_blocks = True
    r = report(narrative={"evidence_items": 1, "meta_status": "OBSERVED"})
    assert U.evaluate(r, s)["decision"] == U.REJECT


def test_unverified_trade_sample_is_an_evidence_gap_not_a_warning(s):
    r = report(trades={"status": "INFERRED", "sample_size": 120, "buys": 90, "sells": 30,
                       "unique_buyers": 70, "buyer_trend": "rising"})
    u = U.evaluate(r, s)
    assert u["decision"] == U.WATCH
    assert not u["warnings"] and u["risk"] == U.RISK_LOW
    assert any("not verified on-chain" in g for g in u["evidence_gaps"])


def test_two_missing_data_items_no_longer_add_up_to_high_risk(s):
    s.min_narrative_evidence_items = 3
    r = report(trades={"status": "INFERRED", "sample_size": 120, "buys": 90, "sells": 30,
                       "unique_buyers": 70, "buyer_trend": "rising"},
               narrative={"evidence_items": 1, "meta_status": "OBSERVED"})
    u = U.evaluate(r, s)
    assert u["decision"] == U.WATCH and u["risk"] == U.RISK_LOW   # v2.4: CAUTION + HIGH risk


def test_an_extended_move_is_timing_not_rejection(s):
    r = report()
    r["market_structure"]["extended"] = True
    u = U.evaluate(r, s)
    assert u["decision"] == U.WATCH and u["reason_class"] == U.RC_TIMING
    assert u["early_entry"]["grade"] == U.LATE


def test_a_provider_outage_does_not_move_the_opportunity_score(s):
    up = U.evaluate(report(), s)
    down_report = report(gates=gates(rugcheck_danger="UNKNOWN"))
    down = U.evaluate(down_report, s)
    assert down["score"] == pytest.approx(up["score"], abs=0.05), \
        "a RugCheck outage is a confidence problem, not an opportunity problem"
    assert down["categories"]["safety"]["coverage"] < up["categories"]["safety"]["coverage"]


def test_v24_score_formula_is_still_available(s):
    s.u_score_includes_safety = True
    u = U.evaluate(report(), s)
    assert u["categories"]["safety"]["points"] > 0 and u["score_basis"].startswith("v2.4")


# =============================================================================================
# 3. Critical unknowns are never treated as safe, and their cause is told apart
# =============================================================================================
def test_lp_unknown_because_rugcheck_failed_is_a_provider_failure(s):
    r = report(gates=gates(lp_lock="UNKNOWN"))
    r["security"]["lp_status"] = "UNKNOWN"
    r["sources"] = {"rugcheck": "FAILED: 503"}
    u = U.evaluate(r, s)
    assert u["decision"] == U.INSUFFICIENT and u["reason_class"] == U.RC_PROVIDER
    assert "will retry" in u["why"][0]


def test_lp_unknown_with_providers_up_is_a_critical_unknown(s):
    r = report(gates=gates(lp_lock="UNKNOWN"))
    r["security"]["lp_status"] = "UNKNOWN"
    u = U.evaluate(r, s)
    assert u["decision"] == U.INSUFFICIENT and u["reason_class"] == U.RC_UNMEASURED


def test_unknown_checks_lower_completeness_instead_of_vanishing(s):
    r = report(gates=gates(lp_lock="UNKNOWN"))
    r["security"]["lp_status"] = "UNKNOWN"
    u = U.evaluate(r, s)
    lp = next(c for c in u["checks"] if c["key"] == "clean:lp")
    assert lp["grade"] == U.UNKNOWN
    assert u["completeness"] < U.evaluate(report(), s)["completeness"]


# =============================================================================================
# 4. Double counting
# =============================================================================================
def test_a_hazard_and_its_scoring_check_count_once(s):
    r = report(verdict={"level": "CAUTION", "score": 10, "red": [
        {"key": "dev_holds", "level": "medium", "text": "dev holds 4.0%"}], "green": [], "unknown": []})
    r["holders"]["creator_pct"] = 4.0
    u = U.evaluate(r, s)
    keys = [c["key"] for c in u["checks"]]
    assert "hold:creator" not in keys and "hold:creator" in u["absorbed"]
    assert [v["key"] for v in u["vetoes"]].count("haz:dev_holdings") == 1


def test_wash_trading_seen_by_two_detectors_is_one_warning(s):
    r = report(verdict={"level": "HIGH RISK", "score": 50, "red": [
        {"key": "possible_wash_trading", "level": "high", "text": "possible wash trading"},
        {"key": "volume_liquidity_implausible", "level": "high", "text": "volume implausible"}],
        "green": [], "unknown": []})
    u = U.evaluate(r, s)
    assert len(u["warnings"]) == 1, "one phenomenon, one warning"
    assert u["decision"] == U.WATCH          # v2.4 counted two warnings -> CAUTION


def test_the_narrative_cap_actually_binds(s):
    """v2.4's cap (20) exceeded narrative's maximum (16) and could never bind."""
    r = report()
    u = U.evaluate(r, s)
    ex = u["score_ex_narrative"]
    s.u_strong_min = ex + s.u_narrative_cap + 1.0     # needs more narrative help than allowed
    s.u_watch_min = 10
    capped = U.evaluate(r, s)
    if capped["score"] >= s.u_strong_min:
        assert capped["decision"] != U.STRONG
        assert any("leans too heavily on narrative" in w for w in capped["why"])


# =============================================================================================
# 5. Exit cost, collapse, early entry
# =============================================================================================
def test_nine_percent_impact_on_a_thousand_dollar_sell_is_a_serious_warning(s):
    r = report(sellability={"checked": [{"usd": 100}, {"usd": 500}, {"usd": 1000}],
                            "worst_impact_pct": 9.0, "max_sellable_usd": 1000.0,
                            "no_route_at_usd": None})
    u = U.evaluate(r, s)
    assert u["decision"] != U.STRONG
    assert any("costs 9.0% in price impact" in w for w in u["warnings"])


def test_ruinous_exit_impact_disqualifies(s):
    r = report(sellability={"checked": [{"usd": 100}, {"usd": 500}, {"usd": 1000}],
                            "worst_impact_pct": 22.0, "max_sellable_usd": 1000.0,
                            "no_route_at_usd": None})
    assert U.evaluate(r, s)["decision"] == U.REJECT


def test_market_structure_detects_a_collapse(s):
    pair = make_pair(price_change={"m5": 2.8, "h1": -96.4, "h6": -94.5, "h24": -94.5})
    ms = analyze_market(pair, [], s)
    assert ms.collapsed and "collapsed" in ms.collapse_text and ms.entry_quality == "POOR"
    healthy = analyze_market(make_pair(price_change={"m5": 1, "h1": 6.5, "h6": -12.4, "h24": -34.6}), [], s)
    assert not healthy.collapsed, "a -35% day is a pullback, not a collapse"


def test_a_collapsed_coin_is_rejected_whatever_else_looks_good(s):
    r = report()
    r["market_structure"].update(collapsed=True, collapse_text="price collapsed: -96% in 1h")
    u = U.evaluate(r, s)
    u = U.apply_ai(u, r, {"verdict": "CALL", "score": 95}, s)
    u = U.apply_model(u, r, 0.9, True, baseline=0.15, s=s)
    assert u["decision"] == U.REJECT and u["early_entry"]["grade"] == U.COLLAPSED


def test_early_entry_grades(s):
    young = report()
    young["market"].update(price_change={"h1": 20.0, "h6": 40.0}, pair_created_at=NOW - 2 * 3600,
                           fetched_at=NOW)
    assert U.early_entry(young, s)["grade"] == U.EARLY
    old = report()
    old["market"].update(price_change={"h1": 5.0, "h6": 30.0}, pair_created_at=NOW - 40 * 3600,
                         fetched_at=NOW)
    assert U.early_entry(old, s)["grade"] == U.ON_TIME
    blind = report()
    blind["market"]["price_change"] = {}
    assert U.early_entry(blind, s)["grade"] == U.ENTRY_UNKNOWN


# =============================================================================================
# 6. LP: PASS / FAIL / UNKNOWN stay distinct, and LP safety is judged across all pools
# =============================================================================================
def test_lp_gate_semantics():
    assert lp_gate("BURNED — 100%", 100.0, 50_000, 25_000).status == GateStatus.PASS
    assert lp_gate("PARTIAL — 40% locked", 40.0, 50_000, 25_000).status == GateStatus.FAIL
    assert lp_gate(None, None, 50_000, 25_000).status == GateStatus.UNKNOWN
    assert lp_gate("CONCENTRATED POSITION — no LP token", 0.0, 50_000, 25_000).status == GateStatus.UNKNOWN


def test_lp_floor_across_pools():
    burned = {"address": "a", "kind": "BURNED", "liquidity_usd": 237_122, "locked_pct": 100, "verified": True}
    dlmm = {"address": "b", "kind": "CONCENTRATED", "liquidity_usd": 405_891, "locked_pct": 0.0}
    unk = {"address": "c", "kind": "UNKNOWN", "liquidity_usd": 60_000}
    ok = resolve_lp_floor([burned, dlmm], 25_000)
    assert ok["status"] == "PASS" and ok["reason"].startswith("LOCKED FLOOR")
    assert ok["withdrawable_share"] == pytest.approx(405_891 / 643_013, abs=1e-3)
    only_dlmm = resolve_lp_floor([dlmm], 25_000)
    assert only_dlmm["status"] == "FAIL", "all liquidity withdrawable is a measured hazard"
    assert resolve_lp_floor([dlmm, unk], 25_000)["status"] == "UNKNOWN"
    assert resolve_lp_floor([{**burned, "liquidity_usd": 10_000}, dlmm], 25_000)["status"] == "FAIL"


# --------------------------------------------------------------------------------------- $DOPE
DOPE = "33jENUQnjoitkMFpvNJcSHyLAHFWJuHwT6E3E8CNpump"
DOPE_PUMPSWAP = "6iep3EgqksbZwgzMBCyAJtiVtVYkCgL9BnPBEwLR8AGQ"
DOPE_DLMM = "FpRcdzEgiHLpo4LPTWDkhq86fCuwoejFXnmoH9cSZZ6Q"


def dope_pairs(include_pumpswap: bool = True):
    common = dict(base_mint=DOPE, base_symbol="DOPE", base_name="Doge Pepe", quote_mint=WSOL, quote_symbol="SOL")
    pairs = [make_pair(pair_address=DOPE_DLMM, dex_id="meteora", labels=["DLMM"], price_usd=0.003337,
                       liquidity_usd=405_891.28, market_cap=3_296_801, fdv=3_296_801,
                       vol={"m5": 588.19, "h1": 29_119.36, "h6": 193_083.25, "h24": 752_562.52},
                       txns={"m5": {"buys": 0, "sells": 19}, "h1": {"buys": 77, "sells": 31},
                             "h6": {"buys": 280, "sells": 318}, "h24": {"buys": 1376, "sells": 1276}},
                       price_change={"m5": -0.94, "h1": 5.57, "h6": -11.82, "h24": -36.03},
                       pair_created_at=NOW - 2 * 3600, **common)]
    if include_pumpswap:
        pairs.append(make_pair(pair_address=DOPE_PUMPSWAP, dex_id="pumpswap", labels=[], price_usd=0.003383,
                               liquidity_usd=237_121.88, market_cap=3_340_114, fdv=3_340_114,
                               vol={"m5": 422.12, "h1": 21_339.62, "h6": 164_138.53, "h24": 844_794.93},
                               txns={"m5": {"buys": 3, "sells": 7}, "h1": {"buys": 84, "sells": 127},
                                     "h6": {"buys": 1019, "sells": 627}, "h24": {"buys": 4473, "sells": 3757}},
                               price_change={"m5": 1.05, "h1": 6.46, "h6": -12.37, "h24": -34.61},
                               pair_created_at=NOW - 6 * 3600, **common))
    return pairs


class DopeRpc(FakeRpc):
    """MOCK chain for $DOPE: the PumpSwap pool account decodes with the verified layout."""

    def __init__(self, burned: bool = True, lp_read_fails: bool = False, **kw):
        holders = [(DOPE_PUMPSWAP, 3.5), (DOPE_DLMM, 1.5)] + [
            (addr(f"dope{i}"), p) for i, p in enumerate([2.1, 1.9, 1.7, 1.6, 1.5, 1.4, 1.3, 1.2, 1.1, 1.0,
                                                        0.9, 0.9, 0.8, 0.8, 0.7, 0.7, 0.6, 0.6])]
        trades = [swap_tx(addr(f"dtrader{i}"), "buy" if i % 4 != 3 else "sell", 50_000 + i * 900,
                          5000 + i * 3, int(NOW - 3000) + i * 25, mint=DOPE, pool_owner=DOPE_PUMPSWAP)
                  for i in range(120)]
        super().__init__(holders=holders, trades=trades, **kw)
        self.lp_read_fails = lp_read_fails
        current = 0 if burned else 4_193_388_318_622 // 2
        self.extra = {DOPE_PUMPSWAP: {"owner": PUMPSWAP_PROGRAM,
                                      "data": [base64.b64encode(pool_bytes(DOPE)).decode(), "base64"]},
                      LP_MINT: {"owner": T22, "data": {"parsed": {"info": {"supply": str(current),
                                                                           "decimals": 9}}}}}

    async def get_account_info(self, a):
        if a in self.extra:
            if self.lp_read_fails:
                raise ProviderError("solana_rpc", "MOCK: LP account read failed")
            return self.extra[a]
        return await super().get_account_info(a)

    async def get_signatures(self, address, limit=100, before=None):
        if address == DOPE_PUMPSWAP:
            txs = sorted(self.trades, key=lambda t: -t["slot"])
            return [{"signature": t["transaction"]["signatures"][0], "slot": t["slot"], "err": None,
                     "blockTime": t["blockTime"]} for t in txs][:limit]
        return await super().get_signatures(address, limit, before)


def dope_providers(rugcheck_up: bool = False, **rpc):
    p = strong_providers()
    p.dex.pairs = dope_pairs(rpc.pop("include_pumpswap", True))
    p.rpc = DopeRpc(**rpc)
    p.rugcheck = (FakeRugCheck(rc_report(mint=DOPE, price=0.00335, markets=[]))
                  if rugcheck_up else FakeRugCheck(fail=ProviderError("rugcheck", "MOCK outage")))
    return p


async def research(settings, db, providers, mint):
    seed_profile(db, mint)
    return await Researcher(settings, db, providers).research(mint)


async def test_dope_concentrated_liquidity_is_resolved_by_the_verified_burned_floor(settings, db):
    out = await research(settings, db, dope_providers(), DOPE)
    gate = next(g for g in out.report["gates"] if g["name"] == "lp_lock")
    assert out.report["pool"]["address"] == DOPE_DLMM, "the DLMM pool is still the deepest"
    assert gate["status"] == "PASS" and "LOCKED FLOOR" in gate["reason"] and "verified on-chain" in gate["reason"]
    lb = out.report["liquidity_breakdown"]
    assert lb["locked_usd"] == pytest.approx(237_121.88, abs=1)
    assert lb["withdrawable_share"] == pytest.approx(0.631, abs=0.01)
    u = out.report["unified"]
    assert u["decision"] != U.INSUFFICIENT, "the LP question is answered, so it is no longer 'unknown'"
    assert any("owner-withdrawable" in t for t in u["suspicions"]), "the withdrawable share stays visible"
    # trade sample comes from the decodable PumpSwap pool, not the DLMM pool
    assert out.report["trade_pool"]["address"] == DOPE_PUMPSWAP
    assert out.report["trades"]["status"] == "VERIFIED"
    # ONE score: what is stored, shown and acted on is the unified score
    assert out.score == u["score"] == out.report["final"]["score"]
    snap = db.fetchone("SELECT score, action, risk FROM research_snapshots WHERE id=?", (out.snapshot_id,))
    assert snap["score"] == out.score and snap["action"] == out.action.value
    assert out.risk.value == snap["risk"] == out.report["risk"]


async def test_dope_is_not_approved_when_the_floor_cannot_be_read(settings, db):
    out = await research(settings, db, dope_providers(lp_read_fails=True), DOPE)
    assert out.action == Action.INSUFFICIENT_DATA
    gate = next(g for g in out.report["gates"] if g["name"] == "lp_lock")
    assert gate["status"] == "UNKNOWN"
    assert out.report["final"]["reason_class"] in (U.RC_PROVIDER, U.RC_UNMEASURED)


async def test_dope_with_only_withdrawable_liquidity_is_rejected(settings, db):
    out = await research(settings, db, dope_providers(include_pumpswap=False), DOPE)
    assert out.action == Action.REJECT
    gate = next(g for g in out.report["gates"] if g["name"] == "lp_lock")
    assert gate["status"] == "FAIL" and "can be withdrawn" in gate["reason"]


async def test_dope_half_burned_floor_is_not_counted_as_locked(settings, db):
    out = await research(settings, db, dope_providers(burned=False), DOPE)
    gate = next(g for g in out.report["gates"] if g["name"] == "lp_lock")
    # 50% of $237K burned = $118K locked, still above the $25K floor -> PASS, but the
    # withdrawable share is now higher and shown
    assert gate["status"] == "PASS"
    assert out.report["liquidity_breakdown"]["withdrawable_share"] > 0.8
    # but someone holds the unburned half of the PumpSwap LP: a serious warning, never a call
    assert "LP_NOT_LOCKED" in {f["code"] for f in out.report["flags"]}
    assert out.report["unified"]["decision"] != U.STRONG
    assert any("LP" in w for w in out.report["unified"]["warnings"])


async def test_research_summary_shows_one_score(settings, db, monkeypatch):
    from test_commands import fake_discord
    for name, mod in fake_discord().items():
        monkeypatch.setitem(sys.modules, name, mod)
    sys.modules.pop("northstar.discord_bot.commands", None)
    module = importlib.import_module("northstar.discord_bot.commands")
    try:
        out = await research(settings, db, dope_providers(), DOPE)
        _, desc, _ = module.research_summary(out)
        u = out.report["unified"]
        assert f"**Score:** {u['score']:.0f}/100" in desc
        legacy = out.report["score"]["total"]
        assert f"**Score:** {legacy}" not in desc, "the legacy v2.3 score must not be shown as THE score"
        assert "**Safety:**" in desc and "**Entry timing:**" in desc
    finally:
        sys.modules.pop("northstar.discord_bot.commands", None)


# ---------------------------------------------------------------------------------- $WORTHLESS
WORTHLESS = "GicxSBiZPMuGEUBmDDqhg2yrxgjDCY1Euof83Gi8pump"
WORTHLESS_POOL = "He6Ps3koVHQHpvgfuiugua4bkvb9bzcnS1qyA4FRNAcc"


def worthless_pair(**over):
    base = dict(pair_address=WORTHLESS_POOL, dex_id="pumpswap", labels=[], base_mint=WORTHLESS,
                base_symbol="WORTHLESS", base_name="Worthless Coin", quote_mint=WSOL, quote_symbol="SOL",
                pair_created_at=NOW - 3 * 3600)
    base.update(over)
    return make_pair(**base)


async def test_worthless_as_seen_today_is_rejected_by_the_prefilter(settings, db):
    """DexScreener 2026-10-19: $2,695 liquidity, -96% in 1h, $403K 1h volume."""
    p = strong_providers()
    p.dex.pairs = [worthless_pair(price_usd=0.00000239, liquidity_usd=2_695.48, market_cap=2_280, fdv=2_280,
                                  vol={"m5": 7.42, "h1": 403_047.4, "h6": 1_143_471.02, "h24": 1_143_471.02},
                                  txns={"m5": {"buys": 3, "sells": 1}, "h1": {"buys": 2162, "sells": 2408},
                                        "h6": {"buys": 7648, "sells": 6926}, "h24": {"buys": 7648, "sells": 6926}},
                                  price_change={"m5": 2.82, "h1": -96.44, "h6": -94.51, "h24": -94.51})]
    out = await research(settings, db, p, WORTHLESS)
    assert out.action == Action.REJECT
    assert out.report["final"]["reason_class"] == "PREFILTER"


def worthless_at_research_time() -> dict:
    """RECONSTRUCTION of the reported situation (not stored data): a coin already falling hard,
    flagged for possible wash trading, where a $1,000 sell costs about 9% in price impact."""
    r = report(
        sellability={"checked": [{"usd": 100}, {"usd": 500}, {"usd": 1000}], "worst_impact_pct": 9.1,
                     "max_sellable_usd": 1000.0, "no_route_at_usd": None},
        verdict={"level": "HIGH RISK", "score": 50, "red": [
            {"key": "possible_wash_trading", "level": "high", "text": "possible wash trading"},
            {"key": "volume_liquidity_implausible", "level": "high",
             "text": "volume is implausible for the liquidity (wash trading?)"},
            {"key": "price_collapse", "level": "high", "text": "the price has already collapsed"}],
            "green": [], "unknown": []})
    r["market"]["price_change"] = {"m5": -8.0, "h1": -62.0, "h6": -71.0, "h24": -71.0}
    # flow taken from DexScreener's 1h window for this pool (2,162 buys / 2,408 sells; 1h volume
    # about 2.1x its 6h hourly pace); the buyer trend is part of the reconstruction
    r["market_structure"].update(collapsed=True, constructive=False, consolidating=False,
                                 buy_sell_ratio_h1=0.898, volume_acceleration=2.11,
                                 collapse_text="price collapsed: -62% in 1h, -71% in 6h")
    r["trades"].update(buyer_trend="declining", unique_buyers=38, buys=90, sells=110)
    return r


def test_worthless_at_research_time_is_never_a_call(s):
    r = worthless_at_research_time()
    u = U.evaluate(r, s)
    assert u["decision"] == U.REJECT
    assert u["risk"] in (U.RISK_HIGH, U.RISK_EXTREME)
    assert u["early_entry"]["grade"] == U.COLLAPSED
    assert u["safety"]["status"] == "FAIL"
    assert u["categories"]["structure"]["quality"] < 0.6, "a collapsing chart is a weak opportunity too"
    assert any("9.1% in price impact" in t for t in u["warnings"] + u["blocking"])
    # wash trading reported twice is still ONE hazard
    assert [v["key"] for v in u["vetoes"]].count("haz:wash") == 1
    # and nothing upstream can rescue it
    rescued = U.apply_model(U.apply_ai(u, r, {"verdict": "CALL", "score": 99}, s), r, 0.95, True,
                            baseline=0.15, s=s)
    assert rescued["decision"] == U.REJECT


def test_worthless_without_the_collapse_is_still_not_a_call(s):
    """Even if the price windows had not shown the collapse, wash trading + 9% exit cost cap it."""
    r = worthless_at_research_time()
    r["market_structure"].update(collapsed=False)
    r["verdict"]["red"] = [x for x in r["verdict"]["red"] if x["key"] != "price_collapse"]
    u = U.evaluate(r, s)
    assert u["decision"] in (U.CAUTION, U.REJECT)
    assert len(u["warnings"]) >= 2


# =============================================================================================
# 7. Legitimate opportunity still gets through; conflicting providers no longer hard-reject
# =============================================================================================
async def test_a_provider_price_disagreement_blocks_the_call(settings, db):
    """v2.5.2 (operator decision): back to blocking. v2.5.0 had made it a confidence note, but on
    the live history coins rejected for it rugged 64% of the time (23 of 36)."""
    p = strong_providers()
    p.rugcheck = FakeRugCheck(rc_report(price=0.0040))          # DexScreener says 0.0012
    out = await research(settings, db, p, addr("mockmint"))
    gate = next(g for g in out.report["gates"] if g["name"] == "source_consistency")
    assert gate["status"] == "FAIL"
    assert out.action == Action.REJECT
    assert out.report["final"]["reason_class"] == "RISK_THRESHOLD", "a risk threshold, not a proven trap"


async def test_strong_coin_is_still_called(settings, db):
    out = await research(settings, db, strong_providers(), addr("mockmint"))
    assert out.action == Action.AUTO_POST
    assert out.report["final"]["safety"] == "PASS"
    assert out.report["final"]["reason_class"] == U.RC_CALL


async def test_dangerous_coin_with_great_story_is_rejected(settings, db):
    p = strong_providers()
    p.rpc.account = mint_account(mint_auth=addr("minter"))
    out = await research(settings, db, p, addr("mockmint"))
    assert out.action == Action.REJECT
    assert out.report["final"]["reason_class"] == "SAFETY_FAIL"


# =============================================================================================
# 8. Telemetry
# =============================================================================================
async def test_funnel_counts_every_stage_with_reasons(settings, db):
    good = await research(settings, db, strong_providers(), addr("mockmint"))
    T.record_outcome(db, good)
    T.record_publish(db, good.mint, True, None, good.score)
    bad_p = strong_providers()
    bad_p.rpc.account = mint_account(freeze_auth=addr("freezer"))
    bad = await research(settings, db, bad_p, addr("mockmint"))
    T.record_outcome(db, bad)
    sm = T.summary(db, 1)
    assert sm["approved"] == 1 and sm["called"] == 1
    assert sm["decisions"]["REJECT"] == 1
    assert sm["by_reason_class"]["REJECT"] == {"SAFETY_FAIL": 1}
    assert "freeze" in sm["top_reasons"]["REJECT"][0]["example"]
    assert "REJECT" in T.format_summary(sm)


async def test_engine_keeps_action_score_and_snapshot_consistent(settings, db):
    """The engine's live-context / brain / AI passes must leave ONE decision everywhere."""
    from conftest import MINT, MemoryPublisher
    from northstar.engine import Engine

    p = strong_providers()

    async def profiles():
        return [{"chainId": "solana", "tokenAddress": MINT,
                 "description": "Mock Frog is a community frog meme on Solana with daily art drops.",
                 "links": [{"type": "twitter", "url": "https://x.com/mockfrogsol"},
                           {"type": "telegram", "url": "https://t.me/mockfrogportal"}]}]

    p.dex.latest_profiles = profiles
    settings.discovery_interval_seconds = 1
    settings.monitor_interval_seconds = 1
    pub = MemoryPublisher()
    eng = Engine(settings, db, p, pub)
    task = asyncio.create_task(eng.run())
    for _ in range(100):
        await asyncio.sleep(0.05)
        if pub.sent:
            break
    eng.request_stop()
    await asyncio.wait_for(task, timeout=5)
    cand = db.get_candidate(MINT)
    snap = db.fetchone("SELECT score, action, report FROM research_snapshots WHERE mint=? ORDER BY id DESC",
                       (MINT,))
    import json
    rep = json.loads(snap["report"])
    assert snap["score"] == cand["last_score"] == rep["unified"]["score"] == rep["final"]["score"]
    assert snap["action"] == rep["final"]["action"] == U.LEGACY_ACTION[rep["unified"]["decision"]]
    alert = db.fetchone("SELECT score FROM alerts WHERE mint=?", (MINT,))
    assert alert is None or alert["score"] == snap["score"]
    sm = T.summary(db, 1)
    assert sm["researched"] >= 1 and sm["discovered"] >= 1


async def test_dashboard_funnel_view_reads_the_same_numbers(settings, db):
    import sqlite3
    from northstar.dashboard import _Ro

    out = await research(settings, db, strong_providers(), addr("mockmint"))
    T.record_outcome(db, out)
    c = sqlite3.connect(f"file:{db.path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        sm = T.summary(_Ro(c), 1)
    finally:
        c.close()
    assert sm["approved"] == 1 and sm["researched"] == 1


# =============================================================================================
# 9. RugCheck "Large Amount of LP Unlocked" vs Northstar's own on-chain burn read
#    (live log 2026-10-19: this danger alone hard-rejected coins before any analysis)
# =============================================================================================
def _rc_lp_unlocked(*extra_dangers):
    risks = [{"name": "Large Amount of LP Unlocked", "level": "danger"}]
    risks += [{"name": n, "level": "danger"} for n in extra_dangers]
    return rc_report(mint=DOPE, price=0.00338, risks=risks,
                     markets=[{"pubkey": DOPE_PUMPSWAP, "marketType": "pump_amm", "lp": {"lpLockedPct": 0.0}}])


def _pumpswap_only():
    # one pool only; market cap lowered so the liquidity/market-cap ratio gate passes and the
    # test isolates the RugCheck LP question
    pairs = [p for p in dope_pairs() if p.pair_address == DOPE_PUMPSWAP]
    for p in pairs:
        p.market_cap = p.fdv = 1_500_000
    return pairs


async def test_rugcheck_lp_danger_contradicted_by_onchain_burn_is_a_conflict(settings, db):
    p = dope_providers(rugcheck_up=True)
    p.dex.pairs = _pumpswap_only()
    p.rugcheck = FakeRugCheck(_rc_lp_unlocked())
    out = await research(settings, db, p, DOPE)
    gates_ = {g["name"]: g for g in out.report["gates"]}
    assert gates_["lp_lock"]["status"] == "PASS" and "VERIFIED on-chain" in gates_["lp_lock"]["reason"]
    assert gates_["rugcheck_danger"]["status"] == "PASS"
    assert out.report["final"]["reason_class"] != "SAFETY_FAIL"
    assert any("LP Unlocked" in c for c in out.report["unified"]["conflicts"])
    assert out.report["unified"]["confidence"] != "HIGH"


async def test_rugcheck_lp_danger_stands_without_an_onchain_read(settings, db):
    p = dope_providers(rugcheck_up=True, lp_read_fails=True)
    p.dex.pairs = _pumpswap_only()
    p.rugcheck = FakeRugCheck(_rc_lp_unlocked())
    out = await research(settings, db, p, DOPE)
    assert out.action == Action.REJECT
    # v2.5.3: with shadow testing on, this coin is analysed to the end (shadow_test.continue_research)
    # and rejected by the unified engine's hard veto ("rugcheck danger: ..."), not the early gate exit
    assert out.report["final"]["reason_class"] == "SAFETY_FAIL"
    assert any(k in r for r in out.reasons for k in ("rugcheck_danger", "rugcheck danger", "lp_lock", "lp lock"))


async def test_other_rugcheck_dangers_still_fail_the_gate(settings, db):
    p = dope_providers(rugcheck_up=True)
    p.dex.pairs = _pumpswap_only()
    p.rugcheck = FakeRugCheck(_rc_lp_unlocked("Copycat token"))
    out = await research(settings, db, p, DOPE)
    gate = next(g for g in out.report["gates"] if g["name"] == "rugcheck_danger")
    assert gate["status"] == "FAIL" and "Copycat" in gate["reason"] and "LP Unlocked" not in gate["reason"]
    assert out.action == Action.REJECT
