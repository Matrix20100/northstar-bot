"""SOL -> USD price for converting bonding-curve (SOL-denominated) values to dollars.

Sources, in order: the deepest SOL/USDC or SOL/USDT pool on DexScreener, then CoinGecko's free
simple-price endpoint. The value is cached for ``ttl`` seconds. If every source fails, the last
good price is reused for up to ``max_stale`` seconds; after that ``get()`` returns None and callers
show SOL instead of inventing a dollar figure.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from ..net import ProviderError
from .solana_rpc import USDC_MINT, USDT_MINT, WSOL_MINT

log = logging.getLogger("northstar.sol_price")

MIN_SANE, MAX_SANE = 1.0, 100_000.0
# v5.2.1: one bad SOL/USD number multiplies every bonding-curve price by the same factor. The deepest DexScreener
# pool is no longer trusted alone: the median of the deepest pools is used, CoinGecko is the cross-check, and a
# sudden move needs a second source (or a second read) that agrees before it replaces the last good price.
AGREE_PCT = 10.0                   # two sources within this % agree
JUMP_PCT = 30.0                    # a move bigger than this from the last good price needs confirming


class SolUsdPrice:
    def __init__(self, dex=None, gecko=None, ttl: float = 60, max_stale: float = 600):
        self.dex, self.gecko = dex, gecko
        self.ttl, self.max_stale = ttl, max_stale
        self._price: Optional[float] = None
        self._at = 0.0
        self.source: Optional[str] = None

    @staticmethod
    def _sane(value) -> Optional[float]:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        return v if MIN_SANE <= v <= MAX_SANE else None

    async def _from_dex(self) -> Optional[float]:
        if self.dex is None:
            return None
        pairs = await self.dex.tokens_batch([WSOL_MINT])
        stable = [p for p in pairs if p.base_mint == WSOL_MINT and p.quote_mint in (USDC_MINT, USDT_MINT)
                  and (p.liquidity_usd or 0) >= 100_000]
        if not stable:
            return None
        deepest = sorted(stable, key=lambda p: -(p.liquidity_usd or 0))[:3]
        prices = sorted(x for x in (self._sane(p.price_usd) for p in deepest) if x)
        return prices[len(prices) // 2] if prices else None

    async def _from_gecko(self) -> Optional[float]:
        if self.gecko is None:
            return None
        data = await self.gecko.request("GET", "/api/v3/simple/price",
                                        params={"ids": "solana", "vs_currencies": "usd"}, cache_ttl=30)
        return self._sane(((data or {}).get("solana") or {}).get("usd"))

    @staticmethod
    def _agree(a: float, b: float, pct: float) -> bool:
        return abs(a - b) / min(a, b) * 100 <= pct

    async def _read(self, name, fn) -> Optional[float]:
        try:
            return await fn()
        except (ProviderError, Exception) as exc:  # noqa: BLE001 - a price source must never crash research
            log.debug("SOL price from %s failed: %s", name, exc)
            return None

    async def get(self) -> Optional[float]:
        now = time.time()
        if self._price is not None and now - self._at < self.ttl:
            return self._price
        dex = await self._read("dexscreener", self._from_dex)
        gecko = await self._read("coingecko", self._from_gecko)
        price, source = None, None
        if dex and gecko:
            if self._agree(dex, gecko, AGREE_PCT):
                price, source = dex, "dexscreener+coingecko"
            else:
                # disagreement: the one closer to the last good price; with no last good price, neither (callers
                # then show SOL instead of guessing dollars)
                if self._price:
                    ref = self._price
                    price, source = (dex, "dexscreener") if abs(dex - ref) < abs(gecko - ref) else (gecko, "coingecko")
                log.warning("SOL price sources disagree: DexScreener $%.2f vs CoinGecko $%.2f; using %s", dex, gecko,
                            source or "neither (no earlier price to compare)")
        else:
            price, source = (dex, "dexscreener") if dex else (gecko, "coingecko") if gecko else (None, None)
            if price is not None and self._price and not self._agree(price, self._price, JUMP_PCT):
                pend = self.__dict__.get("_pending")
                if not (pend and self._agree(pend, price, AGREE_PCT)):
                    self._pending = price                   # one source, a big jump: wait for a second read
                    log.warning("SOL price jump from $%.2f to $%.2f (%s only) not used until confirmed",
                                self._price, price, source)
                    price = None
        if price is not None:
            self._pending = None
            if self._price and not self._agree(price, self._price, AGREE_PCT):
                log.info("SOL price $%.2f -> $%.2f (%s)", self._price, price, source)
            self._price, self._at, self.source = price, now, source
            return price
        if self._price is not None and now - self._at < self.max_stale:
            return self._price
        return None


def usd(sol_amount, sol_usd: Optional[float]) -> Optional[float]:
    """SOL amount -> USD, or None when either side is unknown."""
    if sol_usd is None or not isinstance(sol_amount, (int, float)):
        return None
    return float(sol_amount) * sol_usd
