"""v2.5.5: which coins Northstar keeps following (operator rule, 22 Oct). Synthetic data only.

A coin that fails is not followed again unless it has good numbers, a good narrative and no high
holder concentration; never-researched coins leave the watch list; 10% of other rejections are
still price-tracked so false rejections stay measurable.
"""
from __future__ import annotations

import copy
import time

import pytest

from conftest import MINT, addr, make_pair, seed_profile, strong_providers
from northstar import tracking_policy as TP
from northstar.analysis import unified as U
from northstar.config import Settings
from northstar.engine import Engine
from northstar.final import apply_final
from northstar.models import Action
from northstar.research import Researcher, ResearchOutcome
from conftest import MemoryPublisher

from test_unified_engine import gates, report


@pytest.fixture
def s() -> Settings:
    return Settings(_env_file=None, discord_token="t", discord_channel_id=1, discord_vip_channel_id=2,
                    min_liquidity_usd=15_000, min_narrative_evidence_items=1,
                    min_volume_acceleration=0, min_buy_sell_ratio_h1=0)


def decided(r: dict, s, action=Action.REJECT) -> dict:
    r = copy.deepcopy(r)
    r["unified"] = U.evaluate(r, s)
    out = ResearchOutcome(addr("x"), action, r, pair=make_pair())
    apply_final(out)
    return out.report


def blocked_good_coin() -> dict:
    """Good numbers, good narrative, spread holders — rejected only by a timing-style blocker."""
    r = report(market_structure={"status": "OBSERVED", "constructive": True, "volume_acceleration": 1.8,
                                 "buy_sell_ratio_h1": 2.0, "observed_drawdown_pct": 12.0})
    return r


# ------------------------------------------------------------------ the rule
def test_good_numbers_narrative_and_spread_holders_get_a_second_chance(s):
    ok, why = TP.second_chance(decided(blocked_good_coin(), s), s)
    assert ok, why


@pytest.mark.parametrize("spoil, expect", [
    (lambda r: r.update(gates=gates(holder_concentration="FAIL")), "holder"),
    (lambda r: r.update(gates=gates(holder_concentration="UNKNOWN")), "holder"),
    (lambda r: r.update(narrative={"evidence_items": 0, "meta_status": "UNAVAILABLE"}), "narrative"),
    (lambda r: r["market"].update(liquidity_usd={"value": 9_000}), "liquidity"),
    (lambda r: r.update(verdict={"level": "CAUTION", "red": [{"key": "cluster_big", "level": "high",
                                                              "text": "linked wallets hold 35%"}]}), "cluster"),
    (lambda r: r.update(market_structure={"status": "OBSERVED", "collapsed": True,
                                          "collapse_text": "down 80% in 2h"}), ""),
])
def test_any_missing_part_means_not_followed(s, spoil, expect):
    r = blocked_good_coin()
    spoil(r)
    ok, why = TP.second_chance(decided(r, s), s)
    assert not ok and expect in why


@pytest.mark.parametrize("gate", ["mint_authority", "freeze_authority", "liquidity_removal"])
def test_a_proven_trap_never_gets_a_second_chance(s, gate):
    ok, why = TP.second_chance(decided(report(gates=gates(**{gate: "FAIL"})), s), s)
    assert not ok


def test_rejections_without_a_full_analysis_are_not_followed(s):
    assert TP.second_chance({"final": {"reason_class": "PREFILTER"}}, s)[0] is False
    assert TP.second_chance({"final": {"reason_class": "RISK_THRESHOLD"}}, s)[0] is False


# ------------------------------------------------------------------ price tracking sample
def test_the_learning_sample_is_about_ten_percent_and_stable(s):
    mints = [addr(f"m{i}") + str(i) for i in range(20_000)]
    picked = [m for m in mints if TP.sampled(m, 10)]
    assert 0.085 < len(picked) / len(mints) < 0.115
    assert all(TP.sampled(m, 10) for m in picked[:200]), "the same coin is always in"
    assert TP.sampled(mints[0], 100) and not TP.sampled(mints[0], 0)


def test_what_gets_price_tracked(s):
    weak = decided(report(gates=gates(holder_concentration="FAIL")), s)
    inside = next(m for m in (addr(f"t{i}") for i in range(500)) if TP.sampled(m, 10))
    outside = next(m for m in (addr(f"t{i}") for i in range(500)) if not TP.sampled(m, 10))
    assert TP.track_outcome(outside, "WATCHLIST", weak, s) and TP.track_outcome(outside, "AUTO_POST", weak, s)
    assert TP.track_outcome(inside, "REJECT", weak, s)
    assert not TP.track_outcome(outside, "REJECT", weak, s)
    assert TP.track_outcome(outside, "REJECT", decided(blocked_good_coin(), s), s), "second chance: tracked"
    assert not TP.track_outcome(inside, "REJECT", weak, s.model_copy(update={"reject_tracking_sample_pct": 0}))


def test_settings_bounds():
    x = Settings(_env_file=None, reject_tracking_sample_pct=250, rejected_max_rechecks=-2,
                 insufficient_max_rechecks=-1, discovered_max_age_hours=-3)
    assert (x.reject_tracking_sample_pct, x.rejected_max_rechecks, x.insufficient_max_rechecks,
            x.discovered_max_age_hours) == (100.0, 0, 0, 0.0)
    d = Settings(_env_file=None)
    assert (d.reject_tracking_sample_pct, d.rejected_max_rechecks, d.insufficient_max_rechecks,
            d.discovered_max_age_hours) == (10.0, 6, 8, 6.0)


