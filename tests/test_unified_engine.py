"""The unified decision engine: one score, one risk class, one decision.

Every case here is a situation the bot really meets. The point of the suite is that safety wins
over score, that missing data lowers confidence instead of quietly passing, and that no single
input (narrative, AI, the learning brain) can produce a call on its own.
"""
from __future__ import annotations

import pytest

from northstar.analysis import unified as U
from northstar.config import Settings


@pytest.fixture
def s() -> Settings:
    return Settings(discord_token="t", discord_channel_id=1, discord_vip_channel_id=2,
                    min_liquidity_usd=15_000, min_narrative_evidence_items=0,
                    min_volume_acceleration=0, min_buy_sell_ratio_h1=0)


def gates(**over) -> list[dict]:
    base = {"mint_authority": "PASS", "freeze_authority": "PASS", "token_extensions": "PASS",
            "liquidity_removal": "PASS", "lp_lock": "PASS", "rugcheck_danger": "PASS",
            "liquidity": "PASS", "holder_concentration": "PASS", "creator_holdings": "PASS"}
    base.update(over)
    return [{"name": k, "status": v, "reason": f"{k} {v.lower()}", "critical": True}
            for k, v in base.items()]


def report(**over) -> dict:
    """A clean, well-evidenced coin. Each test spoils exactly one thing."""
    r = {
        "mint": "So11111111111111111111111111111111111111112",
        "identity": {"symbol": "GOOD", "name": "Good Coin"},
        "market": {"price_usd": {"value": 0.0005}, "liquidity_usd": {"value": 60_000},
                   "market_cap": {"value": 300_000}, "fdv": {"value": 300_000},
                   "volume": {"m5": {"value": 9_000}, "h1": {"value": 90_000},
                              "h6": {"value": 300_000}},
                   "txns": {"h1": {"buys": 400, "sells": 200}}, "price_change": {"h1": 22.0}},
        "security": {"mint_authority": "REVOKED (VERIFIED)", "freeze_authority": "REVOKED (VERIFIED)",
                     "lp_status": "BURNED or LOCKED — 100%", "liquidity_to_mcap": 0.2,
                     "volume_to_liquidity_h1": 1.5},
        "gates": gates(),
        "flags": [],
        "holders": {"status": "OBSERVED", "top10_adj": 12.0, "max_single_adj": 2.1,
                    "creator_pct": 0.4},
        "clusters": {"status": "OBSERVED", "max_cluster_supply_pct": 2.0,
                     "fresh_wallet_share": 15.0},
        "creator": {"status": "OBSERVED", "prior_launches": 1, "prior_rugged": 0,
                    "summary": "1 earlier token, none rugged"},
        "trades": {"status": "VERIFIED", "sample_size": 120, "buys": 90, "sells": 30,
                   "unique_buyers": 70, "buyer_trend": "rising"},
        "market_structure": {"status": "OBSERVED", "constructive": True, "consolidating": True,
                             "volume_acceleration": 1.8, "buy_sell_ratio_h1": 2.0,
                             "observed_drawdown_pct": 12.0},
        "narrative": {"evidence_items": 3, "meta_status": "OBSERVED", "meta_match": True,
                      "social_status": "OBSERVED", "competing_tokens": 2},
        "sellability": {"checked": [{"usd": 100}, {"usd": 500}, {"usd": 1000}],
                        "worst_impact_pct": 1.4, "max_sellable_usd": 1000.0,
                        "no_route_at_usd": None},
        "verdict": {"level": "LOOKS CLEAN", "score": 0, "red": [], "green": ["LP burned"],
                    "unknown": []},
    }
    for k, v in over.items():
        r[k] = v
    return r


# 1 --------------------------------------------------------------------------------------------
def test_clean_coin_with_strong_evidence_is_a_call(s):
    u = U.evaluate(report(), s)
    assert u["decision"] == U.STRONG
    assert u["risk"] == U.RISK_LOW
    assert u["score"] >= s.u_strong_min
    assert u["completeness"] >= 0.75
    assert not u["hard_vetoes"] and not u["warnings"]
    assert U.LEGACY_ACTION[u["decision"]] == "AUTO_POST"


