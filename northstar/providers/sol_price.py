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
        best = max(stable, key=lambda p: p.liquidity_usd or 0)
        return self._sane(best.price_usd)

    async def _from_gecko(self) -> Optional[float]:
        if self.gecko is None:
            return None
        data = await self.gecko.request("GET", "/api/v3/simple/price",
                                        params={"ids": "solana", "vs_currencies": "usd"}, cache_ttl=30)
        return self._sane(((data or {}).get("solana") or {}).get("usd"))

    async def get(self) -> Optional[float]:
        now = time.time()
        if self._price is not None and now - self._at < self.ttl:
            return self._price
        for name, fn in (("dexscreener", self._from_dex), ("coingecko", self._from_gecko)):
            try:
                price = await fn()
            except (ProviderError, Exception) as exc:  # noqa: BLE001 - a price source must never crash research
                log.debug("SOL price from %s failed: %s", name, exc)
                continue
            if price is not None:
                self._price, self._at, self.source = price, now, name
                return price
        if self._price is not None and now - self._at < self.max_stale:
            return self._price
        return None


def usd(sol_amount, sol_usd: Optional[float]) -> Optional[float]:
    """SOL amount -> USD, or None when either side is unknown."""
    if sol_usd is None or not isinstance(sol_amount, (int, float)):
        return None
    return float(sol_amount) * sol_usd
