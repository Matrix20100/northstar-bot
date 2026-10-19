"""Jupiter: can you actually SELL this coin, and at what cost?

Free and keyless (lite-api.jup.ag). Jupiter routes across every Solana DEX, so a quote answers the
question every other data source leaves open: a chart can show 3x while the pool is too thin to
exit. For a sell size in dollars it returns the price impact, i.e. how much worse than the quoted
price you would actually get.

  GET https://lite-api.jup.ag/swap/v1/quote
      ?inputMint=<the coin>&outputMint=<SOL>&amount=<raw units>&slippageBps=...
  -> {"outAmount": "...", "priceImpactPct": "0.0123", "routePlan": [...]}

NO ROUTE is the loudest signal of all: nothing can sell the coin at that size right now.
Every method returns None on failure; callers treat that as UNAVAILABLE, never as "safe".
"""
from __future__ import annotations

import logging
from typing import Optional

from ..net import ProviderError

log = logging.getLogger("northstar.jupiter")

SOL = "So11111111111111111111111111111111111111112"


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class Jupiter:
    def __init__(self, client):
        self.client = client

    async def quote(self, mint: str, amount_raw: int, out_mint: str = SOL,
                    slippage_bps: int = 500) -> Optional[dict]:
        """One sell quote. None when Jupiter errors; {'no_route': True} when nothing can fill it."""
        if amount_raw <= 0:
            return None
        try:
            data = await self.client.request(
                "GET", "/swap/v1/quote",
                params={"inputMint": mint, "outputMint": out_mint, "amount": int(amount_raw),
                        "slippageBps": int(slippage_bps), "restrictIntermediateTokens": "true"},
                cache_ttl=20)
        except ProviderError as exc:
            if getattr(exc, "status", None) in (400, 404, 422):
                return {"no_route": True}          # Jupiter answers 4xx when no route exists
            log.info("jupiter quote failed for %s: %s", mint, exc)
            return None
        if not isinstance(data, dict) or not data.get("outAmount"):
            return {"no_route": True}
        impact = _f(data.get("priceImpactPct"))
        return {"no_route": False, "out_amount": _f(data.get("outAmount")),
                "price_impact_pct": round(impact * 100, 3) if impact is not None else None,
                "hops": len(data.get("routePlan") or [])}

    async def sellability(self, mint: str, decimals: Optional[int], price_usd: Optional[float],
                          sizes_usd=(100.0, 500.0, 1000.0)) -> Optional[dict]:
        """How much a sell of each dollar size would cost you in price impact.

        {'checked': [{'usd', 'impact_pct', 'no_route'}], 'worst_impact_pct', 'max_sellable_usd',
         'no_route_at_usd', 'status'}
        """
        if decimals is None or not price_usd or price_usd <= 0:
            return None
        checked, max_ok, blocked = [], None, None
        for usd in sorted(sizes_usd):
            raw = int(usd / price_usd * (10 ** int(decimals)))
            q = await self.quote(mint, raw)
            if q is None:
                break                                   # provider problem: report what we have
            row = {"usd": usd, "no_route": bool(q.get("no_route")), "impact_pct": q.get("price_impact_pct")}
            checked.append(row)
            if row["no_route"]:
                blocked = usd
                break
            if row["impact_pct"] is not None and row["impact_pct"] >= 50:
                blocked = usd                           # a 50%+ haircut is not an exit
                break
            max_ok = usd
        if not checked:
            return None
        impacts = [r["impact_pct"] for r in checked if r["impact_pct"] is not None]
        return {"checked": checked, "worst_impact_pct": max(impacts) if impacts else None,
                "max_sellable_usd": max_ok, "no_route_at_usd": blocked,
                "status": "OBSERVED (Jupiter quote, live routes)"}
