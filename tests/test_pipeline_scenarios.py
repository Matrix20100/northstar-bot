"""End-to-end research scenarios on MOCK data (no network). Not live validation."""
from __future__ import annotations

import base64
import struct
import time

from conftest import (CREATOR, MINT, NOW, POOL, FakeRpc, FakeRugCheck, addr, healthy_holders, healthy_trades,
                      make_pair, mint_account, rc_report, seed_profile, strong_providers, swap_tx)
from northstar.analysis.decision import decide
from northstar.models import Action, Confidence, GateResult, GateStatus, RiskLevel
from northstar.net import ProviderError, RateLimited
from northstar.providers.solana_rpc import TOKEN_2022_PROGRAM
from northstar.research import PUMP_CURVE_DISCRIMINATOR, PUMP_FUN_PROGRAM, Researcher


async def run(settings, db, providers, mint=MINT, profile=True):
    if profile:
        seed_profile(db, mint)
    return await Researcher(settings, db, providers).research(mint)


def flag_codes(out):
    return {f["code"] for f in out.report.get("flags", [])}


def gate(out, name):
    return next((g for g in out.report.get("gates", []) if g["name"] == name), None)


# 1 -----------------------------------------------------------------------------------------
async def test_strong_candidate_qualifies(settings, db):
    out = await run(settings, db, strong_providers())
    assert out.action == Action.AUTO_POST
    # v2.5: out.score IS the unified decision score (one source of truth); the v2.3 opportunity
    # score is kept in the report only, labelled as legacy, and decides nothing.
    assert out.score >= settings.u_strong_min
    assert out.score == out.report["unified"]["score"] == out.report["final"]["score"]
    assert out.report["final"]["score_source"] == "unified decision engine"
    assert out.report["final"]["legacy_score_v23"] == out.report["score"]["total"]
    assert out.risk == RiskLevel.LOW_DETECTED
    assert out.confidence == Confidence.HIGH
    comps = {c["name"]: c for c in out.report["score"]["components"]}
    assert set(comps) == {"narrative", "demand", "liquidity", "holders", "momentum", "wallet_quality", "evidence"}
    assert all(c["evidence"] for c in comps.values()), "every component must carry evidence"
    assert out.report["score"]["could_be_wrong"], "must state reasons the score could be wrong"
    assert out.report["pool"]["address"] == POOL and out.report["pool"]["reason"]


# 2 -----------------------------------------------------------------------------------------
async def test_strong_narrative_insufficient_liquidity(settings, db):
    p = strong_providers()
    p.dex.pairs = [make_pair(liquidity_usd=8_000.0)]
    out = await run(settings, db, p)
    assert out.action == Action.REJECT
    assert gate(out, "liquidity")["status"] == "FAIL"


# 3 -----------------------------------------------------------------------------------------
def test_high_score_never_overrides_failed_gate(settings):
    d = decide(97.0, RiskLevel.LOW_DETECTED, Confidence.HIGH,
               [GateResult("freeze_authority", GateStatus.FAIL, "active")], 25, 4, False, settings)
    assert d.action == Action.REJECT


async def test_strong_candidate_with_failed_security_gate_rejected(settings, db):
    p = strong_providers()
    p.rpc.account = mint_account(freeze_auth=addr("freezer"))
    out = await run(settings, db, p)
    assert out.action == Action.REJECT
    assert any("freeze_authority" in r for r in out.reasons)


# 4 / 5 -------------------------------------------------------------------------------------
async def test_active_mint_authority(settings, db):
    p = strong_providers()
    p.rpc.account = mint_account(mint_auth=addr("minter"))
    out = await run(settings, db, p)
    assert out.action == Action.REJECT
    f = next(f for f in out.report["flags"] if f["code"] == "MINT_AUTHORITY_ACTIVE")
    assert f["status"] == "VERIFIED"


async def test_active_freeze_authority(settings, db):
    p = strong_providers()
    p.rpc.account = mint_account(freeze_auth=addr("freezer"))
    out = await run(settings, db, p)
    assert "FREEZE_AUTHORITY_ACTIVE" in flag_codes(out)
    assert out.risk == RiskLevel.CRITICAL


