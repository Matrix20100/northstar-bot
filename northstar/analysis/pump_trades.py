"""Decode Pump.fun bonding-curve trades from jsonParsed Solana transactions.

Why this module exists
----------------------
Solana's ``jsonParsed`` encoding only expands instructions for programs the RPC node
understands (System, SPL Token, ...). Pump.fun instructions come back *unparsed*
(``programId`` + base58 ``data``), so a parser that looks for a ``parsed`` swap instruction
never finds one, and a parser that only looks at token-balance changes cannot tell a trade
from a transfer.

What the Pump.fun program does emit on every successful buy/sell is an Anchor ``TradeEvent``
containing the mint, SOL amount, token amount, buy/sell flag, trader and timestamp. This
module reads that event. A trade is accepted only if ALL of the following hold:

  1. the transaction succeeded;
  2. a TradeEvent was emitted by the Pump.fun program itself (inner-instruction CPI data, or a
     ``Program data:`` log line emitted while Pump.fun was the executing program);
  3. the event's mint is the mint being researched;
  4. the event's token amount is confirmed by the transaction's own pre/post token balances
     for the event's trader (exact raw-unit match, correct sign).

Anything else is reported with an explicit reason and is never counted as a trade.

Documented formats used (Pump.fun IDL / Anchor conventions)
-----------------------------------------------------------
* Anchor event discriminator  = sha256("event:TradeEvent")[:8] = bddb7fd34ee661ee
* Anchor ``emit_cpi`` data    = e445a52e51cb9a1d + event discriminator + borsh fields
* ``Program data:`` log line  = base64(event discriminator + borsh fields)
* TradeEvent prefix (little-endian borsh), after the 8-byte discriminator:
      mint pubkey[32] | sol_amount u64 | token_amount u64 | is_buy bool |
      user pubkey[32] | timestamp i64 | virtual_sol u64 | virtual_token u64 |
      real_sol u64 | real_token u64 | ...later fields are ignored
Later program versions append fields; only the stable prefix is read. If the layout ever
changes in a way that breaks the prefix, the balance cross-check (step 4) rejects the event
instead of producing a wrong trade, and the failure shows up in the diagnostic counters.

This format description has been unit-tested against synthetic transactions built to it. It
has NOT been validated against live RPC data from this environment.
"""
from __future__ import annotations

import base64
import binascii
import struct
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..models import Trade

PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

EVENT_IX_TAG = bytes.fromhex("e445a52e51cb9a1d")            # Anchor emit_cpi tag (LE)
TRADE_EVENT_DISCRIMINATOR = bytes.fromhex("bddb7fd34ee661ee")  # sha256("event:TradeEvent")[:8]

# sha256("global:<name>")[:8] for Pump.fun instructions (computed, not guessed).
INSTRUCTION_NAMES: dict[bytes, str] = {
    bytes.fromhex("66063d1201daebea"): "buy",
    bytes.fromhex("33e685a4017f83ad"): "sell",
    bytes.fromhex("38fc74089edfcd5f"): "buy_exact_sol_in",
    bytes.fromhex("181ec828051c0777"): "create",
    bytes.fromhex("d6904cec5f8b31b4"): "create_v2",
    bytes.fromhex("ea66c2cb96483ee5"): "extend_account",
    bytes.fromhex("9beae792ec9ea21e"): "migrate",
    bytes.fromhex("1416567bc61cdb84"): "collect_creator_fee",
    bytes.fromhex("fe94ff70cf8eaaa5"): "set_creator",
    bytes.fromhex("b712469c946da122"): "withdraw",
}
TRADE_INSTRUCTIONS = {"buy", "sell", "buy_exact_sol_in"}

VERIFIED = "VERIFIED_PUMP_EVENT"
UNVERIFIED = "UNVERIFIED_CANDIDATE"
PUMP_TOKEN_DECIMALS = 6  # Pump.fun mints always use 6; used only if balances omit decimals

# Every reason decode_curve_transaction can return. Kept explicit so counters never silently
# lose a category.
REASONS = (
    "decoded",
    "invalid_transaction",
    "transaction_failed",
    "no_pump_instruction",
    "non_trade_pump_instruction",
    "unknown_pump_instruction",
    "trade_instruction_without_event",
    "event_decode_error",
    "event_other_mint",
    "event_balance_missing",
    "event_balance_mismatch",
)

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}