# 2 --------------------------------------------------------------------------------------------
def test_a_critical_vulnerability_beats_any_score(s):
    """Narrative and volume must never outweigh a proven trap."""
    r = report(verdict={"level": "RUG LIKELY", "score": 100, "red": [
        {"key": "permanent_delegate", "level": "critical",
         "text": "a permanent delegate can move anyone's tokens"}], "green": [], "unknown": []})
    u = U.evaluate(r, s)
    assert u["decision"] == U.REJECT
    assert u["risk"] == U.RISK_EXTREME
    assert u["hard_vetoes"]
    # and it stays rejected even if the brain is euphoric and the AI loves the story
    lifted = U.apply_model(u, r, probability=0.99, trusted=True, baseline=0.15, s=s)
    lifted = U.apply_ai(lifted, r, {"verdict": "CALL", "score": 99, "reason": "perfect meta"}, s)
    assert lifted["decision"] == U.REJECT
    assert lifted["risk"] == U.RISK_EXTREME


# 3 --------------------------------------------------------------------------------------------
def test_a_coin_that_cannot_be_sold_is_rejected(s):
    r = report(sellability={"checked": [{"usd": 100}], "worst_impact_pct": None,
                            "max_sellable_usd": None, "no_route_at_usd": 100.0})
    u = U.evaluate(r, s)
    assert u["decision"] == U.REJECT
    assert any("cannot sell" in v for v in u["hard_vetoes"])


def test_a_partly_sellable_coin_is_warned_not_called(s):
    r = report(sellability={"checked": [{"usd": 100}, {"usd": 500}, {"usd": 1000}],
                            "worst_impact_pct": 8.0, "max_sellable_usd": 500.0,
                            "no_route_at_usd": 1000.0})
    u = U.evaluate(r, s)
    assert u["decision"] != U.STRONG
    assert any("1,000" in w or "1000" in w for w in u["warnings"])


# 4 --------------------------------------------------------------------------------------------
def test_liquidity_below_the_floor_blocks_a_call(s):
    r = report()
    r["market"]["liquidity_usd"] = {"value": 9_000}       # floor is 15,000
    u = U.evaluate(r, s)
    assert u["decision"] in (U.CAUTION, U.REJECT)
    assert any("below the" in w for w in u["warnings"]) or u["hard_vetoes"]


def test_severely_thin_liquidity_is_a_hard_veto(s):
    r = report()
    r["market"]["liquidity_usd"] = {"value": 2_000}       # under half the floor
    u = U.evaluate(r, s)
    assert u["decision"] == U.REJECT
    assert u["risk"] == U.RISK_EXTREME


# 5 --------------------------------------------------------------------------------------------
def test_missing_holder_data_lowers_confidence_instead_of_passing(s):
    full = U.evaluate(report(), s)
    blind = U.evaluate(report(holders={"status": "UNAVAILABLE"},
                              clusters={"status": "UNAVAILABLE"}), s)
    assert blind["categories"]["holders"]["points"] == 0.0
    assert blind["categories"]["holders"]["coverage"] < 0.5
    assert blind["completeness"] < full["completeness"]
    assert blind["score"] < full["score"]
    assert blind["decision"] != U.STRONG, "unknown holders must never read as clean holders"


# 6 --------------------------------------------------------------------------------------------
def test_conflicting_providers_are_recorded_not_averaged(s):
    r = report(sellability={"checked": [{"usd": 100}, {"usd": 500}], "worst_impact_pct": 3.0,
                            "max_sellable_usd": 100.0, "no_route_at_usd": 500.0})
    u = U.evaluate(r, s)
    assert u["conflicts"], "deep liquidity with no route is a contradiction worth showing"
    assert u["confidence"] != "HIGH"
    assert u["decision"] != U.STRONG


def test_impossible_liquidity_versus_market_cap_is_flagged(s):
    r = report()
    r["market"]["market_cap"] = {"value": 20_000}          # smaller than its own liquidity
    u = U.evaluate(r, s)
    assert any("larger than" in c for c in u["conflicts"])


# 7 --------------------------------------------------------------------------------------------
def test_a_great_story_cannot_carry_a_bad_chart(s):
    r = report(market_structure={"status": "OBSERVED", "fading": True, "volume_acceleration": 0.3,
                                 "buy_sell_ratio_h1": 0.6, "observed_drawdown_pct": 55.0},
               trades={"status": "VERIFIED", "sample_size": 120, "buys": 90, "sells": 60,
                       "unique_buyers": 18, "buyer_trend": "declining"})
    u = U.evaluate(r, s)
    u = U.apply_ai(u, r, {"verdict": "CALL", "score": 97, "reason": "front page news"}, s)
    assert u["decision"] != U.STRONG
    assert u["categories"]["structure"]["points"] < u["categories"]["structure"]["weight"] / 2