# 6 -----------------------------------------------------------------------------------------
async def test_unknown_liquidity_lock(settings, db):
    p = strong_providers()
    p.rugcheck = FakeRugCheck(fail=ProviderError("rugcheck", "MOCK outage"))
    out = await run(settings, db, p)
    assert "UNAVAILABLE" in out.report["security"]["lp_status"] or "UNKNOWN" in out.report["security"]["lp_status"]
    liq = next(c for c in out.report["score"]["components"] if c["name"] == "liquidity")
    assert any("LP" in m for m in liq["missing"])
    assert out.report["sources"]["rugcheck"].startswith("FAILED")
    assert out.risk != RiskLevel.LOW_DETECTED or out.action != Action.AUTO_POST or \
        out.report["score"]["completeness"] < 1


# 7 -----------------------------------------------------------------------------------------
async def test_token2022_permanent_delegate_rejected(settings, db):
    p = strong_providers()
    p.rpc.account = mint_account(program=TOKEN_2022_PROGRAM, extensions=[
        {"extension": "permanentDelegate", "state": {"delegate": addr("delegate")}}])
    out = await run(settings, db, p)
    assert out.action == Action.REJECT
    assert "PERMANENT_DELEGATE" in flag_codes(out)


async def test_token2022_benign_extensions_not_labelled_malicious(settings, db):
    p = strong_providers()
    p.rpc.account = mint_account(program=TOKEN_2022_PROGRAM, extensions=[
        {"extension": "metadataPointer", "state": {}}, {"extension": "tokenMetadata", "state": {}}])
    out = await run(settings, db, p)
    assert gate(out, "token_extensions")["status"] == "PASS"
    assert out.action == Action.AUTO_POST


async def test_token2022_small_transfer_fee_flagged_not_rejected(settings, db):
    p = strong_providers()
    p.rpc.account = mint_account(program=TOKEN_2022_PROGRAM, extensions=[
        {"extension": "transferFeeConfig", "state": {"newerTransferFee": {"transferFeeBasisPoints": 50},
                                                     "olderTransferFee": {"transferFeeBasisPoints": 50},
                                                     "transferFeeConfigAuthority": None}}])
    out = await run(settings, db, p)
    assert gate(out, "token_extensions")["status"] == "PASS"
    assert "TRANSFER_FEE" in flag_codes(out)


async def test_token2022_high_transfer_fee_rejected(settings, db):
    p = strong_providers()
    p.rpc.account = mint_account(program=TOKEN_2022_PROGRAM, extensions=[
        {"extension": "transferFeeConfig", "state": {"newerTransferFee": {"transferFeeBasisPoints": 1000}}}])
    out = await run(settings, db, p)
    assert out.action == Action.REJECT and "TRANSFER_FEE_HIGH" in flag_codes(out)


# 8 -----------------------------------------------------------------------------------------
async def test_concentrated_holders(settings, db):
    p = strong_providers()
    p.rpc.holders = [(POOL, 20.0), (addr("whale1"), 25.0), (addr("whale2"), 15.0)] + healthy_holders()[1:10]
    out = await run(settings, db, p)
    assert out.action == Action.REJECT
    assert gate(out, "holder_concentration")["status"] == "FAIL"
    h = out.report["holders"]
    assert h["top10_raw"] != h["top10_adj"], "raw and adjusted concentration both reported"


# 9 -----------------------------------------------------------------------------------------
async def test_bundled_launch_wallets(settings, db):
    p = strong_providers()
    bundlers = [addr(f"bundler{i}") for i in range(6)]
    t0 = int(NOW - 5 * 3600)
    p.rpc.launch = [swap_tx(w, "buy", 50_000_000, 10, t0) for w in bundlers]  # 6 x 5% in slot 10
    p.rpc.holders = [(POOL, 20.0)] + [(w, 5.0) for w in bundlers] + healthy_holders()[1:6]
    p.rpc.wallet_info = {w: (3600, addr("bundlefunder")) for w in bundlers}
    out = await run(settings, db, p)
    assert out.action == Action.REJECT
    assert {"LAUNCH_BUNDLE", "CONNECTED_WALLET_SUPPLY"} & flag_codes(out)
    clusters = out.report["clusters"]["clusters"]
    assert clusters and clusters[0]["strength"] in ("STRONG", "CONFIRMED")


