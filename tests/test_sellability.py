"""Jupiter exit-cost checks: can you actually sell, and what does it cost? SYNTHETIC quotes."""
from __future__ import annotations

import asyncio

import pytest

from conftest import MINT
from northstar.analysis.verdict import assess
from northstar.net import ProviderError
from northstar.providers.jupiter import SOL, Jupiter


class Client:
    """Returns a quote per requested amount; `fail` raises, `no_route` answers 4xx."""

    def __init__(self, by_amount=None, fail=False, status=None):
        self.by_amount, self.fail, self.status = by_amount or {}, fail, status
        self.calls = []

    async def request(self, method, path, params=None, cache_ttl=0):
        self.calls.append(params)
        if self.fail:
            raise ProviderError("jupiter", "boom", self.status)
        amt = int(params["amount"])
        hit = self.by_amount.get(amt)
        if hit is None:
            raise ProviderError("jupiter", "no route", 400)
        return hit


def quote(out=1_000_000, impact="0.0123", hops=1):
    return {"outAmount": str(out), "priceImpactPct": impact, "routePlan": [{}] * hops}


def test_quote_shapes():
    j = Jupiter(Client({1000: quote(impact="0.25")}))
    q = asyncio.run(j.quote(MINT, 1000))
    assert q == {"no_route": False, "out_amount": 1_000_000.0, "price_impact_pct": 25.0, "hops": 1}
    assert asyncio.run(Jupiter(Client({})).quote(MINT, 5)) == {"no_route": True}        # 400 = no route
    assert asyncio.run(Jupiter(Client(fail=True, status=500)).quote(MINT, 5)) is None   # provider down
    assert asyncio.run(j.quote(MINT, 0)) is None


def test_sellability_walks_sizes_and_reports_the_worst():
    # price $0.001 with 6 decimals: $100 buys 100,000 tokens = 100,000,000,000 raw units
    amounts = {100_000_000_000: quote(impact="0.01"), 500_000_000_000: quote(impact="0.06"),
               1_000_000_000_000: quote(impact="0.22")}
    j = Jupiter(Client(amounts))
    res = asyncio.run(j.sellability(MINT, 6, 0.001, (100.0, 500.0, 1000.0)))
    assert [c["usd"] for c in res["checked"]] == [100.0, 500.0, 1000.0]
    assert res["worst_impact_pct"] == 22.0 and res["max_sellable_usd"] == 1000.0
    assert res["no_route_at_usd"] is None and "Jupiter" in res["status"]
    assert j.client.calls[0]["outputMint"] == SOL


def test_sellability_stops_at_the_size_that_cannot_fill():
    j = Jupiter(Client({100_000_000_000: quote(impact="0.02")}))      # only $100 has a route
    res = asyncio.run(j.sellability(MINT, 6, 0.001, (100.0, 500.0, 1000.0)))
    assert res["max_sellable_usd"] == 100.0 and res["no_route_at_usd"] == 500.0
    assert len(res["checked"]) == 2 and res["checked"][-1]["no_route"]


def test_sellability_treats_a_huge_haircut_as_blocked():
    j = Jupiter(Client({100_000_000_000: quote(impact="0.02"), 500_000_000_000: quote(impact="0.80")}))
    res = asyncio.run(j.sellability(MINT, 6, 0.001, (100.0, 500.0)))
    assert res["no_route_at_usd"] == 500.0 and res["worst_impact_pct"] == 80.0


def test_sellability_needs_decimals_and_a_price():
    j = Jupiter(Client({}))
    assert asyncio.run(j.sellability(MINT, None, 0.001)) is None
    assert asyncio.run(j.sellability(MINT, 6, None)) is None
    assert asyncio.run(j.sellability(MINT, 6, 0)) is None


# ----------------------------------------------------------------------------- verdict wiring
def report(sell):
    return {"mint": MINT, "security": {"mint_authority": "REVOKED (VERIFIED)",
                                       "freeze_authority": "REVOKED (VERIFIED)", "lp_status": "BURNED — 100%"},
            "holders": {"top10_adj": 16.0, "max_single_adj": 3.0, "creator_pct": 0.0},
            "clusters": {"status": "INFERRED", "max_cluster_supply_pct": 0.0, "clusters": [],
                         "fresh_supply_pct": 1.0, "bubbles": [{}], "wallets_aged": 10},
            "trades": {"unique_buyers": 120}, "creator": {"prior_checked": 1, "prior_rugged": 0},
            "flags": [], "gates": [], "sellability": sell}


def test_cannot_sell_is_a_rug():
    v = assess(report({"checked": [{"usd": 100.0, "no_route": True, "impact_pct": None}],
                       "worst_impact_pct": None, "max_sellable_usd": None, "no_route_at_usd": 100.0}))
    assert v["level"] == "RUG LIKELY" and "cannot sell" in v["headline"]


def test_partial_exit_and_slippage_levels():
    v = assess(report({"checked": [{"usd": 100.0, "no_route": False, "impact_pct": 2.0},
                                   {"usd": 1000.0, "no_route": True, "impact_pct": None}],
                       "worst_impact_pct": 2.0, "max_sellable_usd": 100.0, "no_route_at_usd": 1000.0}))
    assert v["level"] == "HIGH RISK" and any(r["key"] == "sell_limit" for r in v["red"])
    v2 = assess(report({"checked": [{"usd": 1000.0, "no_route": False, "impact_pct": 24.0}],
                        "worst_impact_pct": 24.0, "max_sellable_usd": 1000.0, "no_route_at_usd": None}))
    assert any(r["key"] == "high_slippage" and r["level"] == "high" for r in v2["red"])
    v3 = assess(report({"checked": [{"usd": 1000.0, "no_route": False, "impact_pct": 7.0}],
                        "worst_impact_pct": 7.0, "max_sellable_usd": 1000.0, "no_route_at_usd": None}))
    assert any(r["key"] == "slippage" and r["level"] == "medium" for r in v3["red"])


def test_clean_exit_is_a_good_sign_and_missing_data_is_unknown():
    v = assess(report({"checked": [{"usd": 1000.0, "no_route": False, "impact_pct": 1.2}],
                       "worst_impact_pct": 1.2, "max_sellable_usd": 1000.0, "no_route_at_usd": None}))
    assert any("exit checked" in g for g in v["green"]) and v["level"] == "LOOKS CLEAN"
    r = report(None)
    r.pop("sellability")
    assert "exit cost not checked" in assess(r)["unknown"]


def test_brain_reads_the_exit_cost():
    from northstar import brain as B
    v = B.vector(report({"checked": [{"usd": 1000.0, "no_route": False, "impact_pct": 9.0}],
                         "worst_impact_pct": 9.0, "max_sellable_usd": 1000.0, "no_route_at_usd": None}))
    assert v["sell_impact"] == 9.0 and v["sell_blocked"] == 0.0
    blocked = B.vector(report({"checked": [], "worst_impact_pct": None, "max_sellable_usd": None,
                               "no_route_at_usd": 100.0}))
    assert blocked["sell_blocked"] == 1.0
