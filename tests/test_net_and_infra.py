"""HTTP layer, persistence, discovery, logging — MOCK transports only (no network)."""
from __future__ import annotations

import logging
import time

import httpx
import pytest

from conftest import MINT, addr
from northstar.analysis.identity import is_valid_mint_format, select_pool
from northstar.db import Database
from northstar.discovery import Discovery
from northstar.logging_setup import redact, setup_logging
from northstar.models import DataStatus, Datum
from northstar.net import BudgetExceeded, CircuitOpen, NotFound, ProviderClient, RateLimited
from northstar.providers.dexscreener import DexScreener, parse_pair
from conftest import make_pair


def client(handler, **kw) -> ProviderClient:
    return ProviderClient("mock", "https://mock.invalid", rpm=6000, transport=httpx.MockTransport(handler),
                          max_retries=kw.pop("max_retries", 3), **kw)


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    import asyncio
    real = asyncio.sleep

    async def quick(t, *a, **k):
        await real(0)
    monkeypatch.setattr("northstar.net.asyncio.sleep", quick)
    monkeypatch.setattr("northstar.net._retry_after", lambda resp: 0.01)


async def test_rate_limit_then_success_with_backoff():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json=[{"ok": 1}])
    c = client(h)
    assert await c.request("GET", "/x") == [{"ok": 1}]
    assert calls["n"] == 2


async def test_persistent_rate_limit_raises_not_fabricated():
    c = client(lambda r: httpx.Response(429), max_retries=2)
    with pytest.raises(RateLimited):
        await c.request("GET", "/x")


async def test_server_errors_retry_then_circuit_opens():
    c = client(lambda r: httpx.Response(503), max_retries=1, circuit_threshold=3)
    for _ in range(2):
        with pytest.raises(Exception):
            await c.request("GET", "/x")
    assert c.circuit_open
    with pytest.raises(CircuitOpen):
        await c.request("GET", "/x")


