"""Surge radar: detection rules, watchlist selection and immediate research. SYNTHETIC data only."""
from __future__ import annotations

import asyncio
import time

from conftest import MINT, make_pair
from northstar.config import Settings
from northstar.radar import SurgeRadar, detect

S = Settings(_env_file=None)


def pair(**kw):
    base = dict(market_cap=150_000.0, liquidity_usd=30_000.0, vol={"m5": 12_000.0, "h1": 20_000.0},
                txns={"m5": {"buys": 40, "sells": 15}, "h1": {"buys": 80, "sells": 50}},
                price_change={"m5": 18.0, "h1": 40.0})
    base.update(kw)
    return make_pair(**base)


def test_surge_detected():
    s = detect(pair(), S)
    assert s is not None and s.buys_m5 == 40
    assert round(s.baseline_5m) == 727 and s.spike > 16         # 12k vs (20k-12k)/11
    assert "⚡ Surge: $12,000 volume in 5 min" in s.line()


def test_no_surge_cases():
    assert detect(pair(vol={"m5": 12_000.0, "h1": 130_000.0}), S) is None      # steady volume, no spike
    assert detect(pair(vol={"m5": 1_000.0, "h1": 1_500.0}), S) is None          # too small
    assert detect(pair(txns={"m5": {"buys": 20, "sells": 25}}), S) is None      # sellers dominate
    assert detect(pair(price_change={"m5": -5.0, "h1": 10.0}), S) is None       # falling
    assert detect(pair(price_change={"m5": 90.0, "h1": 120.0}), S) is None      # already vertical
    assert detect(pair(price_change={"m5": 10.0, "h1": 450.0}), S) is None      # move already happened
    assert detect(pair(market_cap=10_000.0), S) is None
    assert detect(pair(liquidity_usd=4_000.0), S) is None


class FakeDex:
    def __init__(self, pairs):
        self.pairs, self.calls = pairs, []

    async def tokens_batch(self, mints):
        self.calls.append(list(mints))
        return [p for p in self.pairs if p.base_mint in mints]


def test_radar_records_and_cools_down(db):
    db.upsert_discovery(MINT, "pumpportal_migration", 3.0, "T", "T")
    dex = FakeDex([pair()])
    r = SurgeRadar(S, db, dex)
    surges = asyncio.run(r.run_once())
    assert [x.mint for x in surges] == [MINT]
    assert "radar_surge" in db.get_candidate(MINT)["sources"]
    assert asyncio.run(r.run_once()) == []                                     # cooldown
    assert asyncio.run(r.run_once(now=time.time() + 21 * 60)) != []


def test_radar_ignores_old_and_permanently_rejected(db):
    db.upsert_discovery(MINT, "pumpportal_migration", 3.0, "T", "T")
    db.set_state(MINT, "REJECTED", next_research_at=None)
    r = SurgeRadar(S, db, FakeDex([pair()]))
    assert asyncio.run(r.run_once()) == []
    assert asyncio.run(SurgeRadar(S, db, FakeDex([pair()])).run_once(now=time.time() + 7 * 3600)) == []


def test_priority_inflation_capped_and_not_repeated(db):
    db.upsert_discovery(MINT, "pumpportal_new", 1.0)
    db.upsert_discovery(MINT, "dexscreener_profile", 2.0)
    first = db.get_candidate(MINT)["priority"]
    for _ in range(50):
        db.upsert_discovery(MINT, "dexscreener_profile", 2.0)
    assert db.get_candidate(MINT)["priority"] == first == 2.5


def test_engine_researches_surge_immediately(settings, db, monkeypatch):
    from northstar.engine import Engine
    from northstar.research import Providers
    from conftest import MemoryPublisher
    settings.radar_enabled = True
    db.upsert_discovery(MINT, "pumpportal_migration", 3.0, "T", "T")
    db.set_state(MINT, "REJECTED", next_research_at=time.time() + 3600)       # normally waits an hour
    eng = Engine(settings, db, Providers(dex=FakeDex([pair()])), MemoryPublisher())
    seen = []

    async def fake_process(mint):
        seen.append(mint)
    monkeypatch.setattr(eng, "_process", fake_process)

    async def go():
        await eng._radar()
        await asyncio.gather(*list(eng._radar_tasks))
    asyncio.run(go())
    assert seen == [MINT]


