"""Self-learning: recording, evaluation, plan simulation and safe auto-tuning. SYNTHETIC data only."""
from __future__ import annotations

import asyncio
import json
import time

from conftest import MINT, addr, make_pair
from northstar import learning as L
from northstar import trade_plan as TP
from northstar.config import Settings

STD = TP.plan_values(Settings(_env_file=None), "standard")   # tp1 1.5/50%, tp2 2/25%, stop .7, trail 30


def path(*points):
    """points: (minute, high, low, close)"""
    return [list(p) for p in points]


def test_simulate_take_profits_and_trailing():
    p = path((0, 1.0, 0.98, 1.0), (1, 1.6, 1.1, 1.5), (2, 2.2, 1.9, 2.1), (3, 2.1, 1.4, 1.45))
    r = L.simulate(p, STD)
    # 50% at 1.5x (+0.25), 25% at 2x (+0.25), rest trails out at 2.2*0.7=1.54 (worst-case fill 1.54*0.97)
    assert abs(r - (0.25 + 0.25 + 0.25 * (1.54 * 0.97 - 1) - L.FEES)) < 1e-6


def test_simulate_stop_is_worst_case_and_crash_fills_at_the_bottom():
    dip = path((0, 1.0, 0.95, 1.0), (1, 1.05, 0.68, 0.9))
    assert abs(L.simulate(dip, STD) - ((0.7 * 0.97 - 1) - L.FEES)) < 1e-6
    rug = path((0, 1.0, 0.95, 1.0), (1, 1.4, 0.05, 0.06))          # dumps straight through the stop
    assert abs(L.simulate(rug, STD) - ((0.06 - 1) - L.FEES)) < 1e-6
    same_candle = path((0, 1.0, 0.95, 1.0), (1, 1.6, 0.6, 0.65))   # TP1 and stop in one candle: assume stop
    assert L.simulate(same_candle, STD) < 0


def test_simulate_time_stop_and_no_entry():
    flat = path(*[(m, 1.05, 0.95, 1.0) for m in range(0, 50)])
    r = L.simulate(flat, {**STD, "time_stop_minutes": 30})
    assert abs(r - (0 - L.FEES)) < 1e-6
    ran_away = path((0, 2.0, 1.6, 1.9))
    assert L.simulate(ran_away, STD) is None
    assert L.simulate([], STD) is None


def test_outlier_capped():
    moon = path((0, 1.0, 0.99, 1.0), (1, 1.6, 1.4, 1.6), (2, 2.1, 1.9, 2.0), *[(m, 80.0, 79.0, 80.0) for m in range(3, 10)])
    assert L.simulate(moon, STD) == L.OUTLIER_CAP - L.FEES


def test_path_from_candles():
    t0 = 1_000_000.0
    candles = [(t0 - 120, 1, 1, 1, 1, 0), (t0, 1, 2.0, 0.5, 1.5, 0), (t0 + 60, 1, 3.0, 1.0, 2.0, 0)]
    p = L.path_from(candles, 1.0, t0)
    assert p == [[0.0, 2.0, 0.5, 1.5], [1.0, 3.0, 1.0, 2.0]]
    assert L.path_from(candles, 0, t0) == []


def feats(**kw):
    base = {"score": 50, "risk": "MODERATE", "confidence": "MEDIUM", "vol_acc": 2.0, "bs_h1": 1.5, "extended": False,
            "failed": [], "unknown_critical": [], "surge": False}
    base.update(kw)
    return base