# --------------------------------------------------------------------------- base58 (no dependency)
def b58decode(text: str) -> bytes:
    if not isinstance(text, str) or not text:
        raise ValueError("empty base58")
    n = 0
    for ch in text:
        try:
            n = n * 58 + _B58_INDEX[ch]
        except KeyError:
            raise ValueError("invalid base58 character") from None
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\x00" * pad + body


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(data) - len(data.lstrip(b"\x00"))
    return "1" * pad + out


# --------------------------------------------------------------------------- event model
@dataclass(frozen=True)
class TradeEvent:
    mint: str
    sol_lamports: int
    token_raw: int
    is_buy: bool
    user: str
    timestamp: int
    virtual_sol: Optional[int] = None
    virtual_token: Optional[int] = None
    real_sol: Optional[int] = None
    real_token: Optional[int] = None


_PREFIX_LEN = 32 + 8 + 8 + 1 + 32 + 8   # 89 bytes after the discriminator
_RESERVES_LEN = _PREFIX_LEN + 32


def decode_trade_event(body: bytes) -> TradeEvent:
    """Decode the borsh fields that follow the 8-byte TradeEvent discriminator."""
    if len(body) < _PREFIX_LEN:
        raise ValueError(f"TradeEvent too short ({len(body)} bytes)")
    mint = b58encode(body[0:32])
    sol, tok = struct.unpack_from("<QQ", body, 32)
    flag = body[48]
    if flag not in (0, 1):
        raise ValueError("TradeEvent is_buy is not a boolean")
    user = b58encode(body[49:81])
    (ts,) = struct.unpack_from("<q", body, 81)
    reserves = (None, None, None, None)
    if len(body) >= _RESERVES_LEN:
        reserves = struct.unpack_from("<QQQQ", body, _PREFIX_LEN)
    return TradeEvent(mint, sol, tok, bool(flag), user, ts, *reserves)


# --------------------------------------------------------------------------- transaction scanning
def _all_instructions(tx: dict) -> tuple[list[dict], list[dict]]:
    message = (tx.get("transaction") or {}).get("message") or {}
    top = [i for i in (message.get("instructions") or []) if isinstance(i, dict)]
    inner: list[dict] = []
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        if isinstance(group, dict):
            inner.extend(i for i in (group.get("instructions") or []) if isinstance(i, dict))
    return top, inner


def _ix_data(ix: dict) -> Optional[bytes]:
    data = ix.get("data")
    if not isinstance(data, str) or not data:
        return None
    try:
        return b58decode(data)
    except ValueError:
        return None


def classify_pump_instructions(tx: dict) -> dict[str, int]:
    """Count Pump.fun instructions by name (top-level and inner). Diagnostic only."""
    top, inner = _all_instructions(tx)
    counts: dict[str, int] = {}
    for ix in top + inner:
        if ix.get("programId") != PUMP_FUN_PROGRAM:
            continue
        data = _ix_data(ix)
        if data is None:
            name = "missing_data"
        elif data.startswith(EVENT_IX_TAG):
            name = "event_cpi"
        else:
            name = INSTRUCTION_NAMES.get(data[:8], "unknown")
        counts[name] = counts.get(name, 0) + 1
    return counts


def _events_from_inner_instructions(tx: dict) -> list[bytes]:
    _top, inner = _all_instructions(tx)
    bodies = []
    prefix = EVENT_IX_TAG + TRADE_EVENT_DISCRIMINATOR
    for ix in inner:
        if ix.get("programId") != PUMP_FUN_PROGRAM:
            continue
        data = _ix_data(ix)
        if data and data.startswith(prefix):
            bodies.append(data[len(prefix):])
    return bodies


def _events_from_logs(logs: Optional[Iterable[str]]) -> list[bytes]:
    """Collect ``Program data:`` payloads emitted while Pump.fun was the executing program."""
    bodies: list[bytes] = []
    stack: list[str] = []
    for line in logs or []:
        if not isinstance(line, str):
            continue
        if line.startswith("Program data: "):
            if stack and stack[-1] == PUMP_FUN_PROGRAM:
                try:
                    raw = base64.b64decode(line[len("Program data: "):].strip(), validate=True)
                except (binascii.Error, ValueError):
                    continue
                if raw.startswith(TRADE_EVENT_DISCRIMINATOR):
                    bodies.append(raw[len(TRADE_EVENT_DISCRIMINATOR):])
            continue
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "Program":
            if parts[2].startswith("invoke"):
                stack.append(parts[1])
            elif parts[2] == "success" or parts[2] == "failed:" or parts[2] == "failed":
                if stack:
                    stack.pop()
    return bodies


