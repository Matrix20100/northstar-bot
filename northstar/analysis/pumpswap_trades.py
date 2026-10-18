"""Verified decoding of PumpSwap (Pump.fun AMM) swaps for a selected pool.

Why this exists: after migration, ~94% of Northstar's candidates trade on PumpSwap. The generic
RPC parser cannot see what a PumpSwap instruction did (jsonParsed leaves it as raw bytes), so
every post-migration swap used to be an UNVERIFIED balance-delta candidate and trade statistics
could never be VERIFIED.

PumpSwap emits an Anchor ``BuyEvent`` / ``SellEvent`` for every swap (via ``emit_cpi`` - a
self-CPI whose data starts with the event tag - and/or a ``Program data:`` log line). A swap is
accepted as VERIFIED only when ALL of these hold:

  1. the transaction succeeded;
  2. the event was emitted BY the PumpSwap program (inner instruction whose programId is
     PumpSwap, or a log line while PumpSwap is the executing program) - a look-alike payload
     from another program is ignored;
  3. the event's ``pool`` is exactly the pool Northstar selected;
  4. the event's ``user`` has a pre/post token balance for the requested mint whose raw change
     equals the event amount exactly (sign and magnitude).

Anything else is reported with a reason and is never turned into a trade. A wrong assumption
about the event layout therefore produces zero verified swaps, not false data.

Event prefix layout (both events; little-endian, after the 8-byte discriminator):
  timestamp i64 | base_amount u64 | limit_quote u64 | user_base_reserves u64 |
  user_quote_reserves u64 | pool_base_reserves u64 | pool_quote_reserves u64 |
  quote_amount u64 | lp_fee_bps u64 | lp_fee u64 | protocol_fee_bps u64 | protocol_fee u64 |
  quote_amount_with_or_without_lp_fee u64 | user_quote_amount u64 | pool[32] | user[32] | ...
Later fields are ignored.
"""
from __future__ import annotations

import base64
import binascii
import struct
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..models import Trade
from .pump_trades import EVENT_IX_TAG, _all_instructions, _ix_data, _raw_token_deltas, b58encode

PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
BUY_EVENT_DISCRIMINATOR = bytes.fromhex("67f4521f2cf57777")   # sha256("event:BuyEvent")[:8]
SELL_EVENT_DISCRIMINATOR = bytes.fromhex("3e2f370aa503dc2a")  # sha256("event:SellEvent")[:8]
VERIFIED_PUMPSWAP = "VERIFIED_PUMPSWAP_EVENT"

_U64S = 13                              # u64 fields after the i64 timestamp
_POOL_OFFSET = 8 + 8 * _U64S            # 112
_USER_OFFSET = _POOL_OFFSET + 32        # 144
_MIN_BODY = _USER_OFFSET + 32           # 176 bytes after the discriminator

REASONS = (
    "decoded",
    "invalid_transaction",
    "transaction_failed",
    "no_pumpswap_instruction",
    "pumpswap_without_swap_event",
    "event_decode_error",
    "event_other_pool",
    "event_balance_missing",
    "event_balance_mismatch",
)


@dataclass(frozen=True)
class SwapEvent:
    is_buy: bool              # True = user bought BASE with QUOTE
    timestamp: int
    base_amount: int          # base_amount_out (buy) / base_amount_in (sell), raw units
    quote_amount: int         # quote_amount_in (buy) / quote_amount_out (sell), raw units, pre-fee
    user_quote_amount: int    # quote actually debited (buy) / credited (sell) to the user
    pool: str
    user: str


def decode_swap_event(disc: bytes, body: bytes) -> SwapEvent:
    if disc not in (BUY_EVENT_DISCRIMINATOR, SELL_EVENT_DISCRIMINATOR):
        raise ValueError("not a PumpSwap swap event")
    if len(body) < _MIN_BODY:
        raise ValueError(f"swap event too short: {len(body)} < {_MIN_BODY}")
    ts = struct.unpack_from("<q", body, 0)[0]
    u = struct.unpack_from("<" + "Q" * _U64S, body, 8)
    base_amount, quote_amount, user_quote_amount = u[0], u[6], u[12]
    pool = b58encode(body[_POOL_OFFSET:_POOL_OFFSET + 32])
    user = b58encode(body[_USER_OFFSET:_USER_OFFSET + 32])
    return SwapEvent(disc == BUY_EVENT_DISCRIMINATOR, ts, base_amount, quote_amount,
                     user_quote_amount, pool, user)


def _payloads_from_inner(tx: dict) -> list[bytes]:
    _top, inner = _all_instructions(tx)
    out = []
    for ix in inner:
        if ix.get("programId") != PUMPSWAP_PROGRAM:
            continue
        data = _ix_data(ix)
        if data and data.startswith(EVENT_IX_TAG):
            out.append(data[len(EVENT_IX_TAG):])
    return out


