"""v5.5.2: while Helius is rate limited (or out of credits) Solana reads go to a fallback RPC, so the safety checks
that need the chain still run and calls can be made. 7 Nov log: 318 failures in a row, every runner blocked by
"trap check: mint authority not passed", research ending INSUFFICIENT_DATA ("extensions not verified")."""
from __future__ import annotations

import asyncio
import json

import httpx

from northstar.config import Settings
from northstar.net import ProviderClient, RateLimited
from northstar.providers.solana_rpc import SolanaRpc


def client(name, handler, retries=0):
    return ProviderClient(name, "https://rpc.example", 10_000, 0, 5, retries,
                          transport=httpx.MockTransport(handler), circuit_threshold=3, circuit_cooldown=120)


def ok(value):
    return lambda req: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": value}})


def limited(req):
    return httpx.Response(429, json={"jsonrpc": "2.0", "error": {"code": -32429, "message": "max usage reached"}})


def test_a_rate_limited_main_rpc_falls_back():
    main, fb = client("solana_rpc", limited), client("solana_rpc_fallback", ok(7))
    rpc = SolanaRpc(main, "/", max_retries=0, fallback=fb)
    assert asyncio.run(rpc.call("getBalance", ["x"])) == {"value": 7}
    assert rpc.stats_snapshot()["fallback.calls"] == 1


def test_with_the_main_circuit_open_it_goes_straight_to_the_fallback():
    hits = []

    def main_h(req):
        hits.append(1)
        return limited(req)
    main, fb = client("solana_rpc", main_h), client("solana_rpc_fallback", ok(1))
    rpc = SolanaRpc(main, "/", max_retries=0, fallback=fb)
    for _ in range(4):
        asyncio.run(rpc.call("getBalance", ["x"]))
    assert main.circuit_open and len(hits) == 3          # after the circuit opened the main RPC is not asked


def test_without_a_fallback_the_error_is_still_an_error():
    rpc = SolanaRpc(client("solana_rpc", limited), "/", max_retries=0)
    try:
        asyncio.run(rpc.call("getBalance", ["x"]))
    except RateLimited:
        pass
    else:
        raise AssertionError("a rate limit must not turn into data")


def test_the_fallback_is_the_public_endpoint_by_default_and_can_be_switched_off():
    s = Settings(_env_file=None)
    assert s.solana_rpc_fallback_url == "https://api.mainnet-beta.solana.com"
    assert Settings(_env_file=None, solana_rpc_fallback_url="").solana_rpc_fallback_url == ""