def test_standard_eligibility_never_relaxes_rug_checks():
    s = Settings(_env_file=None, min_opportunity_score=40, min_confidence="MEDIUM", max_allowed_risk="MODERATE",
                 min_callout_market_cap=100_000, min_volume_acceleration=1.0, min_buy_sell_ratio_h1=1.2,
                 block_extended_moves=False)
    assert L.standard_eligible(feats(), 150_000, s, {})
    assert not L.standard_eligible(feats(failed=["holder_concentration"]), 150_000, s, {"min_opportunity_score": 0})
    assert not L.standard_eligible(feats(risk="CRITICAL"), 150_000, s, {})
    assert not L.standard_eligible(feats(), 90_000, s, {})
    assert L.standard_eligible(feats(), 90_000, s, {"min_callout_market_cap": 75_000})
    assert not L.standard_eligible(feats(vol_acc=0.4), 150_000, s, {})


def test_scalp_eligibility():
    s = Settings(_env_file=None)
    assert L.scalp_eligible(feats(failed=["holder_concentration"], surge=True), 120_000, 30_000, s)
    assert not L.scalp_eligible(feats(failed=["holder_concentration", "freeze_authority"], surge=True), 120_000, 30_000, s)
    assert not L.scalp_eligible(feats(failed=["holder_concentration"], surge=False), 120_000, 30_000, s)
    assert not L.scalp_eligible(feats(failed=[], surge=True), 120_000, 30_000, s)


def test_record_dedupes_and_requires_price(db):
    L.init_schema(db)
    pair = make_pair()
    assert L.record(db, MINT, {"score": {"total": 50}}, pair, "REJECT", None, False)
    assert not L.record(db, MINT, {"score": {"total": 51}}, pair, "REJECT", None, False)     # within 30 min
    assert L.record(db, MINT, {"score": {"total": 52}}, pair, "AUTO_POST", "STANDARD", True)  # a real call: always
    assert not L.record(db, addr("x"), {}, make_pair(price_usd=None), "REJECT", None, False)
    rows = db.fetchall("SELECT called, features FROM shadow_calls ORDER BY id")
    assert [r["called"] for r in rows] == [0, 1] and json.loads(rows[0]["features"])["score"] == 50


def test_evaluate_due_stores_path_and_gives_up_after_three_tries(db):
    L.init_schema(db)
    now = time.time()
    L.record(db, MINT, {}, make_pair(price_usd=1.0), "REJECT", None, False, now=now - 3 * 3600)
    L.record(db, addr("nodata"), {}, make_pair(base_mint=addr("nodata"), pair_address=addr("p2"), price_usd=1.0),
             "REJECT", None, False, now=now - 3 * 3600)

    class CG:
        async def candles(self, pool, start, end=None, urgent=False):
            if pool == addr("p2"):
                return []
            return [(start + 60 * i, 1, 1 + i * 0.1, 0.95, 1 + i * 0.05, 0) for i in range(5)]
    for _ in range(3):
        asyncio.run(L.evaluate_due(db, CG(), now=now))
    good = db.fetchone("SELECT * FROM shadow_calls WHERE mint=?", (MINT,))
    assert good["evaluated"] == 1 and abs(good["peak_x"] - 1.4) < 1e-9 and good["minutes_to_peak"] == 4.0
    bad = db.fetchone("SELECT * FROM shadow_calls WHERE mint=?", (addr("nodata"),))
    assert bad["evaluated"] == -1 and bad["attempts"] == 3


def _seed(db, n, now, winner_min_mc=150_000):
    """Coins above winner_min_mc run to 2.2x; smaller ones dump. Alternating so both halves see both."""
    L.init_schema(db)
    for i in range(n):
        mc = 200_000 if i % 2 else 80_000
        p = path((0, 1.0, 0.98, 1.0), (1, 1.6, 1.2, 1.5), (2, 2.2, 1.9, 2.1), (3, 2.1, 1.4, 1.45)) if mc >= winner_min_mc \
            else path((0, 1.0, 0.95, 1.0), (1, 1.02, 0.5, 0.55))
        db.execute("INSERT INTO shadow_calls(mint, ts, pool, entry_price, entry_mcap, liquidity, action, called, features, "
                   "evaluated, path, peak_x, trough_x) VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?)",
                   (addr(f"m{i}"), now - 86400 + i * 60, addr(f"p{i}"), 1.0, mc, 40_000, "WATCHLIST", 0,
                    json.dumps(feats()), json.dumps(p), max(x[1] for x in p), min(x[2] for x in p)))