def test_narrative_alone_cannot_reach_the_call_line(s):
    """The narrative cap: a STRONG call must stand up without most of its narrative points."""
    r = report()
    u = U.evaluate(r, s)
    without = u["score"] - u["categories"]["narrative"]["points"]
    assert u["score_ex_narrative"] == pytest.approx(without, abs=0.2)
    assert u["score_ex_narrative"] >= s.u_strong_min - s.u_narrative_cap


# 8 --------------------------------------------------------------------------------------------
def test_strong_volume_with_suspicious_wallets_is_not_a_call(s):
    r = report(verdict={"level": "HIGH RISK", "score": 50, "red": [
        {"key": "buying_concentrated", "level": "high", "text": "a few wallets do most of the buying"},
        {"key": "fresh_wallets", "level": "high", "text": "mostly brand-new wallets"}],
        "green": [], "unknown": []})
    u = U.evaluate(r, s)
    assert u["decision"] == U.REJECT            # v2.5.9: two serious warnings are never watched
    assert u["reason_class"] == "SERIOUS_WARNINGS"
    assert u["risk"] == U.RISK_HIGH
    assert len(u["warnings"]) >= 2


def test_double_warning_watch_can_be_restored(s):
    s.u_reject_double_warning = False
    r = report(verdict={"level": "HIGH RISK", "score": 50, "red": [
        {"key": "buying_concentrated", "level": "high", "text": "a few wallets do most of the buying"},
        {"key": "fresh_wallets", "level": "high", "text": "mostly brand-new wallets"}],
        "green": [], "unknown": []})
    assert U.evaluate(r, s)["decision"] == U.CAUTION



# 9 --------------------------------------------------------------------------------------------
def test_an_unavailable_ai_provider_changes_nothing(s):
    r = report()
    u = U.evaluate(r, s)
    after = U.apply_ai(u, r, None, s)
    assert after["ai"]["used"] is False
    assert after["decision"] == u["decision"]
    assert after["score"] == pytest.approx(u["score"], abs=0.01)


def test_a_malformed_ai_answer_is_ignored(s):
    r = report()
    u = U.evaluate(r, s)
    after = U.apply_ai(u, r, {"error": "unparsed"}, s)
    assert after["ai"]["used"] is False


# 10 -------------------------------------------------------------------------------------------
def test_rugcheck_being_down_is_not_evidence_of_safety(s):
    r = report(gates=gates(rugcheck_danger="UNKNOWN", lp_lock="UNKNOWN"))
    r["security"]["lp_status"] = "UNKNOWN"
    u = U.evaluate(r, s)
    assert u["decision"] == U.INSUFFICIENT, "an unverifiable LP cannot be called clean"
    assert any("lp lock" in x for x in u["unresolved"])
    assert u["risk"] != U.RISK_LOW


def test_rugcheck_down_but_lp_verified_on_chain_still_decides(s):
    """The on-chain LP read is what makes a call possible while RugCheck is degraded."""
    r = report(gates=gates(rugcheck_danger="UNKNOWN"))
    u = U.evaluate(r, s)
    assert u["decision"] in (U.STRONG, U.WATCH)
    assert u["categories"]["safety"]["coverage"] < 1.0


# 11 -------------------------------------------------------------------------------------------
def test_the_decision_moves_when_the_signals_move(s):
    before = U.evaluate(report(), s)
    r = report()
    r["market"]["liquidity_usd"] = {"value": 18_000}
    r["market_structure"]["fading"] = True
    r["market_structure"]["constructive"] = False
    r["market_structure"]["consolidating"] = False
    after = U.evaluate(r, s)
    assert after["score"] < before["score"]
    assert after["decision"] != before["decision"]


# 12 -------------------------------------------------------------------------------------------
def test_a_called_coin_whose_risk_deteriorates_is_downgraded(s):
    r = report()
    called = U.evaluate(r, s)
    assert called["decision"] == U.STRONG
    # the dev starts selling and the LP read goes away: re-checking must not still say CALL
    r["verdict"] = {"level": "HIGH RISK", "score": 50, "red": [
        {"key": "creator_dumping", "level": "high", "text": "the dev is selling"}],
        "green": [], "unknown": []}
    r["gates"] = gates(creator_dumping="FAIL")
    again = U.evaluate(r, s)
    assert again["decision"] in (U.CAUTION, U.REJECT)
    assert again["risk"] in (U.RISK_HIGH, U.RISK_EXTREME)
    assert again["exit_triggers"]