# 10 ----------------------------------------------------------------------------------------
async def test_creator_dumping(settings, db):
    p = strong_providers()
    p.rpc.trades = healthy_trades() + [swap_tx(CREATOR, "sell", 40_000_000, 5000, int(NOW - 60))]
    out = await run(settings, db, p)
    assert out.action == Action.REJECT
    assert gate(out, "creator_dumping")["status"] == "FAIL"
    f = next(f for f in out.report["flags"] if f["code"] == "CREATOR_DUMPING")
    assert f["strength"] == "CONFIRMED" and "sampled" in f["message"]


# 11 ----------------------------------------------------------------------------------------
async def test_manipulated_volume_indicators(settings, db):
    p = strong_providers()
    wash = [addr(f"wash{i % 4}") for i in range(80)]
    p.rpc.trades = [swap_tx(w, "buy" if i % 2 else "sell", 100_000, 2000 + i, int(NOW - 3000 + i * 30))
                    for i, w in enumerate(wash)]
    out = await run(settings, db, p)
    codes = flag_codes(out)
    assert "POSSIBLE_MANIPULATION_REPEAT_BUYS" in codes or "POSSIBLE_WASH_TRADING" in codes
    assert all("possible manipulation" in f["message"] for f in out.report["flags"]
               if f["code"].startswith("POSSIBLE_")), "circumstantial evidence must be worded as 'possible'"
    demand = next(c for c in out.report["score"]["components"] if c["name"] == "demand")
    assert demand["points"] < 10 and demand["penalties"]
    assert out.action != Action.AUTO_POST


# 12 ----------------------------------------------------------------------------------------
async def test_strong_volume_but_declining_unique_buyers(settings, db):
    p = strong_providers()
    t0 = int(NOW - 3000)
    txs = []
    for i in range(100):
        w = addr(f"early{i}") if i < 50 else addr(f"late{i % 8}")  # many buyers early, few late
        txs.append(swap_tx(w, "buy", 100_000, 3000 + i, t0 + i * 30))
    p.rpc.trades = txs
    out = await run(settings, db, p)
    assert out.report["trades"]["buyer_trend"] == "declining"
    assert "DECLINING_BUYERS" in flag_codes(out)
    demand = next(c for c in out.report["score"]["components"] if c["name"] == "demand")
    assert any("declining" in x for x in demand["penalties"])


# 13 ----------------------------------------------------------------------------------------
async def test_already_pumped_excessively(settings, db):
    p = strong_providers()
    p.dex.pairs = [make_pair(price_change={"m5": 5.0, "h1": 80.0, "h6": 900.0, "h24": 1500.0})]
    out = await run(settings, db, p)
    assert "EXTENDED" in flag_codes(out)
    assert out.action != Action.AUTO_POST
    assert any("extended" in b for b in out.report["decision"]["blockers"])


# 14 ----------------------------------------------------------------------------------------
async def test_insufficient_narrative_evidence(settings, db):
    p = strong_providers()
    p.dex.metas = []  # nothing trending matches
    out = await run(settings, db, p, profile=False)
    assert out.action != Action.AUTO_POST
    nar = out.report["narrative"]
    assert nar["evidence_items"] < settings.min_narrative_evidence_items
    assert any("narrative" in b for b in out.report["decision"]["blockers"])


