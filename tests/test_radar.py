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
