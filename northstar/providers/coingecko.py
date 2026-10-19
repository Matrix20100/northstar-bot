"""CoinGecko on-chain data (the same data GeckoTerminal shows).

Used for:
  * 1-minute candles: the true high a coin reached after a call (30-second polling misses wicks),
    and the full price path the self-learning loop scores every past decision against;
  * trending Solana pools: an attention feed (volume, trades and views) the surge radar watches.

Two clients share one interface:
  * KEYED: your CoinGecko Demo or Pro key (higher limits; kept for time-critical calls);
  * KEYLESS: the public GeckoTerminal API (no key, ~10 calls/min) for background work, so the
    Demo plan's monthly credits are not burned by the learning loop.
Every method returns None/[] on failure; callers treat that as UNAVAILABLE.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from ..net import ProviderError

log = logging.getLogger("northstar.coingecko")
NETWORK = "solana"


def _strip(prefixed: Optional[str]) -> Optional[str]:
    if not prefixed:
        return None
    return prefixed.split("_", 1)[1] if prefixed.startswith(NETWORK + "_") else prefixed


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class CoinGeckoOnchain:
    def __init__(self, keyed=None, keyless=None, keyed_prefix: str = "/onchain", keyless_prefix: str = ""):
        self.keyed, self.keyless = keyed, keyless
        self.keyed_prefix, self.keyless_prefix = keyed_prefix, keyless_prefix

    def _order(self, urgent: bool):
        a = (self.keyed, self.keyed_prefix)
        b = (self.keyless, self.keyless_prefix)
        return [c for c in ((a, b) if urgent else (b, a)) if c[0] is not None]

    async def _get(self, path: str, params: dict, urgent: bool, cache_ttl: float = 0):
        last = None
        for client, prefix in self._order(urgent):
            try:
                return await client.request("GET", prefix + path, params=params, cache_ttl=cache_ttl)
            except ProviderError as exc:
                last = exc
                continue
        if last:
            log.debug("coingecko %s failed: %s", path, last)
        return None

    async def candles(self, pool: str, start: float, end: Optional[float] = None, urgent: bool = False,
                      timeframe: str = "minute", token: Optional[str] = None
                      ) -> list[tuple[float, float, float, float, float, float]]:
        """1-minute OHLCV (ts, open, high, low, close, volume_usd) for start..end, oldest first.

        ``token`` must be the coin's mint: a pool's "base" side is the pool's own token order, which on
        some DEXes is SOL, and candles for SOL would look like a 1,000,000x move.
        """
        end = end or time.time()
        span_min = max(1, int((end - start) / 60) + 2)
        data = await self._get(f"/networks/{NETWORK}/pools/{pool}/ohlcv/{timeframe}",
                               {"aggregate": 1, "before_timestamp": int(end) + 60, "limit": min(1000, span_min),
                                "currency": "usd", "token": token or "base"}, urgent, cache_ttl=30)
        rows = (((data or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        out = []
        for r in rows:
            if not isinstance(r, (list, tuple)) or len(r) < 6:
                continue
            vals = [_f(x) for x in r[:6]]
            if None in vals[:5] or vals[0] < start - 60 or vals[0] > end:
                continue
            out.append(tuple(vals))  # type: ignore[arg-type]
        return sorted(out)

    async def trending_pools(self, duration: str = "5m", urgent: bool = False) -> list[dict]:
        """Trending Solana pools: [{mint, pool, name, market_cap, fdv, liquidity, vol_m5, vol_h1, ...}]."""
        data = await self._get(f"/networks/{NETWORK}/trending_pools",
                               {"include": "base_token", "duration": duration, "page": 1}, urgent, cache_ttl=60)
        out = []
        for item in (data or {}).get("data") or []:
            a = item.get("attributes") or {}
            rel = ((item.get("relationships") or {}).get("base_token") or {}).get("data") or {}
            mint = _strip(rel.get("id"))
            if not mint or not a.get("address"):
                continue
            vol = a.get("volume_usd") or {}
            tx5 = (a.get("transactions") or {}).get("m5") or {}
            pc = a.get("price_change_percentage") or {}
            out.append({"mint": mint, "pool": a["address"], "name": a.get("name"),
                        "market_cap": _f(a.get("market_cap_usd")), "fdv": _f(a.get("fdv_usd")),
                        "liquidity": _f(a.get("reserve_in_usd")), "vol_m5": _f(vol.get("m5")),
                        "vol_h1": _f(vol.get("h1")), "buys_m5": tx5.get("buys"), "sells_m5": tx5.get("sells"),
                        "buyers_m5": tx5.get("buyers"), "change_m5": _f(pc.get("m5")), "change_h1": _f(pc.get("h1"))})
        return out


def peak_after(candles, entry_value: float, since: float) -> Optional[tuple[float, float]]:
    """(highest multiple of ``entry_value``, its unix time) from candles at or after ``since``."""
    best = None
    for ts, _o, high, _l, _c, _v in candles:
        if ts < since or not entry_value:       # only whole candles after the call (no pre-call highs)
            continue
        mult = high / entry_value
        if best is None or mult > best[0]:
            best = (mult, ts)
    return best


class CoinGeckoMarkets:
    """CoinGecko's main API: which crypto NARRATIVES (categories) and coins are hot right now.

    Free with a Demo key (the same one the on-chain client uses) and keyless at a lower rate.
      /search/trending   coins and categories people are searching for on CoinGecko
      /coins/categories  every sector with its 24h market-cap change ("AI agents", "memecoins"...)
    This is crypto-sector attention, which complements (never replaces) the real-world event feeds.
    """

    def __init__(self, client=None, prefix: str = ""):
        self.client, self.prefix = client, prefix

    async def _get(self, path: str, params: Optional[dict] = None, cache_ttl: float = 300):
        if self.client is None:
            return None
        try:
            return await self.client.request("GET", self.prefix + path, params=params or {}, cache_ttl=cache_ttl)
        except ProviderError as exc:
            log.info("coingecko %s failed: %s", path, exc)
            return None

    async def trending(self) -> dict:
        """{'coins': [{name, symbol, rank, market_cap_change_24h}], 'categories': [{name, change_24h}]}"""
        data = await self._get("/search/trending")
        if not isinstance(data, dict):
            return {"coins": [], "categories": []}
        coins = []
        for item in (data.get("coins") or [])[:15]:
            c = (item or {}).get("item") or {}
            pct = ((c.get("data") or {}).get("price_change_percentage_24h") or {}).get("usd")
            coins.append({"name": c.get("name"), "symbol": (c.get("symbol") or "").upper(),
                          "rank": c.get("score"), "change_24h": _f(pct)})
        cats = [{"name": (c or {}).get("name"), "change_24h": _f((c or {}).get("market_cap_1h_change"))}
                for c in (data.get("categories") or [])[:10] if (c or {}).get("name")]
        return {"coins": coins, "categories": cats}

    async def hot_categories(self, limit: int = 12) -> list[dict]:
        """Sectors ranked by 24h market-cap change: the crypto meta that is moving today."""
        data = await self._get("/coins/categories", {"order": "market_cap_change_24h_desc"}, cache_ttl=900)
        if not isinstance(data, list):
            return []
        out = []
        for c in data[:limit]:
            change = _f(c.get("market_cap_change_24h"))
            if change is None:
                continue
            out.append({"name": c.get("name"), "change_24h": round(change, 1),
                        "volume_24h": _f(c.get("volume_24h")), "top": [t for t in (c.get("top_3_coins_id") or [])][:3]})
        return out
