"""Whole-engine run on MOCK providers: discovery -> research -> publish -> monitor -> shutdown."""
from __future__ import annotations

import asyncio

from conftest import MINT, MemoryPublisher, strong_providers
from northstar.engine import Engine


async def test_engine_end_to_end_with_mocks(settings, db):
    p = strong_providers()

    async def profiles():
        return [{"chainId": "solana", "tokenAddress": MINT,
                 "description": "Mock Frog is a community frog meme on Solana with daily art drops.",
                 "links": [{"type": "twitter", "url": "https://x.com/mockfrogsol"},
                           {"type": "telegram", "url": "https://t.me/mockfrogportal"}]}]

    async def broken():
        raise RuntimeError("MOCK: unexpected feed bug")  # must not kill the loop

    p.dex.latest_profiles = profiles
    p.dex.community_takeovers = broken
    settings.discovery_interval_seconds = 1
    settings.monitor_interval_seconds = 1
    pub = MemoryPublisher()
    eng = Engine(settings, db, p, pub)
    task = asyncio.create_task(eng.run())
    for _ in range(100):
        await asyncio.sleep(0.05)
        if pub.sent:
            break
    await asyncio.sleep(0.2)
    eng.request_stop()
    await asyncio.wait_for(task, timeout=5)
    assert len(pub.sent) == 1, "exactly one callout"
    assert db.get_candidate(MINT)["state"] in ("PUBLISHED", "WARNING")
    assert eng.stats["auto_post"] == 1


async def test_engine_publishes_nothing_when_nothing_qualifies(settings, db):
    p = strong_providers()
    p.dex.metas = []

    async def profiles():
        return [{"chainId": "solana", "tokenAddress": MINT}]

    p.dex.latest_profiles = profiles
    settings.discovery_interval_seconds = 1
    pub = MemoryPublisher()
    eng = Engine(settings, db, p, pub)
    task = asyncio.create_task(eng.run())
    for _ in range(40):
        await asyncio.sleep(0.05)
        if eng.stats["researched"]:
            break
    eng.request_stop()
    await asyncio.wait_for(task, timeout=5)
    assert eng.stats["researched"] >= 1
    assert pub.sent == [], "zero alerts is better than manufacturing confidence"
    assert db.get_candidate(MINT)["state"] in ("WATCHLIST", "REJECTED")
