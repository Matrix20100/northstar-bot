"""Verified prices (v5.1.3). Operator: "the bot lies to me, it posts milestones that didn't happen and lies about the
current price of a coin."

Seen: $Pumpers (called at $16.6K, still on its bonding curve, real high $20.5K) was posted as "hit 25x ... peak
27.10x ($450.7K)"; $Pilly (called at $26.9K, real high about $40K) showed a $1.06M peak. Both readings came from
DexScreener, not from the chain:

  * When the bot cannot read a coin's pump.fun curve (another launchpad, a non-`pump` mint, or a failed RPC read),
    the early-call tracker fell back to DexScreener and took the DEEPEST pool listed for the mint, whatever it was,
    trusted its `market_cap or fdv`, and marked the coin migrated. One badly priced pool (a thin side pool, a
    wrong fdv) was enough to post a 25x that never happened, and the same number became the "current price".
  * Nothing checked a sudden jump: the paper fills had a 20x sanity rail, the tracker had none.

The guard:

  1. `trusted(pair)`: a DexScreener pool is only used when it is quoted in SOL/USDC/USDT and holds real liquidity
     (at least $1,500 and at least 2% of the market cap it claims). A pool claiming a $450K market cap on $900 of
     liquidity is not a price.
  2. `PriceGuard.verify()`: a DexScreener reading that jumps 2x or more above the last accepted price is checked
     against Jupiter, which quotes what a real 0.05 SOL buy gets now (routing only through pools that can fill it).
     Jupiter's market cap = price per token x the token's supply read from the chain. If the two agree (within
     1.67x) the reading stands; if not, Jupiter's number is used and the disagreement is logged. If Jupiter cannot be
     asked right now, the jump waits (nothing is posted on an unchecked jump). With Jupiter switched off, a jump needs
     a second reading that agrees.
  3. Falls of 2x or more need a second reading that agrees (or Jupiter agreeing), so one bad quote cannot fake a
     dump either.

Chain reads of a pump.fun curve are exact and are not second-guessed. Nothing is invented: a reading the guard
cannot confirm is simply not used.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .providers.solana_rpc import QUOTE_MINTS

log = logging.getLogger("northstar.price_guard")

CURVE_DEXES = ("pumpfun", "pump.fun", "pump")
MIN_POOL_LIQ_USD = 1_500.0
MIN_POOL_LIQ_SHARE = 0.02          # liquidity at least this share of the market cap the pool claims
JUMP_X = 2.0                       # a move this big (up or down) since the last accepted price is checked
AGREE_X = 1.67                     # two prices within this ratio agree
CONFIRM_X = 1.35                   # a second DexScreener read must be within this of the first
PENDING_TTL = 300.0                # an unconfirmed reading is forgotten after this many seconds
QUOTE_SOL = 0.05                   # (kept for older callers)
QUOTE_USDC = 10.0                  # v5.2.1: the Jupiter buy quote is in USDC: a dollar price that does not depend
                                   # on the bot's own SOL/USD price (a wrong SOL price fooled the SOL-based check)
CONFIRM_TTL = 60.0                 # a milestone check result is reused for this long
CURVE_MAX_MCAP_SOL = 450.0         # a pump.fun curve completes near 410 SOL of market cap: more is not a curve price


def mcap_of(p) -> Optional[float]:
    v = getattr(p, "market_cap", None) or getattr(p, "fdv", None)
    try:
        v = float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
    return v if v and v > 0 else None


def is_curve(p) -> bool:
    return (getattr(p, "dex_id", "") or "").lower() in CURVE_DEXES


def trusted(p, mint: Optional[str] = None) -> bool:
    """Is this DexScreener pool a believable price for the coin?"""
    if p is None or (mint and getattr(p, "base_mint", None) != mint):
        return False
    mc = mcap_of(p)
    if not mc:
        return False
    if is_curve(p):
        return True                                  # the pump.fun curve itself (DexScreener lists it)
    quote = getattr(p, "quote_mint", None)
    if quote and quote not in QUOTE_MINTS:
        return False                                 # priced in some other token: not a USD price we can use
    liq = getattr(p, "liquidity_usd", None) or 0.0
    return liq >= MIN_POOL_LIQ_USD and liq >= MIN_POOL_LIQ_SHARE * mc


def agree(a: float, b: float, x: float = AGREE_X) -> bool:
    return a > 0 and b > 0 and max(a, b) / min(a, b) <= x


class PriceGuard:
    def __init__(self, rpc=None, jupiter=None, sol_price=None, now=None):
        self.rpc, self.jupiter, self.sol_price = rpc, jupiter, sol_price
        self._now = now or time.time
        self._supply: dict[str, tuple[float, int]] = {}      # mint -> (supply in whole tokens, decimals)
        self._pending: dict[str, tuple[float, float]] = {}   # mint -> (reading, when)
        self.stats = {"checked": 0, "confirmed": 0, "corrected": 0, "held": 0}

    async def _supply_of(self, mint: str) -> Optional[tuple[float, int]]:
        if mint in self._supply:
            return self._supply[mint]
        if self.rpc is None:
            return None
        try:
            v = await self.rpc.get_token_supply(mint)
        except Exception:  # noqa: BLE001
            return None
        try:
            dec = int(v["decimals"])
            ui = float(v.get("uiAmount") or int(v["amount"]) / 10 ** dec)
        except (TypeError, ValueError, KeyError):
            return None
        if ui <= 0:
            return None
        self._supply[mint] = (ui, dec)
        return self._supply[mint]

    async def jupiter_mcap(self, mint: str):
        """Market cap implied by a real 10 USDC buy on Jupiter now, times the supply read from the chain. In dollars
        directly, so it does not depend on the bot's SOL/USD price. None = cannot ask; "NO_ROUTE" = nothing fills."""
        if self.jupiter is None:
            return None
        sup = await self._supply_of(mint)
        if not sup:
            return None
        from .providers.solana_rpc import USDC_MINT
        try:
            q = await self.jupiter.quote(USDC_MINT, int(QUOTE_USDC * 1e6), out_mint=mint)
        except Exception:  # noqa: BLE001
            return None
        if q is None:
            return None
        if q.get("no_route") or not q.get("out_amount"):
            return "NO_ROUTE"
        supply, dec = sup
        tokens = float(q["out_amount"]) / 10 ** dec
        if tokens <= 0:
            return "NO_ROUTE"
        return QUOTE_USDC / tokens * supply

    async def confirm_milestone(self, mint: str, mcap: float, label: str = "") -> tuple[bool, Optional[float]]:
        """v5.2.1: may a milestone be posted at this market cap? (ok, Jupiter's market cap). Every milestone, from any
        price source (chain curve, DexScreener), is checked against a real dollar buy quote first. Operator: "bot
        lied again" ($UP posted 40x and 50x at $975K; its real high was $44.7K)."""
        if self.jupiter is None:
            if not self.__dict__.get("_no_jup_logged"):
                self._no_jup_logged = True
                log.warning("milestone check: Jupiter is switched off (NORTHSTAR_ENABLE_JUPITER); milestones are "
                            "posted unchecked")
            return True, None
        now = self._now()
        cache = self.__dict__.setdefault("_confirm", {})
        hit = cache.get(mint)
        if hit and now - hit[0] < CONFIRM_TTL:
            jup = hit[1]
        else:
            jup = await self.jupiter_mcap(mint)
            if jup is not None:
                cache[mint] = (now, jup)
        if isinstance(jup, float):
            if jup * AGREE_X >= mcap:                       # a real buy prices it at least this high (within 1.67x)
                return True, jup
            log.warning("milestone check %s%s: the bot read $%.0f but a real buy on Jupiter prices it at $%.0f; "
                        "milestone NOT posted, price corrected", mint, f" ({label})" if label else "", mcap, jup)
            return False, jup
        if jup == "NO_ROUTE":
            log.warning("milestone check %s%s: nothing can buy it on Jupiter; milestone not posted", mint,
                        f" ({label})" if label else "")
        return False, None

    async def verify(self, mint: str, reading: float, last: Optional[float], label: str = "") -> Optional[float]:
        """The market cap to use for this DexScreener `reading` (None = do not use it yet). `last` is the last
        accepted market cap for the coin (the call's entry for a new call)."""
        if not reading or reading <= 0:
            return None
        if not last or last <= 0:
            return reading
        up, down = reading >= JUMP_X * last, reading * JUMP_X <= last
        if not (up or down):
            self._pending.pop(mint, None)
            return reading
        self.stats["checked"] += 1
        now = self._now()
        pend = self._pending.get(mint)
        second = bool(pend and now - pend[1] <= PENDING_TTL and agree(reading, pend[0], CONFIRM_X))
        jup = await self.jupiter_mcap(mint)
        if isinstance(jup, float):
            self._pending.pop(mint, None)
            if agree(reading, jup):
                self.stats["confirmed"] += 1
                return reading
            self.stats["corrected"] += 1
            log.warning("price check %s%s: DexScreener says $%.0f but a real buy on Jupiter prices it at $%.0f "
                        "(last $%.0f); using Jupiter", mint, f" ({label})" if label else "", reading, jup, last)
            return jup
        if jup == "NO_ROUTE":
            if down:
                self._pending.pop(mint, None)
                return reading                       # nothing can buy it: the fall is real
            self.stats["held"] += 1
            log.warning("price check %s%s: DexScreener says $%.0f (%.1fx the last $%.0f) but Jupiter cannot route a "
                        "buy; not used", mint, f" ({label})" if label else "", reading, reading / last, last)
            return None
        # Jupiter could not be asked (off, or failing right now)
        if down and second:
            self._pending.pop(mint, None)
            return reading
        if up and second and self.jupiter is None:
            self._pending.pop(mint, None)
            return reading                           # Jupiter switched off: two agreeing reads
        if not second:
            self._pending[mint] = (reading, now)
        self.stats["held"] += 1
        return None
