"""v5.5.1: the recap lists only calls that reached 1.4x, and a migrated runner's peak is chart-checked.
Operator: "We dont show the coins that didnt go above a 1x peak, 1.4x's show". The 30 Oct recap showed $UP at a false
58.16x* because a migrated runner was never chart-checked."""
from __future__ import annotations

import asyncio

from conftest import addr
from northstar import recap as R
from test_daily_recap_v520 import DAY_20H, S, runner_call

UP = "EQ7gCEMvQxC48PYfuPWwXVXxYuvGabpPWcCT1okvpump"


def call(sym, x, unchecked=False):
    return {"mint": addr(sym), "symbol": sym, "entry": 20_000, "peak": 20_000 * x, "x": x, "at": 0,
            "unchecked": unchecked}


def test_only_calls_at_1_4x_or_more_are_listed():
    calls = [call("RUN", 3.0), call("OKAY", 1.46), call("EDGE", 1.40), call("MEH", 1.39), call("FLAT", 1.0)]
    desc = R.build(calls, "30 Oct 2026")[0]["description"]
    assert "**5 calls in the last 24 hours** · 3 went 1.4x or more · 1 reached 2x or more · best **$RUN 3.00x**" in desc
    for sym in ("RUN", "OKAY", "EDGE"):
        assert f"**${sym}**" in desc
    for sym in ("MEH", "FLAT"):
        assert f"${sym}" not in desc
    assert "✅ **$EDGE** — **1.40x**" in desc


def test_the_cutoff_is_a_setting_and_a_quiet_day_says_so():
    calls = [call("A", 1.6), call("B", 1.2)]
    assert "$A" not in R.build(calls, "d", min_x=2.0)[0]["description"]
    assert "none went 2x or more" in R.build(calls, "d", min_x=2.0)[0]["description"]
    assert S().daily_recap_min_multiple == 1.4


class Gecko:
    """The curve shows the call at $16.8K; after migration the PumpSwap pool shows the real high of $44.7K."""
    def __init__(self, at):
        self.at, self.asked = at, []

    async def token_pools(self, mint, limit=3, urgent=False):
        return ["PUMPSWAPPOOL"]

    async def candles(self, pool, start, end=None, urgent=False, timeframe="minute", token=None, aggregate=1):
        self.asked.append((pool, aggregate))
        at = self.at
        if pool == "PUMPSWAPPOOL":
            return [(at + 600, 0.0000300, 0.0000447, 0.0000290, 0.0000400, 900.0)]
        return [(at, 0.0000168, 0.0000170, 0.0000165, 0.0000169, 100.0),
                (at + 60, 0.0000169, 0.0000300, 0.0000168, 0.0000290, 500.0)]


def test_a_migrated_runner_is_checked_on_its_pool(db):
    at = DAY_20H - 20 * 3600                                     # 20 h before the recap: read in 5-minute candles
    runner_call(db, "UP", 16_800, 975_400, at)
    db.execute("UPDATE early_calls SET mint = ?, phase = 'POOL'", (UP,))
    calls = R.calls_between(db, DAY_20H - R.DAY, DAY_20H)
    cg = Gecko(at)
    asyncio.run(R.verify_peaks(db, calls, cg, now=DAY_20H))
    assert "PUMPSWAPPOOL" in [p for p, _ in cg.asked] and all(a == 5 for _, a in cg.asked)
    assert round(calls[0]["x"], 2) == 2.66 and calls[0].get("checked")
    assert db.fetchone("SELECT peak_mcap FROM early_calls")["peak_mcap"] < 50_000
    desc = R.build(calls, "30 Oct 2026")[0]["description"]
    assert "58" not in desc and "**$UP** — **2.66x**" in desc
