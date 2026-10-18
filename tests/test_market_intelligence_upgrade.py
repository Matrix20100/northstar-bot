"""Synthetic unit tests for evidence-led chart-path analysis (no live API calls)."""
from __future__ import annotations

from conftest import make_pair
from northstar.analysis.market import analyze_market


def test_market_path_measures_return_drawdown_and_consolidation(settings):
    pair = make_pair(price_usd=1.056, price_change={"m5": 1, "h1": 3, "h6": 20, "h24": 25},
                     vol={"m5": 1000, "h1": 6000, "h6": 30000, "h24": 60000})
    history = [
        {"pair_address": pair.pair_address, "ts": 100, "price_usd": 0.96, "liquidity_usd": 100000, "market_cap": 500000},
        {"pair_address": pair.pair_address, "ts": 200, "price_usd": 1.03, "liquidity_usd": 110000, "market_cap": 550000},
        {"pair_address": pair.pair_address, "ts": 300, "price_usd": 0.98, "liquidity_usd": 115000, "market_cap": 560000},
        {"pair_address": pair.pair_address, "ts": 400, "price_usd": 1.03, "liquidity_usd": 118000, "market_cap": 570000},
    ]
    result = analyze_market(pair, history, settings)
    assert result.snapshots_used == 4
    assert result.observed_return_pct == 10.0
    # peak 1.03 -> trough 0.98 is the deepest observed drawdown
    assert result.observed_drawdown_pct == 4.85
    assert result.consolidating is True
    assert result.buy_sell_ratio_h1 is not None
    assert result.to_dict()["entry_quality"] in {"POTENTIALLY_CONSTRUCTIVE", "WATCH_PULLBACK", "UNCONFIRMED"}


def test_market_rejects_extended_fading_setup(settings):
    pair = make_pair(price_change={"m5": -8, "h1": -25, "h6": 180, "h24": 250},
                     liquidity_usd=30000, txns={"m5": {"buys": 2, "sells": 12},
                     "h1": {"buys": 20, "sells": 80}, "h6": {"buys": 300, "sells": 450},
                     "h24": {"buys": 800, "sells": 1000}})
    result = analyze_market(pair, [], settings)
    assert result.fading is True
    assert result.entry_quality == "POOR"
    assert any(flag.code == "FADING_AFTER_PUMP" for flag in result.flags)


def test_market_does_not_confuse_other_pool_history(settings):
    pair = make_pair()
    history = [{"pair_address": "wrong-pool", "ts": 100, "price_usd": 0.00001} for _ in range(10)]
    result = analyze_market(pair, history, settings)
    assert result.snapshots_used == 0
    assert result.observed_return_pct is None
    assert result.entry_quality == "INSUFFICIENT_HISTORY"
