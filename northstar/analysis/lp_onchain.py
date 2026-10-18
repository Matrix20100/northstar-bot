"""On-chain LP burn check for PumpSwap pools (no RugCheck needed).

Every Pump.fun coin that graduates lands in a PumpSwap pool, and its LP tokens are burned at
migration. RugCheck usually reports that, but when RugCheck is slow, rate-limited or has not
indexed a new pool yet, the LP gate became UNKNOWN and EVERY migrated coin was blocked.

This reads the facts directly from the chain:

  PumpSwap Pool account (owner pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA), little-endian:
    discriminator[8] | pool_bump u8 | index u16 | creator[32] | base_mint[32] | quote_mint[32] |
    lp_mint[32] | pool_base_token_account[32] | pool_quote_token_account[32] | lp_supply u64 | ...
  lp_supply = LP tokens minted when the pool was created.
  The LP mint's CURRENT supply is what still exists; burned tokens are gone from the supply.

  burned % = 1 - current_supply / lp_supply

Verified against the live $UV pool (8Xev4P…): base mint matched, lp_supply 4,193,388,318,622,
current LP supply 0 -> 100% burned.

The result is only used when every identity check passes (pool owned by PumpSwap, base mint is
the token being researched, LP mint owned by a token program). Anything else returns None and
the gate stays UNKNOWN — the check can confirm a burn, never invent one.
"""
from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from typing import Any, Optional

from .pump_trades import b58encode
from .pumpswap_trades import PUMPSWAP_PROGRAM

TOKEN_PROGRAMS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}
POOL_MIN_LEN = 211
BURNED_PCT = 95.0


@dataclass
class PoolLp:
    base_mint: str
    quote_mint: str
    lp_mint: str
    lp_supply: int


def parse_pumpswap_pool(data: bytes) -> Optional[PoolLp]:
    if len(data) < POOL_MIN_LEN:
        return None
    pk = lambda o: b58encode(data[o:o + 32])  # noqa: E731
    lp_supply = struct.unpack_from("<Q", data, 203)[0]
    return PoolLp(base_mint=pk(43), quote_mint=pk(75), lp_mint=pk(107), lp_supply=lp_supply)


def _raw_bytes(acc: dict) -> Optional[bytes]:
    data = acc.get("data")
    if isinstance(data, list) and len(data) == 2 and data[1] == "base64":
        try:
            return base64.b64decode(data[0])
        except (ValueError, TypeError):
            return None
    return None


def _mint_supply(acc: dict) -> Optional[int]:
    """Current supply of an SPL / Token-2022 mint, from a jsonParsed or base64 account."""
    data = acc.get("data")
    if isinstance(data, dict):
        info = ((data.get("parsed") or {}).get("info") or {})
        try:
            return int(info["supply"])
        except (KeyError, TypeError, ValueError):
            return None
    raw = _raw_bytes(acc)
    if raw is not None and len(raw) >= 44:
        return struct.unpack_from("<Q", raw, 36)[0]
    return None


async def pumpswap_lp_burn(rpc: Any, pool: str, mint: str) -> Optional[dict]:
    """{'burned_pct', 'lp_mint', 'lp_supply', 'current_supply'} when fully verified, else None."""
    if not rpc or not pool:
        return None
    pool_acc = await rpc.get_account_info(pool)
    if not pool_acc or pool_acc.get("owner") != PUMPSWAP_PROGRAM:
        return None
    raw = _raw_bytes(pool_acc)
    parsed = parse_pumpswap_pool(raw) if raw else None
    if parsed is None or parsed.base_mint != mint or parsed.lp_supply <= 0:
        return None
    lp_acc = await rpc.get_account_info(parsed.lp_mint)
    if not lp_acc or lp_acc.get("owner") not in TOKEN_PROGRAMS:
        return None
    current = _mint_supply(lp_acc)
    if current is None or current < 0 or current > parsed.lp_supply:
        return None
    burned = (1 - current / parsed.lp_supply) * 100
    return {"burned_pct": burned, "lp_mint": parsed.lp_mint, "lp_supply": parsed.lp_supply,
            "current_supply": current}
