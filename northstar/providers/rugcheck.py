"""RugCheck public API (no key for the endpoints used).

Verified 2026-10-15:
  GET /v1/tokens/{mint}/report/summary -> {tokenProgram, tokenType, risks[{name,value,description,
                                           score,level}], score, score_normalised, lpLockedPct}
  GET /v1/tokens/{mint}/report         -> full report incl. creator, creatorBalance, token{...},
                                           topHolders[{address,amount,decimals,pct,uiAmount,owner,insider}],
                                           markets[{pubkey,marketType,mintA,mintB,mintLP,liquidityA,liquidityB,
                                                    lp{...lpLockedPct,lpLockedUSD,...}}],
                                           lockers{}, transferFee{pct,maxAmount,authority}, rugged,
                                           graphInsidersDetected, insiderNetworks, knownAccounts{addr:{name,type}},
                                           totalHolders, totalMarketLiquidity, price, creatorTokens, ...

Everything from RugCheck is treated as OBSERVED (third-party analysis, not independently verified).
Several fields are frequently null for young tokens; callers must treat null as UNKNOWN.
"""
from __future__ import annotations

from typing import Any, Optional

from ..net import ProviderClient


class RugCheck:
    def __init__(self, client: ProviderClient):
        self.client = client

    async def report(self, mint: str) -> dict:
        data = await self.client.request("GET", f"/v1/tokens/{mint}/report", cache_ttl=120)
        return data if isinstance(data, dict) else {}

    async def summary(self, mint: str) -> dict:
        data = await self.client.request("GET", f"/v1/tokens/{mint}/report/summary", cache_ttl=120)
        return data if isinstance(data, dict) else {}


def rc_float(d: Any, *keys: str) -> Optional[float]:
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    try:
        return float(cur) if cur is not None else None
    except (TypeError, ValueError):
        return None
