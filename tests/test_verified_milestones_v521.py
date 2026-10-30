"""v5.2.1: "bot lied again". $UP (EQ7g...pump), called 06:17 UTC at $16.8K, real high $44.7K (Axiom ATH), was posted
in the free channel as "hit 40x" and "hit 50x" at $975.4K. The inflation was about the same factor as $Pumpers
and $Pilly (about 22-58x), which a wrong SOL/USD price would cause, and the v5.1.3 Jupiter check priced in SOL
with that same SOL price. Now: every milestone is checked against a real buy quote in USDC, the SOL price needs
sources that agree, and a curve cannot read more than a pump.fun curve holds."""
from __future__ import annotations

import asyncio
import json
import types

from northstar import early_calls as EC
from northstar import price_guard as PG
from northstar.providers.sol_price import SolUsdPrice
from northstar.providers.solana_rpc import USDC_MINT, WSOL_MINT

import test_early_calls as TE
from test_price_guard_v513 import Jup, Rpc


def test_the_milestone_check_is_in_dollars_not_through_the_sol_price():
    j = Jup(40_000.0)
    g = PG.PriceGuard(Rpc(0), j, sol_price=None)                    # no SOL price needed at all
    ok, real = asyncio.run(g.confirm_milestone(TE.MINT, 975_400.0, "UP"))
    assert not ok and real == 40_000.0
    ok, real = asyncio.run(g.confirm_milestone(TE.MINT, 45_000.0, "UP"))
    assert ok


def test_a_milestone_that_cannot_be_checked_waits_and_one_nothing_can_buy_is_dropped():
    assert asyncio.run(PG.PriceGuard(Rpc(0), Jup(None)).confirm_milestone(TE.MINT, 975_400.0)) == (False, None)
    assert asyncio.run(PG.PriceGuard(Rpc(0), Jup("NO_ROUTE")).confirm_milestone(TE.MINT, 975_400.0)) == (False, None)


def _tracker(db, monkeypatch, cap_sol, jup):
    from northstar.research import Researcher
    monkeypatch.setattr(Researcher, "_parse_pump_curve_account",
                        staticmethod(lambda acc: {"market_cap_sol": cap_sol, "complete": False}))
    s = TE.Settings(_env_file=None, runner_calls_enabled=False, discord_channel_id=111, discord_free_channel_id=222,
                    embed_edit_min_seconds=0)

    class Dex:
        async def tokens_batch(self, mints):
            return []
    sol = types.SimpleNamespace(get=lambda: _async(4_400.0))      # a WRONG SOL price (should be about $190)
    return EC.EarlyCallTracker(s, db, None, Rpc(0), Dex(), sol,
                               guard=PG.PriceGuard(Rpc(0), Jup(jup), sol))


async def _async(v):
    return v


def test_up_a_wrong_sol_price_cannot_post_50x(db, monkeypatch):
    s, pub, c = TE.caller(db)
    asyncio.run(c.consider(TE.outcome(TE.curve_report())))          # called at $40K
    db.execute("UPDATE early_calls SET created_at=? WHERE mint=?", (TE.NOW, TE.MINT))
    n0 = len(pub.sent)
    tr = _tracker(db, monkeypatch, cap_sol=220.0, jup=45_000.0)     # 220 SOL x $4,400 = $968K: the "58x"
    tr.pub = pub
    asyncio.run(tr.run_once(TE.NOW + 60))
    r = db.fetchone("SELECT * FROM early_calls WHERE mint=?", (TE.MINT,))
    assert json.loads(r["milestones"]) == [] and r["peak_mcap"] < 60_000 and r["current_mcap"] == 45_000.0
    assert not [e for _ch, e in pub.sent[n0:] if "since the call" in json.dumps(e)]


def test_a_real_run_still_posts(db, monkeypatch):
    s, pub, c = TE.caller(db)
    asyncio.run(c.consider(TE.outcome(TE.curve_report())))
    db.execute("UPDATE early_calls SET created_at=? WHERE mint=?", (TE.NOW, TE.MINT))
    tr = _tracker(db, monkeypatch, cap_sol=30.0, jup=125_000.0)      # 30 SOL x 4,400 = $132K, Jupiter agrees
    tr.pub = pub
    asyncio.run(tr.run_once(TE.NOW + 60))
    assert 3.0 in json.loads(db.fetchone("SELECT milestones FROM early_calls WHERE mint=?", (TE.MINT,))["milestones"])