def test_tune_raises_market_cap_floor_one_step_and_announces(db):
    s = Settings(_env_file=None, min_opportunity_score=40, min_confidence="MEDIUM", max_allowed_risk="MODERATE",
                 min_callout_market_cap=50_000, min_volume_acceleration=1.0, min_buy_sell_ratio_h1=1.2,
                 block_extended_moves=False)
    now = time.time()
    _seed(db, 60, now)
    changes = L.tune(db, s, days=14, min_samples=20, now=now)
    mc = [c for c in changes if c["key"] == "min_callout_market_cap"]
    assert mc and mc[0]["old"] == 50_000 and mc[0]["new"] == 75_000            # one grid step, not a jump
    assert s.min_callout_market_cap == 75_000
    assert db.fetchone("SELECT value FROM learned_settings WHERE key='min_callout_market_cap'")["value"] == 75_000
    assert "minimum market cap" in L.fmt_change(mc[0])
    for _ in range(3):
        L.tune(db, s, days=14, min_samples=20, now=now)
    assert s.min_callout_market_cap in (100_000, 150_000)                       # keeps moving toward the best


def test_tune_needs_enough_samples_and_out_of_sample_confirmation(db):
    s = Settings(_env_file=None, min_callout_market_cap=50_000)
    now = time.time()
    _seed(db, 10, now)
    assert L.tune(db, s, min_samples=20, now=now) == []
    assert s.min_callout_market_cap == 50_000


def test_learned_values_apply_and_reset(db):
    s = Settings(_env_file=None)
    L.init_schema(db)
    db.execute("INSERT INTO learned_settings(key, value, updated_at) VALUES ('plan_tp1', 2.5, 0), "
               "('plan_time_stop_minutes', 20, 0), ('not_a_tunable', 1, 0)")
    applied = L.apply_learned(db, s)
    assert s.plan_tp1 == 2.5 and s.plan_time_stop_minutes == 20 and "not_a_tunable" not in applied
    defaults = Settings(_env_file=None)
    assert set(L.reset(db, s, defaults)) == {"plan_tp1", "plan_time_stop_minutes", "not_a_tunable"}
    assert s.plan_tp1 == defaults.plan_tp1 and db.fetchone("SELECT COUNT(*) n FROM learned_settings")["n"] == 0


def test_engine_sync_picks_up_reset(settings, db):
    from conftest import MemoryPublisher
    from northstar.engine import Engine
    from northstar.research import Providers
    L.init_schema(db)
    db.execute("INSERT INTO learned_settings(key, value, updated_at) VALUES ('plan_stop', 0.8, 0)")
    original = settings.plan_stop
    eng = Engine(settings, db, Providers(), MemoryPublisher())
    assert settings.plan_stop == 0.8
    db.execute("DELETE FROM learned_settings")
    asyncio.run(eng._learning_sync())
    assert settings.plan_stop == original


def test_backfill_from_research_snapshots(db):
    L.init_schema(db)
    now = time.time()
    rep = {"market": {"price_usd": {"value": 0.001}, "market_cap": {"value": 150_000}, "liquidity_usd": {"value": 30_000}},
           "pool": {"address": addr("pool")}, "score": {"total": 45}, "risk": "MODERATE"}
    for ts in (now - 10 * 3600, now - 9.9 * 3600, now - 5 * 3600):
        db.execute("INSERT INTO research_snapshots(mint, ts, action, score, risk, report) VALUES (?,?,?,?,?,?)",
                   (MINT, ts, "WATCHLIST", 45, "MODERATE", json.dumps(rep)))
    assert L.backfill_from_snapshots(db, now=now) == 2          # one per coin per 2h
    assert L.backfill_from_snapshots(db, now=now) == 0          # only once
