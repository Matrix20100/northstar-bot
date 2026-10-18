"""OPTIONAL live integration tests against real public APIs.

Opt in:  NORTHSTAR_LIVE_TESTS=1 pytest -m live -q
These never send Discord messages. They check that endpoints exist and that response schemas
still match what the parsers expect. Results depend on the network and on third-party uptime.
"""
from __future__ import annotations

import pytest

from conftest import LIVE
from northstar.config import Settings
from northstar.engine import build_providers

pytestmark = [pytest.mark.live, pytest.mark.skipif(not LIVE, reason="set NORTHSTAR_LIVE_TESTS=1 to run")]

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"  # long-lived public token, used only as a schema probe


@pytest.fixture
async def providers(tmp_path):
    s = Settings(data_dir=str(tmp_path), dry_run=True, discord_enabled=False)
    p, clients = build_providers(s, None)
    yield p
    for c in clients:
        await c.close()


async def test_live_dexscreener_schema(providers):
    profiles = await providers.dex.latest_profiles()
    assert isinstance(profiles, list)
    pairs = await providers.dex.token_pairs(BONK)
    assert pairs and pairs[0].base_mint == BONK and pairs[0].price_usd
    assert set(pairs[0].vol) == {"m5", "h1", "h6", "h24"}
    metas = await providers.dex.trending_metas()
    assert isinstance(metas, list)


async def test_live_rugcheck_schema(providers):
    rep = await providers.rugcheck.report(BONK)
    assert "token" in rep and "risks" in rep and "markets" in rep


async def test_live_rpc_mint_account(providers):
    from northstar.analysis.identity import parse_mint_account
    acc = parse_mint_account(await providers.rpc.get_account_info(BONK))
    assert acc.verified_mint


async def test_live_full_analysis_without_discord(tmp_path):
    from northstar.db import Database
    from northstar.research import Researcher
    s = Settings(data_dir=str(tmp_path), dry_run=True, discord_enabled=False, max_token_age_hours=0,
                 swap_sample_signatures=20, funding_trace_wallets=3)
    db = Database(tmp_path / "live.db")
    db.open()
    p, clients = build_providers(s, db)
    try:
        out = await Researcher(s, db, p).research(BONK, force=True)
        assert out.action.value in ("AUTO_POST", "WATCHLIST", "REJECT", "INSUFFICIENT_DATA")
        assert out.report["pool"]["address"]
    finally:
        for c in clients:
            await c.close()
        db.close()