# 13 -------------------------------------------------------------------------------------------
def test_an_untrusted_or_absent_model_cannot_tilt_the_score(s):
    r = report()
    u = U.evaluate(r, s)
    untrusted = U.apply_model(u, r, probability=0.95, trusted=False, baseline=0.15, s=s)
    assert untrusted["model"]["tilt"] == 0.0
    assert untrusted["score"] == pytest.approx(u["score"], abs=0.01)
    nothing = U.apply_model(u, r, probability=None, trusted=True, baseline=0.15, s=s)
    assert nothing["model"]["tilt"] == 0.0


def test_a_trusted_model_tilt_is_bounded_both_ways(s):
    r = report()
    u = U.evaluate(r, s)
    up = U.apply_model(u, r, probability=0.95, trusted=True, baseline=0.15, s=s)
    down = U.apply_model(u, r, probability=0.0, trusted=True, baseline=0.15, s=s)
    assert 0 < up["model"]["tilt"] <= s.u_model_tilt
    assert -s.u_model_tilt <= down["model"]["tilt"] < 0
    assert up["score"] - u["score"] <= s.u_model_tilt + 0.01
    assert u["score"] - down["score"] <= s.u_model_tilt + 0.01
    assert down["score"] < u["score"]
    # the brain can never clear a veto, however good the odds
    trap = report(verdict={"level": "RUG LIKELY", "score": 100, "red": [
        {"key": "transfer_hook", "level": "critical", "text": "transfers run custom code"}],
        "green": [], "unknown": []})
    assert U.apply_model(U.evaluate(trap, s), trap, 0.99, True, baseline=0.15,
                         s=s)["decision"] == U.REJECT


# 14 -------------------------------------------------------------------------------------------
def test_walk_forward_never_learns_from_later_coins():
    """A fold's predictions must not change when the labels of LATER coins change."""
    from northstar import brain as B

    rows = []
    for i in range(240):
        rows.append({"id": i, "ts": 1_700_000_000 + i * 60,
                     "v": {"log_mcap": 4 + (i % 7) * 0.1, "log_liq": 3 + (i % 5) * 0.2,
                           "buy_share_h1": 0.4 + (i % 10) * 0.05},
                     "y": 1 if i % 4 == 0 else 0})
    features = ["log_mcap", "log_liq", "buy_share_h1"]
    _, _, oof_a = B.walk_forward(rows, features)
    flipped = [dict(r) for r in rows]
    for r in flipped[-40:]:
        r["y"] = 1 - r["y"]
    _, _, oof_b = B.walk_forward(flipped, features)
    early = [i for i in oof_a if i < 150 and i in oof_b]
    assert early, "there should be out-of-sample predictions for early coins"
    for i in early:
        assert oof_a[i] == pytest.approx(oof_b[i], abs=1e-9), \
            f"coin {i} changed when later labels changed — future information leaked"


def test_unresolved_outcomes_are_not_trained_on():
    """A coin still in flight has no label and must be dropped, not counted as a loss."""
    from northstar import brain as B

    # path rows are (minute, high, low, close) multiples of the entry price
    assert B.outcome([], 2.0, 0.6) is None                         # still in flight: no label
    assert B.outcome([(0, 1.0, 1.0, 1.0), (60, 2.2, 1.1, 2.0)], 2.0, 0.6) == 1
    assert B.outcome([(0, 1.0, 1.0, 1.0), (60, 1.2, 0.5, 0.6)], 2.0, 0.6) == 0
    # 2x AFTER the drawdown is not a win: the stop came first
    assert B.outcome([(0, 1.2, 0.5, 0.6), (60, 3.0, 1.0, 2.5)], 2.0, 0.6) == 0


# the panel -------------------------------------------------------------------------------------
def test_panel_text_is_readable_and_bounded(s):
    u = U.evaluate(report(), s)
    text = U.panel_text(u)
    assert "STRONG OPPORTUNITY" in text and "score" in text and "evidence" in text
    assert len(text) <= 1024, "the panel has to fit a Discord field"
    assert "100/100" not in text
    cats = U.category_text(u)
    for label in ("Liquidity and exit", "Holders and wallets", "Narrative and attention"):
        assert label[:20] in cats