async def test_404_is_not_found_and_invalid_json_is_error():
    c = client(lambda r: httpx.Response(404))
    with pytest.raises(NotFound):
        await c.request("GET", "/x")
    c2 = client(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(Exception):
        await c2.request("GET", "/x")


async def test_cache_and_request_coalescing():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(200, json={"v": calls["n"]})
    c = client(h)
    import asyncio
    a, b = await asyncio.gather(c.request("GET", "/x", cache_ttl=60), c.request("GET", "/x", cache_ttl=60))
    assert a == b and calls["n"] == 1
    assert (await c.request("GET", "/x", cache_ttl=60)) == a and calls["n"] == 1


async def test_daily_budget_enforced():
    c = client(lambda r: httpx.Response(200, json={}), daily_budget=2)
    await c.request("GET", "/a")
    await c.request("GET", "/b")
    with pytest.raises(BudgetExceeded):
        await c.request("GET", "/c")


async def test_health_callback_records(db):
    c = client(lambda r: httpx.Response(200, json={}), health_cb=db.record_provider)
    await c.request("GET", "/a")
    row = db.provider_health()[0]
    assert row["provider"] == "mock" and row["status"] == "OK" and row["budget_used"] == 1


async def test_dexscreener_parses_and_skips_malformed():
    good = {"chainId": "solana", "dexId": "raydium", "pairAddress": addr("p"), "baseToken": {"address": MINT,
            "symbol": "M", "name": "M"}, "quoteToken": {"address": addr("q")}, "priceUsd": "0.1",
            "liquidity": {"usd": 1000}, "txns": {"h1": {"buys": 1, "sells": 2}}, "volume": {"h1": 5},
            "pairCreatedAt": 1700000000000}
    bad = [{"chainId": "solana"}, {"chainId": "ethereum", "pairAddress": "x"}, "garbage", None]

    def h(req):
        return httpx.Response(200, json=[good, *bad])
    tr = httpx.MockTransport(h)
    dex = DexScreener(ProviderClient("d", "https://x.invalid", 600, transport=tr),
                      ProviderClient("d2", "https://x.invalid", 600, transport=tr))
    pairs = await dex.token_pairs(MINT)
    assert len(pairs) == 1 and pairs[0].price_usd == 0.1 and pairs[0].vol["m5"] is None
    assert pairs[0].pair_created_at == 1700000000.0
    assert parse_pair({"chainId": "solana", "pairAddress": "a", "baseToken": {"address": "b"},
                       "priceUsd": "NaN"}).price_usd is None


def test_mint_format_validation():
    assert is_valid_mint_format(MINT)
    assert is_valid_mint_format("So11111111111111111111111111111111111111112")
    for bad in ("", "abc", "0OIl" * 11, MINT + "x", None):
        assert not is_valid_mint_format(bad)


def test_pool_selection_not_first_and_rejects_outliers():
    small = make_pair(pair_address=addr("small"), liquidity_usd=5_000.0)
    big = make_pair(pair_address=addr("big"), liquidity_usd=200_000.0)
    off = make_pair(pair_address=addr("off"), liquidity_usd=900_000.0, price_usd=0.01, quote_mint=addr("weird"))
    sel = select_pool(MINT, [small, off, big])
    assert sel.pair.pair_address == addr("big")
    assert "highest liquidity" in sel.reason


def test_datum_freshness_marks_stale_never_zero():
    d = Datum(1.5, DataStatus.OBSERVED, "x", ts=time.time() - 1000)
    s = d.freshen(300)
    assert s.status == DataStatus.STALE and s.value == 1.5
    assert not Datum.unknown("x").usable


def test_secret_redaction(capsys):
    setup_logging("INFO", False, ["supersecretkey123"])
    logging.getLogger("t").info("calling https://mainnet.helius-rpc.com/?api-key=abcdef12345 with supersecretkey123")
    out = capsys.readouterr().out
    assert "supersecretkey123" not in out and "abcdef12345" not in out
    assert redact("Bot " + "x" * 40).endswith("***REDACTED***")


# ------------------------------------------------------------------- persistence / discovery
def test_duplicate_discoveries_dedup_by_mint(db, settings):
    disc = Discovery(settings, db, None)
    assert disc.ingest(MINT, "dexscreener_profile", {"description": "a"}) is True
    assert disc.ingest(MINT, "dexscreener_profile") is False
    assert disc.ingest(MINT, "pumpportal_migration") is False
    rows = db.fetchall("SELECT * FROM candidates")
    assert len(rows) == 1
    import json
    assert set(json.loads(rows[0]["sources"])) == {"dexscreener_profile", "pumpportal_migration"}
    assert disc.ingest("bad-mint", "dexscreener_profile") is False and disc.invalid_seen == 1


def test_new_pumpfun_launch_waits_for_the_curve_radar(db, settings):
    """A launch costs nothing until it shows buying; see tests/test_launch_queue.py."""
    disc = Discovery(settings, db, None)
    disc.ingest(MINT, "pumpportal_new", {"traderPublicKey": addr("dev")})
    assert db.get_candidate(MINT)["next_research_at"] is None
    assert db.due_candidates(10) == []


def test_new_pumpfun_launch_can_still_be_put_on_a_timer(db, settings):
    settings.research_new_launches_on_timer = True
    disc = Discovery(settings, db, None)
    disc.ingest(MINT, "pumpportal_new", {"traderPublicKey": addr("dev")})
    c = db.get_candidate(MINT)
    assert c["next_research_at"] > time.time() + settings.new_token_initial_delay_seconds - 2
    assert c["next_research_at"] < time.time() + settings.new_token_initial_delay_seconds + 2
    assert db.due_candidates(10) == []


def test_restart_recovery_requeues_interrupted(db):
    db.upsert_discovery(MINT, "x", 1)
    db.set_state(MINT, "RESEARCHING", next_research_at=None)
    assert db.recover_after_restart() == 1
    assert db.get_candidate(MINT)["state"] == "DISCOVERED"
    assert db.due_candidates(5)[0]["mint"] == MINT


def test_database_corruption_is_quarantined(tmp_path):
    p = tmp_path / "corrupt.db"
    p.write_bytes(b"this is not a sqlite database" * 100)
    d = Database(p)
    d.open()
    assert d.recovered_from_corruption
    assert list(tmp_path.glob("corrupt.db.corrupt-*")), "original kept for forensics, not deleted"
    d.upsert_discovery(MINT, "x", 1)
    assert d.get_candidate(MINT)
    d.close()


def test_persistence_survives_reopen(tmp_path):
    d = Database(tmp_path / "p.db")
    d.open()
    d.upsert_discovery(MINT, "x", 1)
    d.close()
    d2 = Database(tmp_path / "p.db")
    d2.open()
    assert d2.get_candidate(MINT)["state"] == "DISCOVERED"
    d2.close()


def test_instance_lock_prevents_duplicate_workers(tmp_path):
    from northstar.engine import InstanceLock
    a, b = InstanceLock(tmp_path / "x.lock"), InstanceLock(tmp_path / "x.lock")
    a.acquire()
    with pytest.raises(RuntimeError):
        b.acquire()
    a.release()
    b.acquire()
    b.release()
