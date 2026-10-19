"""New pump.fun launches wait for evidence instead of taking a research slot each.

On the live bot this was 1,740 coins researched an hour with 44% coming back INSUFFICIENT DATA,
most of them "0 verified trades from 0 in-window transactions" — launches researched before they
had a pool or a single trade.
"""
from __future__ import annotations

import time

from northstar.discovery import Discovery


def test_a_new_launch_is_not_queued_for_research(settings, db):
    d = Discovery(settings, db, dex=None)
    assert d.ingest("So11111111111111111111111111111111111111112", "pumpportal_new") is True
    cand = db.get_candidate("So11111111111111111111111111111111111111112")
    assert cand["next_research_at"] is None, "a fresh launch waits for the curve radar"
    assert not [c for c in db.due_candidates(50) if c["mint"] == cand["mint"]]


def test_the_old_behaviour_is_one_setting_away(settings, db):
    settings.research_new_launches_on_timer = True
    d = Discovery(settings, db, dex=None)
    d.ingest("So11111111111111111111111111111111111111112", "pumpportal_new")
    cand = db.get_candidate("So11111111111111111111111111111111111111112")
    assert cand["next_research_at"] is not None
    assert cand["next_research_at"] >= time.time()


def test_a_migration_is_still_researched_at_once(settings, db):
    d = Discovery(settings, db, dex=None)
    d.ingest("4nJjZR1D7koAtbfuwCPuQ91F5bkwqgA2ENNWun6Kpump", "pumpportal_migration")
    cand = db.get_candidate("4nJjZR1D7koAtbfuwCPuQ91F5bkwqgA2ENNWun6Kpump")
    assert cand["next_research_at"] is not None, "a graduation has a pool and real data"


def test_the_radar_queues_a_launch_once_it_has_buyers(settings, db):
    """What the radar promotes has a pool, a market cap in the band and more buys than sells."""
    import asyncio

    from northstar.early_calls import CurveRadar

    from conftest import make_pair

    mint = "4nJjZR1D7koAtbfuwCPuQ91F5bkwqgA2ENNWun6Kpump"
    Discovery(settings, db, dex=None).ingest(mint, "pumpportal_new")
    assert db.get_candidate(mint)["next_research_at"] is None

    class Dex:
        async def tokens_batch(self, mints):
            return [make_pair(dex_id="pumpfun", base_mint=mint, market_cap=38_000.0, fdv=38_000.0,
                              liquidity_usd=12_000.0, txns={"m5": {"buys": 40, "sells": 9}})]

    promoted = asyncio.new_event_loop().run_until_complete(CurveRadar(settings, db, Dex()).run_once())
    assert promoted == [mint]
    assert db.get_candidate(mint)["next_research_at"] is not None, "now it earns a research slot"


def test_launches_already_in_the_queue_are_released_on_start(settings, db):
    """Changing the rule is not enough: the backlog keeps its slots until it is cleared."""
    import time as _t

    queued = "4nJjZR1D7koAtbfuwCPuQ91F5bkwqgA2ENNWun6Kpump"
    promoted = "CgfbhqzqSHoygUwdywzRo8o9wZEbLtRAVYw8fXkPjiaFxx"
    graduated = "So11111111111111111111111111111111111111112"
    for m in (queued, promoted, graduated):
        db.upsert_discovery(m, "pumpportal_new", 1.0)
    db.upsert_discovery(promoted, "curve_radar", 9.0)
    db.upsert_discovery(graduated, "pumpportal_migration", 3.0)
    now = _t.time()
    for m in (queued, promoted, graduated):
        db.execute("UPDATE candidates SET next_research_at=? WHERE mint=?", (now, m))

    assert db.unschedule_stale_launches() == 1
    assert db.get_candidate(queued)["next_research_at"] is None
    assert db.get_candidate(promoted)["next_research_at"] is not None, "the radar already chose it"
    assert db.get_candidate(graduated)["next_research_at"] is not None, "it has a pool now"


def test_a_launch_that_was_already_researched_keeps_its_schedule(settings, db):
    mint = "4nJjZR1D7koAtbfuwCPuQ91F5bkwqgA2ENNWun6Kpump"
    db.upsert_discovery(mint, "pumpportal_new", 1.0)
    db.execute("UPDATE candidates SET next_research_at=?, last_research_at=?, research_count=1 "
               "WHERE mint=?", (time.time(), time.time(), mint))
    assert db.unschedule_stale_launches() == 0


# nothing to judge yet is not a decision ----------------------------------------------------------
def test_a_coin_with_no_pool_is_not_recorded_as_a_decision(settings, db):
    """61% of the live panel was INSUFFICIENT DATA for coins that had nothing to look at."""
    from conftest import strong_providers
    from northstar.research import Researcher

    p = strong_providers()
    p.dex.pairs = []                                   # DexScreener knows no pool for this mint
    mint = "So11111111111111111111111111111111111111112"
    db.upsert_discovery(mint, "pumpportal_new", 1.0)

    import asyncio
    out = asyncio.new_event_loop().run_until_complete(
        Researcher(settings, db, p).research(mint, db.get_candidate(mint)))

    assert out.not_ready is True
    assert out.report["final_action"] == "NOT_READY"
    assert db.fetchall("SELECT id FROM research_snapshots WHERE mint=?", (mint,)) == [], \
        "a coin with nothing to judge leaves no verdict behind"