def test_no_signal_is_counted_twice(s):
    """The same hazard reported by a gate and by a flag must produce ONE veto."""
    r = report(gates=gates(holder_concentration="FAIL"),
               verdict={"level": "HIGH RISK", "score": 50, "red": [
                   {"key": "top10_big", "level": "high", "text": "top 10 hold a lot"},
                   {"key": "whale_holder", "level": "high", "text": "one wallet holds 9%"}],
                   "green": [], "unknown": []})
    u = U.evaluate(r, s)
    keys = [v["key"] for v in u["vetoes"]]
    assert keys.count("haz:concentration") == 1
    assert len(keys) == len(set(keys))
    # and a vetoed hazard never also costs category points
    assert all(c["key"] != "haz:concentration" for c in u["checks"])


def test_every_check_belongs_to_exactly_one_category(s):
    u = U.evaluate(report(), s)
    keys = [c["key"] for c in u["checks"]]
    assert len(keys) == len(set(keys))
    assert {c["category"] for c in u["checks"]} <= set(U.CATEGORIES)


def test_weights_sum_to_one_hundred(s):
    assert sum(U._weights(s).values()) == pytest.approx(100.0)


# the callout's new holder and thesis blocks -----------------------------------------------------
def _holder(owner, pct, **extra):
    return {"token_account": owner + "ta", "owner": owner, "amount": 1000.0, "pct_raw": pct, **extra}


def test_top_holders_block_lists_wallets_with_what_is_known(s):
    from northstar.discord_bot.embeds import holders_text

    r = report()
    r["holders"]["holders"] = [
        _holder("Aa11111111111111111111111111111111111111", 2.61),
        _holder("Bb22222222222222222222222222222222222222", 2.10),
        _holder("Cc33333333333333333333333333333333333333", 1.80, insider=True),
        _holder("Dd44444444444444444444444444444444444444", 9.90, excluded="pool"),
    ]
    r["clusters"]["bubbles"] = [
        {"id": "Aa11111111111111111111111111111111111111", "age_h": 400.0},
        {"id": "Bb22222222222222222222222222222222222222", "age_h": 4.0, "fresh": True,
         "cluster": 0, "cluster_strength": "STRONG", "launch_buyer": True},
    ]
    text = holders_text(r)
    assert "2.61%" in text and "2.10%" in text
    assert "17d old" in text and "4h old" in text and "⚠ fresh" in text
    assert "🎯 launch buyer" in text and "🫧 group 1" in text and "insider" in text
    assert "9.90%" not in text, "pool and locker accounts are excluded from the holder list"
    assert "Top 10 hold **12.0%**" in text and "dev **0.40%**" in text
    assert len(text) <= 1024


def test_holder_block_says_so_when_there_is_no_holder_data(s):
    from northstar.discord_bot.embeds import holders_text

    assert "not available" in holders_text(report(holders={"status": "UNAVAILABLE"}))


def test_thesis_block_carries_the_story_and_the_ai_read(s):
    from northstar.discord_bot.embeds import thesis_text

    r = report()
    r["narrative"] |= {"category": "Space / launch meta",
                       "thesis": "Rides the launch-week attention around the Roadster flight."}
    r["score"] = {"components": [{"name": "narrative", "points": 18.0, "max_points": 25,
                                  "evidence": ["matches a trending meta: space/launch"],
                                  "penalties": ["9 tokens share this ticker"]}]}
    r["unified"] = U.apply_ai(U.evaluate(r, s), r,
                              {"verdict": "CALL", "score": 84, "provider": "gemini",
                               "reason": "biggest attention event of the day"}, s)
    text = thesis_text(r)
    assert "Space / launch meta" in text and "Roadster flight" in text
    assert "3 independent evidence item" in text
    assert "Against it:" in text and "share this ticker" in text
    assert "**AI read (gemini):** CALL 84/100" in text and "attention event" in text
    assert len(text) <= 1024


def test_callout_shows_thesis_and_holders(s):
    from northstar.discord_bot.embeds import callout_embed, embed_size

    r = report()
    r["holders"]["holders"] = [_holder("Aa11111111111111111111111111111111111111", 2.61)]
    r["pool"] = {"address": "9Xev", "dex": "pumpswap"}
    r["unified"] = U.evaluate(r, s)
    e = callout_embed(r, 1_792_000_000.0)
    names = [f["name"] for f in e["fields"]]
    assert "🧵 Thesis" in names and "👥 Top holders" in names
    assert "Why Northstar selected it" not in names, "the thesis block replaces it"
    assert embed_size(e) <= 6000
