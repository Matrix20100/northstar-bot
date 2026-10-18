"""DexScreener public API (no key).

Verified endpoints (docs.dexscreener.com/api/reference, checked 2026-10-15):
  GET /token-profiles/latest/v1          -> list[TokenProfile]          (60 rpm)
  GET /token-boosts/latest/v1            -> list[{url,chainId,tokenAddress,amount,totalAmount}] (60 rpm)
  GET /token-boosts/top/v1               -> same shape                  (60 rpm)
  GET /community-takeovers/latest/v1     -> list[TokenProfile + claimDate] (60 rpm)
  GET /metas/trending/v1                 -> list[Meta]                  (60 rpm)
  GET /token-pairs/v1/{chain}/{mint}     -> list[Pair]
  GET /tokens/v1/{chain}/{mints,<=30}    -> list[Pair]
  GET /latest/dex/search?q=              -> {"pairs": list[Pair]}

Known limits of this source: volume windows are m5/h1/h6/h24 only (no 15m), txns are counts
(not unique wallets), and there is no free OHLC candle endpoint.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from ..models import PairInfo
from ..net import NotFound, ProviderClient

log = logging.getLogger("northstar.dexscreener")
CHAIN = "solana"


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return x


def _i(v: Any) -> Optional[int]:
    x = _f(v)
    return int(x) if x is not None else None


def parse_pair(p: dict) -> Optional[PairInfo]:
    """Validate and normalize one pair. Malformed rows return None instead of raising."""
    try:
        if not isinstance(p, dict) or p.get("chainId") != CHAIN:
            return None
        base = p.get("baseToken") or {}
        quote = p.get("quoteToken") or {}
        if not base.get("address") or not p.get("pairAddress"):
            return None
        txns_raw = p.get("txns") or {}
        txns = {k: {"buys": _i((txns_raw.get(k) or {}).get("buys")),
                    "sells": _i((txns_raw.get(k) or {}).get("sells"))} for k in ("m5", "h1", "h6", "h24")}
        vol_raw = p.get("volume") or {}
        pc_raw = p.get("priceChange") or {}
        liq = p.get("liquidity") or {}
        info = p.get("info") or {}
        created = _f(p.get("pairCreatedAt"))
        return PairInfo(
            pair_address=str(p["pairAddress"]),
            dex_id=str(p.get("dexId") or ""),
            base_mint=str(base["address"]),
            base_symbol=str(base.get("symbol") or "")[:32],
            base_name=str(base.get("name") or "")[:80],
            quote_mint=str(quote.get("address") or ""),
            quote_symbol=str(quote.get("symbol") or ""),
            price_usd=_f(p.get("priceUsd")),
            price_native=_f(p.get("priceNative")),
            liquidity_usd=_f(liq.get("usd")),
            fdv=_f(p.get("fdv")),
            market_cap=_f(p.get("marketCap")),
            pair_created_at=(created / 1000.0) if created else None,
            vol={k: _f(vol_raw.get(k)) for k in ("m5", "h1", "h6", "h24")},
            txns=txns,
            price_change={k: _f(pc_raw.get(k)) for k in ("m5", "h1", "h6", "h24")},
            url=str(p.get("url") or ""),
            labels=[str(x) for x in (p.get("labels") or [])],
            image_url=info.get("imageUrl"),
            websites=[w.get("url") for w in (info.get("websites") or []) if isinstance(w, dict) and w.get("url")],
            socials=[s for s in (info.get("socials") or []) if isinstance(s, dict)],
            boosts_active=_i((p.get("boosts") or {}).get("active")) or 0,
            fetched_at=time.time(),
        )
    except Exception:  # never let one malformed row crash a scan
        log.debug("malformed pair skipped", exc_info=True)
        return None


class DexScreener:
    def __init__(self, slow: ProviderClient, fast: ProviderClient):
        self.slow = slow  # 60 rpm endpoints
        self.fast = fast  # pair/token endpoints

    async def latest_profiles(self) -> list[dict]:
        data = await self.slow.request("GET", "/token-profiles/latest/v1", cache_ttl=30)
        return [d for d in _as_list(data) if d.get("chainId") == CHAIN and d.get("tokenAddress")]

    async def latest_boosts(self) -> list[dict]:
        data = await self.slow.request("GET", "/token-boosts/latest/v1", cache_ttl=30)
        return [d for d in _as_list(data) if d.get("chainId") == CHAIN and d.get("tokenAddress")]

    async def top_boosts(self) -> list[dict]:
        data = await self.slow.request("GET", "/token-boosts/top/v1", cache_ttl=60)
        return [d for d in _as_list(data) if d.get("chainId") == CHAIN and d.get("tokenAddress")]

    async def community_takeovers(self) -> list[dict]:
        data = await self.slow.request("GET", "/community-takeovers/latest/v1", cache_ttl=60)
        return [d for d in _as_list(data) if d.get("chainId") == CHAIN and d.get("tokenAddress")]

    async def trending_metas(self) -> list[dict]:
        data = await self.slow.request("GET", "/metas/trending/v1", cache_ttl=300)
        return [m for m in _as_list(data) if isinstance(m, dict) and m.get("slug")]

    async def token_pairs(self, mint: str) -> list[PairInfo]:
        try:
            data = await self.fast.request("GET", f"/token-pairs/v1/{CHAIN}/{mint}", cache_ttl=20)
        except NotFound:
            return []
        return [p for p in (parse_pair(x) for x in _as_list(data)) if p]

    async def tokens_batch(self, mints: list[str]) -> list[PairInfo]:
        out: list[PairInfo] = []
        for i in range(0, len(mints), 30):
            chunk = ",".join(mints[i:i + 30])
            try:
                data = await self.fast.request("GET", f"/tokens/v1/{CHAIN}/{chunk}", cache_ttl=20)
            except NotFound:
                continue
            out.extend(p for p in (parse_pair(x) for x in _as_list(data)) if p)
        return out

    async def search(self, query: str) -> list[PairInfo]:
        data = await self.fast.request("GET", "/latest/dex/search", params={"q": query}, cache_ttl=300)
        pairs = data.get("pairs") if isinstance(data, dict) else None
        return [p for p in (parse_pair(x) for x in (pairs or [])) if p]


def _as_list(data: Any) -> list:
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):  # tolerate {"pairs": [...]} or a single object
        if isinstance(data.get("pairs"), list):
            return [d for d in data["pairs"] if isinstance(d, dict)]
        return [data]
    return []