def _payloads_from_logs(logs: Optional[Iterable[str]]) -> list[bytes]:
    out: list[bytes] = []
    stack: list[str] = []
    for line in logs or []:
        if not isinstance(line, str):
            continue
        if line.startswith("Program data: "):
            if stack and stack[-1] == PUMPSWAP_PROGRAM:
                try:
                    out.append(base64.b64decode(line[len("Program data: "):].strip(), validate=True))
                except (binascii.Error, ValueError):
                    continue
            continue
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "Program":
            if parts[2].startswith("invoke"):
                stack.append(parts[1])
            elif parts[2] in ("success", "failed", "failed:") and stack:
                stack.pop()
    return out


def _mentions_pumpswap(tx: dict) -> bool:
    top, inner = _all_instructions(tx)
    if any(ix.get("programId") == PUMPSWAP_PROGRAM for ix in top + inner):
        return True
    logs = (tx.get("meta") or {}).get("logMessages") or []
    return any(isinstance(l, str) and l.startswith(f"Program {PUMPSWAP_PROGRAM} invoke") for l in logs)


@dataclass
class SwapDecodeResult:
    trades: list[Trade] = field(default_factory=list)
    reason: str = "decoded"
    notes: list[str] = field(default_factory=list)


def decode_pumpswap_transaction(tx: Optional[dict], mint: str, pool: str,
                                signature: str = "") -> SwapDecodeResult:
    """Return every VERIFIED PumpSwap swap of ``mint`` on ``pool`` inside one transaction."""
    if not isinstance(tx, dict) or not isinstance(tx.get("meta"), dict):
        return SwapDecodeResult(reason="invalid_transaction")
    meta = tx["meta"]
    if meta.get("err"):
        return SwapDecodeResult(reason="transaction_failed")
    if not _mentions_pumpswap(tx):
        return SwapDecodeResult(reason="no_pumpswap_instruction")

    payloads: list[bytes] = []
    seen: set[bytes] = set()
    for p in _payloads_from_inner(tx) + _payloads_from_logs(meta.get("logMessages")):
        if p[:8] in (BUY_EVENT_DISCRIMINATOR, SELL_EVENT_DISCRIMINATOR) and p not in seen:
            seen.add(p)          # the same event may appear both as CPI data and as a log line
            payloads.append(p)
    if not payloads:
        return SwapDecodeResult(reason="pumpswap_without_swap_event")

    events: list[SwapEvent] = []
    for p in payloads:
        try:
            events.append(decode_swap_event(p[:8], p[8:]))
        except (ValueError, struct.error):
            continue
    if not events:
        return SwapDecodeResult(reason="event_decode_error")
    mine = [e for e in events if e.pool == pool]
    if not mine:
        return SwapDecodeResult(reason="event_other_pool")

    deltas, decimals = _raw_token_deltas(tx, mint)

    # Hypothesis A: mint is the pool's BASE token (normal Pump.fun migration pool).
    # Hypothesis B: mint is the QUOTE token (inverted pool). Exactly one must reconcile.
    def expected(as_base: bool) -> dict[str, int]:
        want: dict[str, int] = {}
        for e in mine:
            if as_base:
                d = e.base_amount if e.is_buy else -e.base_amount
            else:
                d = -e.user_quote_amount if e.is_buy else e.user_quote_amount
            want[e.user] = want.get(e.user, 0) + d
        return want

    def reconciles(want: dict[str, int]) -> Optional[str]:
        for user, amount in want.items():
            if user not in deltas:
                return "event_balance_missing"
            if deltas[user] != amount:
                return "event_balance_mismatch"
        return None

    fail_base = reconciles(expected(True))
    fail_quote = reconciles(expected(False))
    if fail_base is None and fail_quote is None:
        return SwapDecodeResult(reason="event_balance_mismatch", notes=["ambiguous base/quote reconciliation"])
    if fail_base is not None and fail_quote is not None:
        return SwapDecodeResult(reason=fail_base)   # report the normal (base) hypothesis' failure
    as_base = fail_base is None

    sig = signature or str(((tx.get("transaction") or {}).get("signatures") or [""])[0])
    try:
        slot = int(tx.get("slot") or 0)
    except (TypeError, ValueError):
        slot = 0
    scale = 10 ** (decimals if decimals is not None else 6)
    notes: list[str] = []
    trades: list[Trade] = []
    for e in mine:
        bt = tx.get("blockTime")
        if not bt and e.timestamp > 0:
            bt = e.timestamp
            if "block_time_from_event" not in notes:
                notes.append("block_time_from_event")
        if as_base:
            side = "buy" if e.is_buy else "sell"
            token_raw, sol_raw = e.base_amount, e.quote_amount
        else:
            side = "sell" if e.is_buy else "buy"
            token_raw, sol_raw = e.user_quote_amount, e.base_amount
        # SOL leg assumes the other side of the pool is wrapped SOL (Northstar only selects
        # SOL-quoted pools); it is the pool-side amount before fees.
        trades.append(Trade(sig, slot, bt, e.user, side, token_raw / scale, sol_raw / 1e9, VERIFIED_PUMPSWAP))
    return SwapDecodeResult(trades=trades, reason="decoded", notes=notes)
