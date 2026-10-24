"""v4.5.1: blocking for good needs a coin that is really gone; RugCheck's "revoked" answer counts when
the on-chain read fails; the radar rejects pools older than the age limit without a research."""
from __future__ import annotations

import asyncio
import time

import pytest

from conftest import MINT, FakeRpc, addr, make_pair, seed_profile, strong_providers
from northstar import brain as B
from northstar import rugs as RG
from northstar.net import ProviderError
from northstar.radar import SurgeRadar
from northstar.research import Researcher


@pytest.fixture(autouse=True)
def _dead_line():
    old = B.DEAD_MCAP
    B.DEAD_MCAP = 5_000.0
    yield
    B.DEAD_MCAP = old


def test_a_big_fall_is_not_a_confirmed_rug(settings):
    # live: $ChatGPT blocked at $65K after a $386K high (0.17x)
    assert RG.confirmed(65_017, 120_000, True, 386_557, 130_000, settings) is None
    assert RG.confirmed(30_000, None, False, 386_557, None, settings).startswith("died")      # down 92%
    assert RG.confirmed(4_500, None, False, 17_000, None, settings).startswith("died")        # at $5K after $10K+
    assert RG.confirmed(6_156, None, False, 22_731, None, settings) is None                  # $COGE: 0.27x, alive


class RateLimitedMintRpc(FakeRpc):
    async def get_account_info(self, a):
        raise ProviderError("solana_rpc", "HTTP 429 rate limited")


async def test_rugcheck_revoked_counts_when_the_chain_read_fails(settings, db):
    p = strong_providers()
    p.rpc = RateLimitedMintRpc()
    seed_profile(db)
    out = await Researcher(settings, db, p).research(MINT)
    gates = {g["name"]: g for g in out.report.get("gates") or []}
    for name in ("mint_authority", "freeze_authority"):
        assert gates[name]["status"] == "PASS", gates[name]
        assert "per RugCheck" in gates[name]["reason"]


def test_radar_rejects_old_pools_without_research(settings, db):
    old = make_pair(market_cap=900_000.0, liquidity_usd=200_000.0, pair_created_at=time.time() - 19_207 * 3600)

    class Dex:
        async def tokens_batch(self, mints):
            return [old]
    settings.max_token_age_hours = 8
    db.upsert_discovery(MINT, "coingecko_trending", 4.0, "USD Coin", "USDC")
    db.execute("UPDATE candidates SET pool_address = 'pool'")
    assert asyncio.run(SurgeRadar(settings, db, Dex()).run_once()) == []
    c = db.get_candidate(MINT)
    assert c["state"] == "REJECTED" and c["next_research_at"] is None and c["last_action"] == "REJECT"
    assert "pool age 19207h exceeds max 8h" in c["reasons"]


def test_radar_block_shows_as_a_reject(settings, db):
    dead = make_pair(market_cap=8_000.0, liquidity_usd=3.0)

    class Dex:
        async def tokens_batch(self, mints):
            return [dead]
    m = addr("dd")
    db.upsert_discovery(m, "pumpportal_migration", 3.0, "D", "D")
    db.execute("UPDATE candidates SET pool_address='pool', last_action='INSUFFICIENT_DATA'")
    dead.base_mint = m
    asyncio.run(SurgeRadar(settings, db, Dex()).run_once())
    c = db.get_candidate(m)
    assert c["state"] == "RUGGED" and c["last_action"] == "REJECT"


def test_coins_blocked_by_the_old_rule_are_released(settings, db):
    import json
    keep, free = addr("keep"), addr("free")
    for m, why in ((keep, "confirmed rug: died: market cap $3,152 after a $14,594 high (seen by the surge radar)"),
                   (free, "confirmed rug: died: market cap $65,017 after a $386,557 high (seen by the surge radar)")):
        db.upsert_discovery(m, "pumpportal_migration", 3.0, "x", "X")
        db.execute("UPDATE candidates SET state='RUGGED', next_research_at=NULL, reasons=? WHERE mint=?",
                   (json.dumps([why]), m))
    assert RG.release_wrong_blocks(db, settings) == 1
    assert db.get_candidate(keep)["state"] == "RUGGED"
    c = db.get_candidate(free)
    assert c["state"] == "REJECTED" and c["next_research_at"] is None and "unblocked" in c["reasons"]