# ------------------------------------------------------------------ live wiring
async def test_a_rejected_coin_is_not_queued_again_and_not_price_tracked(settings, db):
    p = strong_providers()
    p.dex.pairs = [make_pair(liquidity_usd=8_000.0)]
    seed_profile(db)
    db.upsert_discovery(MINT, "dexscreener_profile", 5.0, payload={"x": 1})
    settings = settings.model_copy(update={"reject_tracking_sample_pct": 0.0})
    eng = Engine(settings, db, p, MemoryPublisher())
    await eng.process(MINT)
    cand = db.get_candidate(MINT)
    assert cand["state"] == "REJECTED" and cand["next_research_at"] is None
    assert db.fetchone("SELECT COUNT(*) n FROM decision_tracking")["n"] == 0
    assert db.fetchone("SELECT COUNT(*) n FROM outcomes")["n"] == 0
    assert db.due_candidates(10, time.time() + 10 * 86400) == []


def test_never_researched_coins_expire_and_new_information_brings_them_back(db):
    old, fresh, done = addr("old"), addr("fresh"), addr("done")
    for m in (old, fresh, done):
        db.upsert_discovery(m, "pumpportal_new", 1.0, payload={"mint": m})
    now = time.time()
    db.execute("UPDATE candidates SET discovered_at=? WHERE mint IN (?, ?)", (now - 7 * 3600, old, done))
    db.execute("UPDATE candidates SET research_count=1, state='WATCHLIST' WHERE mint=?", (done,))
    assert db.expire_discovered(6, now) == 1
    states = {r["mint"]: r["state"] for r in db.fetchall("SELECT mint, state FROM candidates")}
    assert states == {old: "STALE", fresh: "DISCOVERED", done: "WATCHLIST"}
    db.upsert_discovery(old, "pumpportal_new", 1.0, payload={"mint": old})          # a repeat: stays out
    assert db.get_candidate(old)["state"] == "STALE"
    db.upsert_discovery(old, "radar_surge", 10.0, payload={"spike": 3.1})           # new information
    c = db.get_candidate(old)
    assert c["state"] == "DISCOVERED" and c["next_research_at"] is not None
    assert db.expire_discovered(0, now) == 0


def test_finished_unfollowed_coins_are_forgotten_and_followed_ones_kept(db):
    now = time.time()
    old = now - 10 * 86400
    keep_alert, keep_open, keep_recheck, forget_stale, forget_rej, recent = (addr(n) for n in
        ("alert", "open", "recheck", "stale", "rej", "recent"))
    for m in (keep_alert, keep_open, keep_recheck, forget_stale, forget_rej, recent):
        db.upsert_discovery(m, "dexscreener_profile", 1.0, payload={"m": m})
    db.execute("UPDATE candidates SET state='REJECTED', next_research_at=NULL, updated_at=?", (old,))
    db.execute("UPDATE candidates SET state='STALE' WHERE mint=?", (forget_stale,))
    db.execute("UPDATE candidates SET next_research_at=? WHERE mint=?", (now + 60, keep_recheck))
    db.execute("UPDATE candidates SET updated_at=? WHERE mint=?", (now, recent))
    db.execute("INSERT INTO alerts(mint, status, state, created_at) VALUES (?,?,?,?)", (keep_alert, "SENT", "OPEN", old))
    db.execute("INSERT INTO decision_tracking(snapshot_id, mint, action, decided_at, done) VALUES (1,?,?,?,0)",
               (keep_open, "REJECT", now))
    assert db.prune_candidates(7, now) == 2
    left = {r["mint"] for r in db.fetchall("SELECT mint FROM candidates")}
    assert left == {keep_alert, keep_open, keep_recheck, recent}
    assert db.prune_candidates(0, now) == 0


# ------------------------------------------------------------------ v2.5.5 review: what "strong numbers" means
@pytest.mark.parametrize("spoil, expect", [
    (lambda r: r.update(gates=gates(lp_lock="UNKNOWN")), "safety not established"),
    (lambda r: r["market"].update(liquidity_usd={"value": 25_000}), "liquidity under $35,000"),
    (lambda r: r["market_structure"].update(buy_sell_ratio_h1=0.9), "buyers not ahead"),
    (lambda r: r["market_structure"].update(volume_acceleration=0.7), "volume"),
    (lambda r: r["market_structure"].update(fading=True), "fading"),
])
def test_a_second_chance_needs_strong_activity_and_established_safety(s, spoil, expect):
    r = blocked_good_coin()
    spoil(r)
    ok, why = TP.second_chance(decided(r, s), s)
    assert not ok and expect in why, why


def test_the_learning_brain_cannot_buy_a_second_chance(s):
    """A coin just under the watch line stays out even if the brain's tilt lifts it over."""
    r = decided(blocked_good_coin(), s)
    u = r["unified"]
    lifted = dict(u, score=s.u_watch_min + 5, model={"tilt": 10.0, "trusted": True})
    r2 = dict(r, unified=lifted)
    ok, why = TP.second_chance(r2, s)
    assert not ok and "watch line" in why