# 15 ----------------------------------------------------------------------------------------
async def test_missing_social_apis(settings, db):
    p = strong_providers()
    p.socials = None
    out = await run(settings, db, p)
    nar = out.report["narrative"]
    assert nar["social_status"] == "UNAVAILABLE"
    comp = nar["component"]
    assert any("social attention not measured" in m for m in comp["missing"])
    assert "social attention 0.0/4" in comp["evidence"][0], "no points for unavailable data"
    assert out.report["score"]["completeness"] < 1


# 16 ----------------------------------------------------------------------------------------
async def test_missing_creator_history(settings, db):
    p = strong_providers()
    p.rugcheck = FakeRugCheck(rc_report(creator=None, creatorTokens=None))
    out = await run(settings, db, p)
    assert out.report["creator"]["status"] == "UNKNOWN"
    assert "CREATOR_HISTORY_UNKNOWN" in flag_codes(out)
    holders = next(c for c in out.report["score"]["components"] if c["name"] == "holders")
    assert any("creator history unknown" in m for m in holders["missing"])


# 17 ----------------------------------------------------------------------------------------
async def test_stale_market_data(settings, db):
    p = strong_providers()
    p.dex.pairs = [make_pair(fetched_at=time.time() - 3600)]
    out = await run(settings, db, p)
    assert gate(out, "price_fresh")["status"] == "FAIL"
    assert out.action == Action.REJECT


# 18 ----------------------------------------------------------------------------------------
async def test_dexscreener_rate_limited_is_not_clean(settings, db):
    p = strong_providers()
    p.dex.fail = RateLimited("dexscreener", "MOCK 429", 429)
    out = await run(settings, db, p)
    assert out.action == Action.INSUFFICIENT_DATA
    assert out.report["sources"]["dexscreener"].startswith("FAILED")


# 19 ----------------------------------------------------------------------------------------
async def test_rpc_failure_is_not_clean(settings, db):
    p = strong_providers()
    p.rpc = FakeRpc(fail=ProviderError("solana_rpc", "MOCK RPC down"))
    out = await run(settings, db, p)
    assert out.action == Action.INSUFFICIENT_DATA
    assert gate(out, "mint_account")["status"] == "UNKNOWN"
    assert out.risk in (RiskLevel.INCOMPLETE, RiskLevel.CRITICAL, RiskLevel.ELEVATED)
    assert out.risk != RiskLevel.LOW_DETECTED


async def test_rpc_largest_accounts_disabled_falls_back_to_rugcheck_observed(settings, db):
    p = strong_providers()
    p.rpc.largest_fail = True
    p.rugcheck = FakeRugCheck(rc_report(topHolders=[
        {"address": addr("vaultA"), "owner": POOL, "pct": 20.0, "uiAmount": 2e8, "insider": False},
        *[{"address": addr(f"t{i}"), "owner": addr(f"o{i}"), "pct": 2.0, "uiAmount": 2e7, "insider": False}
          for i in range(12)]]))
    out = await run(settings, db, p)
    assert out.report["holders"]["status"] == "OBSERVED"
    assert out.report["holders"]["source"] == "rugcheck"


# 20 ----------------------------------------------------------------------------------------
async def test_invalid_mint(settings, db):
    out = await Researcher(settings, db, strong_providers()).research("not-a-real-mint!!")
    assert out.action == Action.REJECT
    assert "invalid mint" in out.reasons[0]


async def test_account_is_not_a_mint(settings, db):
    p = strong_providers()
    p.rpc.account = {"owner": "11111111111111111111111111111111", "data": ["", "base64"]}
    out = await run(settings, db, p)
    assert out.action == Action.REJECT and "not a valid SPL mint" in out.reasons[0]


async def test_pool_for_wrong_mint_is_never_used(settings, db):
    p = strong_providers()
    other = addr("othermint")
    p.dex.pairs = [make_pair(base_mint=other, quote_mint=MINT, pair_address=addr("invpool"))]
    out = await run(settings, db, p)
    assert out.action == Action.INSUFFICIENT_DATA
    assert out.report["pool"]["inverted_pools_ignored"] == 1