def test_a_curve_reading_over_what_a_curve_holds_is_not_used(db, monkeypatch):
    s, pub, c = TE.caller(db)
    asyncio.run(c.consider(TE.outcome(TE.curve_report())))
    tr = _tracker(db, monkeypatch, cap_sol=5_000.0, jup=45_000.0)
    tr.pub = pub
    asyncio.run(tr.run_once(TE.NOW + 60))
    assert db.fetchone("SELECT peak_mcap FROM early_calls WHERE mint=?", (TE.MINT,))["peak_mcap"] < 60_000


# ----------------------------------------------------------------------------- the SOL price
def _pair(price, liq):
    return types.SimpleNamespace(base_mint=WSOL_MINT, quote_mint=USDC_MINT, price_usd=price, liquidity_usd=liq)


class Dex:
    def __init__(self, pairs):
        self.pairs = pairs

    async def tokens_batch(self, mints):
        return self.pairs


class Gecko:
    def __init__(self, price):
        self.price = price

    async def request(self, *a, **k):
        if self.price is None:
            raise RuntimeError("down")
        return {"solana": {"usd": self.price}}


def test_one_bad_deep_pool_does_not_set_the_sol_price():
    p = SolUsdPrice(Dex([_pair(4_400.0, 90_000_000), _pair(190.0, 40_000_000), _pair(191.0, 30_000_000)]), Gecko(None))
    assert asyncio.run(p.get()) == 191.0                           # the median of the deepest pools


def test_a_sol_price_jump_needs_a_second_source():
    p = SolUsdPrice(Dex([_pair(190.0, 50_000_000)]), Gecko(190.5), ttl=0)
    assert asyncio.run(p.get()) == 190.0
    p.dex = Dex([_pair(4_400.0, 50_000_000)])
    assert asyncio.run(p.get()) == 190.5                           # CoinGecko disagrees: the one near $190 wins
    p.gecko = Gecko(None)
    assert asyncio.run(p.get()) == 190.5                           # DexScreener alone: a 23x jump is not used


def test_sources_that_disagree_with_no_history_give_no_price():
    assert asyncio.run(SolUsdPrice(Dex([_pair(4_400.0, 50_000_000)]), Gecko(190.0)).get()) is None


# ----------------------------------------------------------------------------- the recap checks peaks on the chart
class Candles:
    """GeckoTerminal-style 1-minute candles: the call at $16.8K, a real high of $44.7K (UP's Axiom ATH)."""
    def __init__(self, rows):
        self.rows, self.pools = rows, []

    async def candles(self, pool, start, end=None, urgent=False, timeframe="minute", token=None):
        self.pools.append(pool)
        return self.rows


def test_the_recap_corrects_a_peak_the_chart_does_not_show(db):
    from northstar import recap as R
    from test_daily_recap_v520 import DAY_20H, runner_call
    at = DAY_20H - 5 * 3600
    runner_call(db, "UP", 16_800, 975_400, at)                       # the false 58x
    db.execute("UPDATE early_calls SET mint = ?", ("EQ7gCEMvQxC48PYfuPWwXVXxYuvGabpPWcCT1okvpump",))   # $UP itself
    calls = R.calls_between(db, DAY_20H - R.DAY, DAY_20H)
    ohlc = [(at, 0.0000168, 0.0000170, 0.0000165, 0.0000169, 100.0),
            (at + 60, 0.0000169, 0.0000447, 0.0000168, 0.0000400, 900.0),
            (at + 120, 0.0000400, 0.0000410, 0.0000027, 0.0000028, 900.0)]
    cg = Candles(ohlc)
    asyncio.run(R.verify_peaks(db, calls, cg))
    assert round(calls[0]["x"], 1) == 2.7 and not calls[0].get("unchecked")
    assert cg.pools and db.fetchone("SELECT peak_mcap FROM early_calls")["peak_mcap"] < 50_000
    desc = R.build(calls, "30 Oct 2026")[0]["description"]
    assert "58" not in desc and "**$UP** — **2.66x**" in desc


def test_a_peak_the_chart_cannot_check_is_marked(db):
    from northstar import recap as R
    from test_daily_recap_v520 import DAY_20H, runner_call
    runner_call(db, "BER", 16_500, 127_100, DAY_20H - 3 * 3600)
    calls = R.calls_between(db, DAY_20H - R.DAY, DAY_20H)
    asyncio.run(R.verify_peaks(db, calls, Candles([])))
    desc = R.build(calls, "30 Oct 2026")[0]["description"]
    assert "7.70x" not in desc and "$BER" not in desc                      # v5.5.1: unconfirmed 2x+ is not shown
    assert "1 more call not shown: the peak could not be checked against chart data." in desc
