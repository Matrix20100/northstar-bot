"""SOL/USD source selection and fallbacks (synthetic)."""
import asyncio
import types

from northstar.net import ProviderError
from northstar.providers.sol_price import SolUsdPrice, usd
from northstar.providers.solana_rpc import USDC_MINT, WSOL_MINT


def pair(price, liq, quote=USDC_MINT, base=WSOL_MINT):
    return types.SimpleNamespace(base_mint=base, quote_mint=quote, price_usd=price, liquidity_usd=liq)


class Dex:
    def __init__(self, pairs=None, fail=False):
        self.pairs, self.fail, self.calls = pairs or [], fail, 0

    async def tokens_batch(self, mints):
        self.calls += 1
        if self.fail:
            raise ProviderError("dexscreener", "down")
        return self.pairs


class Gecko:
    def __init__(self, price=None, fail=False):
        self.price, self.fail = price, fail

    async def request(self, *a, **k):
        if self.fail:
            raise ProviderError("coingecko", "down")
        return {"solana": {"usd": self.price}}


def test_deepest_stable_pool_wins_and_is_cached():
    dex = Dex([pair(150.0, 5_000_000), pair(149.0, 200_000), pair(999.0, 50)])
    p = SolUsdPrice(dex, Gecko(1.0))
    assert asyncio.run(p.get()) == 150.0 and p.source == "dexscreener"
    asyncio.run(p.get())
    assert dex.calls == 1


def test_falls_back_to_coingecko_then_last_good_then_none():
    p = SolUsdPrice(Dex(fail=True), Gecko(151.5), ttl=0, max_stale=600)
    assert asyncio.run(p.get()) == 151.5 and p.source == "coingecko"
    p.gecko = Gecko(fail=True)
    assert asyncio.run(p.get()) == 151.5            # last good value, still fresh enough
    p._at -= 10_000
    assert asyncio.run(p.get()) is None             # too old: show SOL instead of guessing


def test_insane_prices_rejected():
    p = SolUsdPrice(Dex([pair(0.0, 5_000_000)]), Gecko(10**9))
    assert asyncio.run(p.get()) is None


def test_usd_helper():
    assert usd(2.0, 150.0) == 300.0 and usd(None, 150.0) is None and usd(2.0, None) is None