async def test_fdv_never_presented_as_market_cap(settings, db):
    p = strong_providers()
    p.dex.pairs = [make_pair(market_cap=None)]
    out = await run(settings, db, p)
    mc = out.report["market"]["market_cap"]
    assert mc["value"] is None and mc["status"] == "UNAVAILABLE"
    assert out.report["market"]["fdv"]["value"] == 600_000.0


async def test_unchanged_rejected_token_not_reanalysed(settings, db):
    p = strong_providers()
    p.dex.pairs = [make_pair(liquidity_usd=8_000.0)]
    r = Researcher(settings, db, p)
    seed_profile(db)
    first = await r.research(MINT)
    db.set_state(MINT, "REJECTED", last_data_hash=first.data_hash)
    second = await r.research(MINT, db.get_candidate(MINT))
    assert second.skipped_unchanged


async def test_rugcheck_danger_and_copycat(settings, db):
    p = strong_providers()
    p.rugcheck = FakeRugCheck(rc_report(risks=[{"name": "Large Amount of LP Unlocked", "level": "danger",
                                                "description": "MOCK"}]))
    out = await run(settings, db, p)
    assert out.action == Action.REJECT and "RUGCHECK_DANGER" in flag_codes(out)

    p2 = strong_providers()
    p2.dex.competitors = [make_pair(base_mint=addr("realone"), pair_address=addr("realpool"),
                                    liquidity_usd=900_000.0)]
    out2 = await run(settings, db, p2)
    assert "POSSIBLE_COPYCAT" in flag_codes(out2)


async def test_pumpfun_pre_migration_curve_can_qualify_early_watch(settings, db):
    """A token with no DEX pool can pass the separate early-watch path on real curve observations."""
    p = strong_providers()
    p.dex.pairs = []
    # Bonding-curve token: the curve vault holds most of the supply, early buyers small shares.
    p.rpc.holders = [(addr("curve"), 82.0)] + [(addr(f"early{i}"), 0.9) for i in range(19)]
    db.upsert_discovery(MINT, "pumpportal_new", 1.0, name="Early Frog", symbol="EFROG",
                        payload={"bondingCurveKey": addr("curve")})
    now = time.time()
    for i in range(20):
        is_sell = i in {4, 13}
        db.record_curve_snapshot(MINT, {
            "txType": "sell" if is_sell else "buy",
            "traderPublicKey": addr(f"curve-wallet-{i}"),
            "signature": f"mock-signature-{i}",
            "solAmount": 0.25,
            "tokenAmount": 100_000,
            "marketCapSol": 25.0 + i * 0.4,
            "vSolInBondingCurve": 40.0 + i,
            "vTokensInBondingCurve": 800_000_000.0 - i * 1000,
        }, ts=now - 20 + i)
    cand = db.get_candidate(MINT)
    out = await Researcher(settings, db, p).research(MINT, cand)
    assert out.pair is None
    assert out.action == Action.WATCHLIST
    assert out.risk == RiskLevel.INCOMPLETE
    assert out.report["lifecycle"] == "PRE_MIGRATION_BONDING_CURVE"
    assert out.report["pre_migration"]["qualified_early_watch"] is True
    assert out.report["pre_migration"]["market_cap_change_pct"] > 0
    assert "top-10 9.0%" in out.report["pre_migration"]["holder_risk"]


async def test_pre_migration_concentrated_holders_are_rejected(settings, db):
    p = strong_providers()
    p.dex.pairs = []
    p.rpc.holders = [(addr("curve"), 70.0), (addr("whale"), 15.0)] + [(addr(f"e{i}"), 0.5) for i in range(18)]
    db.upsert_discovery(MINT, "pumpportal_new", 1.0, name="Early Frog", symbol="EFROG",
                        payload={"bondingCurveKey": addr("curve")})
    now = time.time()
    for i in range(20):
        db.record_curve_snapshot(MINT, {"txType": "buy", "traderPublicKey": addr(f"w{i}"), "signature": f"s{i}",
                                        "solAmount": 0.25, "tokenAmount": 100_000, "marketCapSol": 25.0 + i,
                                        "vSolInBondingCurve": 40.0 + i, "vTokensInBondingCurve": 8e8},
                                 ts=now - 20 + i)
    out = await Researcher(settings, db, p).research(MINT, db.get_candidate(MINT))
    assert out.action == Action.REJECT
    assert any("largest ordinary holder owns 15.0% of total supply" in r for r in out.reasons)


