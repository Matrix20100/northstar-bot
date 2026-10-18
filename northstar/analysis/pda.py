"""Solana program-derived addresses (PDAs), stdlib only.

Used to find a Pump.fun token's bonding-curve account (and its Metaplex metadata account) from
the mint alone, so pre-migration research does not depend on having seen the launch event or on
DexScreener. Any derived address is still verified by reading the account on-chain (owner and
discriminator) before use, so a derivation mistake can only cause "not found", never wrong data.
"""
from __future__ import annotations

import hashlib
from typing import Optional

from .pump_trades import PUMP_FUN_PROGRAM, b58decode, b58encode

METAPLEX_METADATA_PROGRAM = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"

_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def is_on_curve(point: bytes) -> bool:
    """True if 32 bytes decompress to an ed25519 point (same rule as curve25519-dalek)."""
    if len(point) != 32:
        return False
    y = int.from_bytes(point, "little") & ((1 << 255) - 1)
    y %= _P
    u = (y * y - 1) % _P
    v = (_D * y * y + 1) % _P
    # candidate root of u/v: x = u v^3 (u v^7)^((p-5)/8)
    x = (u * pow(v, 3, _P) * pow(u * pow(v, 7, _P), (_P - 5) // 8, _P)) % _P
    vx2 = (v * x * x) % _P
    if vx2 == u % _P:
        return True
    if vx2 == (-u) % _P:
        return True
    return False


def find_program_address(seeds: list[bytes], program_id: str) -> Optional[tuple[str, int]]:
    program = b58decode(program_id)
    for bump in range(255, -1, -1):
        h = hashlib.sha256()
        for seed in seeds:
            h.update(seed)
        h.update(bytes([bump]))
        h.update(program)
        h.update(b"ProgramDerivedAddress")
        candidate = h.digest()
        if not is_on_curve(candidate):
            return b58encode(candidate), bump
    return None


def pump_bonding_curve_address(mint: str) -> Optional[str]:
    try:
        found = find_program_address([b"bonding-curve", b58decode(mint)], PUMP_FUN_PROGRAM)
    except ValueError:
        return None
    return found[0] if found else None


def metaplex_metadata_address(mint: str) -> Optional[str]:
    try:
        found = find_program_address(
            [b"metadata", b58decode(METAPLEX_METADATA_PROGRAM), b58decode(mint)], METAPLEX_METADATA_PROGRAM)
    except ValueError:
        return None
    return found[0] if found else None


def parse_metaplex_metadata(data: bytes) -> Optional[dict]:
    """Parse name/symbol/uri from a Metaplex Metadata account (key, update_authority, mint, data)."""
    try:
        if not data or data[0] != 4:          # Key::MetadataV1
            return None
        off = 1 + 32 + 32
        out = {}
        for field, cap in (("name", 64), ("symbol", 32), ("uri", 400)):
            n = int.from_bytes(data[off:off + 4], "little")
            off += 4
            if n > cap or off + n > len(data):
                return None
            out[field] = data[off:off + n].decode("utf-8", "replace").rstrip("\x00").strip()
            off += n
        return out
    except (IndexError, ValueError):
        return None