def _raw_token_deltas(tx: dict, mint: str) -> tuple[dict[str, int], Optional[int]]:
    """Owner -> raw token delta for ``mint`` (post - pre), plus the mint's decimals if present."""
    meta = tx.get("meta") or {}
    deltas: dict[str, int] = {}
    decimals: Optional[int] = None
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for bal in meta.get(side) or []:
            if not isinstance(bal, dict) or bal.get("mint") != mint:
                continue
            owner = bal.get("owner")
            ui = bal.get("uiTokenAmount") or {}
            try:
                raw = int(ui.get("amount"))
            except (TypeError, ValueError):
                continue
            if decimals is None and isinstance(ui.get("decimals"), int):
                decimals = ui["decimals"]
            if not owner:
                continue
            deltas[owner] = deltas.get(owner, 0) + sign * raw
    return deltas, decimals


@dataclass
class DecodeResult:
    trades: list[Trade] = field(default_factory=list)
    reason: str = "decoded"
    notes: list[str] = field(default_factory=list)


def decode_curve_transaction(tx: Optional[dict], mint: str, signature: str = "") -> DecodeResult:
    """Decode every verified Pump.fun trade for ``mint`` inside one transaction."""
    if not isinstance(tx, dict):
        return DecodeResult(reason="invalid_transaction")
    meta = tx.get("meta")
    if not isinstance(meta, dict):
        return DecodeResult(reason="invalid_transaction")
    if meta.get("err"):
        return DecodeResult(reason="transaction_failed")

    bodies: list[bytes] = []
    seen: set[bytes] = set()
    for body in _events_from_inner_instructions(tx) + _events_from_logs(meta.get("logMessages")):
        if body not in seen:          # same event can legitimately appear via more than one source
            seen.add(body)
            bodies.append(body)

    if not bodies:
        counts = classify_pump_instructions(tx)
        if not counts:
            return DecodeResult(reason="no_pump_instruction")
        if any(name in TRADE_INSTRUCTIONS for name in counts):
            return DecodeResult(reason="trade_instruction_without_event",
                                notes=[f"pump instructions: {counts}"])
        if set(counts) - {"unknown", "missing_data", "event_cpi"}:
            return DecodeResult(reason="non_trade_pump_instruction")
        return DecodeResult(reason="unknown_pump_instruction", notes=[f"pump instructions: {counts}"])

    events: list[TradeEvent] = []
    errors = 0
    for body in bodies:
        try:
            events.append(decode_trade_event(body))
        except (ValueError, struct.error):
            errors += 1
    if not events:
        return DecodeResult(reason="event_decode_error")

    mine = [e for e in events if e.mint == mint]
    if not mine:
        return DecodeResult(reason="event_other_mint")

    deltas, decimals = _raw_token_deltas(tx, mint)
    expected: dict[str, int] = {}
    for e in mine:
        expected[e.user] = expected.get(e.user, 0) + (e.token_raw if e.is_buy else -e.token_raw)
    for user, want in expected.items():
        if user not in deltas:
            return DecodeResult(reason="event_balance_missing",
                                notes=["trader has no pre/post token balance entry for this mint"])
        if deltas[user] != want:
            return DecodeResult(reason="event_balance_mismatch",
                                notes=[f"event says {want} raw units, balances say {deltas[user]}"])

    sig = signature or str(((tx.get("transaction") or {}).get("signatures") or [""])[0])
    try:
        slot = int(tx.get("slot") or 0)
    except (TypeError, ValueError):
        slot = 0
    block_time = tx.get("blockTime")
    notes: list[str] = []
    scale = 10 ** (decimals if decimals is not None else PUMP_TOKEN_DECIMALS)
    trades = []
    for e in mine:
        bt = block_time
        if not bt and e.timestamp and e.timestamp > 0:
            bt = e.timestamp
            if "block_time_from_event" not in notes:
                notes.append("block_time_from_event")
        trades.append(Trade(sig, slot, bt, e.user, "buy" if e.is_buy else "sell",
                            e.token_raw / scale, e.sol_lamports / 1e9, VERIFIED))
    return DecodeResult(trades=trades, reason="decoded", notes=notes)