def test_review_new_or_lagging_pools_are_not_surges():
    assert detect(pair(pair_created_at=time.time() - 120), S) is None              # 2-minute-old pool
    assert detect(pair(vol={"m5": 12_000.0, "h1": 12_000.0}), S) is None           # 1h == 5m: no baseline
    assert detect(pair(vol={"m5": 12_000.0, "h1": None}), S) is None


def test_radar_records_surges_and_near_misses_for_learning(db):
    from northstar import learning as L
    L.init_schema(db)
    db.upsert_discovery(MINT, "pumpportal_migration", 3.0, "T", "T")
    db.upsert_discovery(addr_b := "So11111111111111111111111111111111111111112", "pumpportal_migration", 3.0, "U", "U")
    weak = make_pair(base_mint=addr_b, pair_address="Pool2222222222222222222222222222222222222222",
                     market_cap=150_000.0, liquidity_usd=30_000.0, vol={"m5": 2_500.0, "h1": 14_000.0},
                     txns={"m5": {"buys": 9, "sells": 7}}, price_change={"m5": 4.0, "h1": 10.0})
    r = SurgeRadar(S, db, FakeDex([pair(), weak]))
    surges = asyncio.run(r.run_once())
    assert [x.mint for x in surges] == [MINT]
    obs = {o["mint"]: o for o in db.fetchall("SELECT mint, fired FROM radar_obs")}
    assert obs[MINT]["fired"] == 1 and obs[addr_b]["fired"] == 0           # near-miss kept for learning
    asyncio.run(r.run_once())
    assert db.fetchone("SELECT COUNT(*) n FROM radar_obs")["n"] == 2       # max one per coin per 20 min


def test_radar_thresholds_are_learned_from_outcomes(db):
    """Near-misses with a 1.8x spike keep running; the learner should lower the spike bar a step."""
    import json
    from northstar import learning as L
    from northstar.config import Settings
    L.init_schema(db)
    s = Settings(_env_file=None, radar_volume_spike=2.5)
    now = time.time()
    run = [[0, 1.0, 0.97, 1.0], [5, 1.6, 1.2, 1.5], [10, 2.4, 1.9, 2.2], [30, 2.3, 1.7, 1.8]]
    dud = [[0, 1.0, 0.9, 0.95], [3, 1.0, 0.5, 0.55]]
    for i in range(80):
        spike = 1.8 if i % 2 else 6.0
        good = (i % 2 == 1) or (i % 4 == 0)
        m = {"mc": 150_000, "liq": 30_000, "vol_m5": 8_000, "baseline": 8_000 / spike, "spike": spike, "buys": 40,
             "sells": 10, "c5": 10.0, "c1": 30.0}
        db.execute("INSERT INTO radar_obs(mint, ts, pool, price, metrics, fired, evaluated, path) VALUES (?,?,?,?,?,?,1,?)",
                   (f"m{i}", now - 86400 + i * 300, f"p{i}", 1.0, json.dumps(m), 1 if spike >= 2.5 else 0,
                    json.dumps(run if good else dud)))
    changes = L.tune(db, s, days=14, min_samples=20)
    spike_change = [c for c in changes if c["key"] == "radar_volume_spike"]
    assert spike_change and spike_change[0]["new"] == 2.0 and s.radar_volume_spike == 2.0


def test_near_miss_logging_is_capped_but_surges_never_are(db):
    from northstar import learning as L
    from northstar import radar as R
    L.init_schema(db)
    r = SurgeRadar(S, db, FakeDex([]))
    now = 1_800_000_000.0
    weak = dict(market_cap=150_000.0, liquidity_usd=30_000.0, vol={"m5": 2_500.0, "h1": 14_000.0},
                txns={"m5": {"buys": 9, "sells": 7}}, price_change={"m5": 4.0, "h1": 10.0})
    for i in range(R.NEAR_MISS_PER_HOUR + 10):
        r._observe(f"w{i}", make_pair(base_mint=f"w{i}", **weak), now)
    r._observe("surge1", pair(base_mint="surge1"), now)
    assert db.fetchone("SELECT COUNT(*) n FROM radar_obs WHERE fired=0")["n"] == R.NEAR_MISS_PER_HOUR
    assert db.fetchone("SELECT COUNT(*) n FROM radar_obs WHERE fired=1")["n"] == 1
    r._observe("w_next_hour", make_pair(base_mint="w_next_hour", **weak), now + 3600)
    assert db.fetchone("SELECT COUNT(*) n FROM radar_obs WHERE fired=0")["n"] == R.NEAR_MISS_PER_HOUR + 1
