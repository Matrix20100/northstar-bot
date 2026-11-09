"""v5.6.0: the bot on a 1 vCPU / 1 GiB server with a free RPC plan.
7 Nov: Discord "heartbeat blocked for more than 10 seconds" with the traceback in db.fetchall (one connection, one
lock, shared with the retention / learning threads); Helius out of credits in 5 days (200,000 a day budget, a write
transaction per HTTP request, 200+ RPC calls of deep research on coins already rejected)."""
from __future__ import annotations

import asyncio
import threading
import time

import httpx

from northstar.config import Settings
from northstar.db import Database, ProviderLedger
from northstar.net import ProviderClient


def test_background_threads_get_their_own_connection(db):
    main = db._conn
    seen = {}

    def work():
        seen["conn"] = db._conn
        seen["lock"] = db._lock
        db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('from_thread', '1')")

    t = threading.Thread(target=work)
    t.start()
    t.join()
    assert seen["conn"] is not main and seen["lock"] is not db._main_lock
    assert db.fetchone("SELECT value FROM meta WHERE key = 'from_thread'")["value"] == "1"


def test_a_long_read_in_a_thread_does_not_block_the_main_thread(db):
    """A thread holding its connection busy for a second must not stop the main thread's tiny query."""
    started, release = threading.Event(), threading.Event()

    def work():
        with db._lock:                          # the thread's own lock, held for the whole wait
            db.fetchall("SELECT * FROM meta")
            started.set()
            release.wait(2)

    t = threading.Thread(target=work)
    t.start()
    started.wait(2)
    t0 = time.monotonic()
    db.fetchall("SELECT key FROM meta")
    took = time.monotonic() - t0
    release.set()
    t.join()
    assert took < 0.5, f"main thread waited {took:.2f}s on a background thread"


def test_provider_ledger_counts_in_memory_and_flushes(db):
    led = ProviderLedger(db, flush_seconds=3600)
    for _ in range(50):
        led.record("rpc", True)
    assert led.used_today("rpc") == 50
    assert db.budget_used_today("rpc") == 1       # only the first (UNKNOWN -> OK status change) was written
    led.flush()
    row = {r["provider"]: r for r in db.provider_health()}["rpc"]
    assert row["ok_count"] == 50 and row["budget_used"] == 50 and row["status"] == "OK"
    led.record("rpc", False, "HTTP 500")                                           # status change: written at once
    assert {r["provider"]: r for r in db.provider_health()}["rpc"]["status"] == "DEGRADED"
    for _ in range(5):
        led.record("rpc", False, "HTTP 500")
    assert {r["provider"]: r for r in db.provider_health()}["rpc"]["status"] == "DOWN"
    # a fresh ledger (restart) picks up today's spend
    led.flush()
    assert ProviderLedger(db).used_today("rpc") == 56


def test_the_ledger_feeds_the_clients_budget(db):
    led = ProviderLedger(db, flush_seconds=3600)
    c = ProviderClient("rpc", "https://x.example", 10_000, 3, 5, 0, health_cb=led.record, budget_cb=led.used_today,
                       transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    for _ in range(3):
        asyncio.run(c.request("GET", "/a"))
    try:
        asyncio.run(c.request("GET", "/a"))
    except Exception as exc:  # noqa: BLE001
        assert "budget" in str(exc)
    else:
        raise AssertionError("the 4th request must exceed the budget of 3")


def test_server_limits():
    s = Settings(_env_file=None, research_concurrency=10, insufficient_data_recheck_seconds=30)
    assert s.research_concurrency == 4 and s.insufficient_data_recheck_seconds == 120
    assert Settings(_env_file=None).rpc_daily_budget == 30_000
    assert Settings(_env_file=None).shadow_test_deep_per_hour == 4


def test_response_cache_is_capped():
    n = {"i": 0}

    def h(r):
        n["i"] += 1
        return httpx.Response(200, json={"i": n["i"]})
    c = ProviderClient("x", "https://x.example", 10_000, 0, 5, 0, transport=httpx.MockTransport(h), cache_max=10)
    for i in range(30):
        asyncio.run(c.request("GET", f"/p{i}", cache_ttl=60))
    assert len(c._cache) <= 10


def test_a_zero_market_cap_is_a_dead_coin_not_missing_data(db):
    """7 Nov: 98 coins blocked as "died: market cap $0 after a $X high" were unblocked at every restart."""
    from northstar import rugs as RG
    from conftest import addr
    s = Settings(_env_file=None)
    assert RG.confirmed(0.0, None, False, 175_969, None, s).startswith("died")
    assert RG.confirmed(None, None, False, 175_969, None, s) is None
    mint = addr("dead")
    db.upsert_discovery(mint, "radar_surge", 1.0, "Dead", "DEAD", {})
    RG.block(db, mint, "died: market cap $0 after a $175,969 high (seen by the surge radar)", time.time(),
             last_action="REJECT")
    assert RG.release_wrong_blocks(db, s) == 0
    assert db.fetchone("SELECT state FROM candidates WHERE mint = ?", (mint,))["state"] == RG.STATE