def test_pump_fun_curve_account_parser_validates_owner_discriminator_and_units():
    # Synthetic account bytes only; validates decoder math, not a live account.
    virtual_tokens = 1_000_000_000_000_000
    virtual_sol_lamports = 30_000_000_000
    real_tokens = 793_100_000_000_000
    real_sol_lamports = 0
    total_supply = 1_000_000_000_000_000
    raw = PUMP_CURVE_DISCRIMINATOR + struct.pack(
        "<QQQQQ", virtual_tokens, virtual_sol_lamports, real_tokens, real_sol_lamports, total_supply
    ) + b"\x00" + b"\x01" * 32
    account = {"owner": PUMP_FUN_PROGRAM, "data": [base64.b64encode(raw).decode(), "base64"]}
    state = Researcher._parse_pump_curve_account(account)
    assert state is not None and state["complete"] is False
    assert round(state["market_cap_sol"], 2) == 30.0
    assert Researcher._parse_pump_curve_account({**account, "owner": "not-pump"}) is None
    bad = {"owner": PUMP_FUN_PROGRAM, "data": [base64.b64encode(b"wrong-layout" + raw[8:]).decode(), "base64"]}
    assert Researcher._parse_pump_curve_account(bad) is None


async def test_a_coin_whose_best_pair_is_its_own_pump_curve_gets_the_curve_research(settings, db, monkeypatch):
    """v3.2.1: DexScreener lists the bonding curve itself as a "pumpfun" pair. Such a coin has not
    migrated, so it must get the curve research (and runner check), not the DEX-pool rules."""
    p = strong_providers()
    p.dex.pairs = [make_pair(dex_id="pumpfun", liquidity_usd=5_395.0, market_cap=6_700.0, fdv=6_700.0)]
    p.rpc.holders = [(addr("curve"), 82.0)] + [(addr(f"early{i}"), 0.9) for i in range(19)]
    db.upsert_discovery(MINT, "curve_radar", 9.0, name="Early Frog", symbol="EFROG", payload={})
    r = Researcher(settings, db, p)

    async def on_curve(mint, report):
        return addr("curve"), {"complete": False, "market_cap_sol": 30.0}
    monkeypatch.setattr(r, "_probe_curve", on_curve)
    out = await r.research(MINT, db.get_candidate(MINT))
    assert out.report["lifecycle"] == "PRE_MIGRATION_BONDING_CURVE"
    assert not any("gate failed: liquidity" in x for x in out.reasons)

    async def finished(mint, report):
        return addr("curve"), {"complete": True, "market_cap_sol": 400.0}
    r2 = Researcher(settings, db, p)
    monkeypatch.setattr(r2, "_probe_curve", finished)
    out2 = await r2.research(MINT, db.get_candidate(MINT), force=True)
    assert out2.report.get("lifecycle") != "PRE_MIGRATION_BONDING_CURVE"


async def test_runner_candidates_read_further_back_to_the_launch(settings):
    from northstar.analysis import launch_trades as LT

    class Rpc:
        def __init__(self):
            self.calls = 0

        async def get_signatures(self, addr_, limit=1000, before=None):
            self.calls += 1
            return [{"signature": f"s{self.calls}-{i}"} for i in range(1000)]     # always a full page

    rpc = Rpc()
    lb = await LT.analyze(rpc, MINT, addr("curve"), None, settings)
    assert lb.status == "INCOMPLETE" and rpc.calls == settings.launch_trades_max_pages
    rpc2 = Rpc()
    lb2 = await LT.analyze(rpc2, MINT, addr("curve"), None, settings, max_pages=20)
    assert rpc2.calls == 20 and "20,000" in lb2.reasons[0]
