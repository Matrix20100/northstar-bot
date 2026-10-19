"""On-chain PumpSwap LP burn check. SYNTHETIC accounts built with the verified live layout."""
from __future__ import annotations

import asyncio
import base64
import struct

from conftest import MINT, make_pair
from northstar.analysis.lp_onchain import PUMPSWAP_PROGRAM, parse_pumpswap_pool, pumpswap_lp_burn
from northstar.analysis.pump_trades import b58encode
from northstar.analysis.security import assess_liquidity
from northstar.config import Settings
from northstar.models import GateStatus
from northstar.research import Researcher

S = Settings(_env_file=None)
POOL = "8Xev4PoVsLfyt1A31cGUcGU9tW738MaGsB2MczezcgVf"
LP_MINT_BYTES = bytes(range(1, 33))
LP_MINT = b58encode(LP_MINT_BYTES)
T22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


def b58decode(s: str) -> bytes:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = 0
    for ch in s:
        n = n * 58 + alphabet.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(s) - len(s.lstrip("1"))) + raw


def pool_bytes(base=MINT, lp_supply=4_193_388_318_622) -> bytes:
    d = bytearray(301)
    d[0:8] = bytes([241, 154, 109, 4, 17, 177, 109, 188])
    d[8] = 255
    d[43:75] = b58decode(base).rjust(32, b"\0")
    d[75:107] = b58decode("So11111111111111111111111111111111111111112")
    d[107:139] = LP_MINT_BYTES
    struct.pack_into("<Q", d, 203, lp_supply)
    return bytes(d)


class Rpc:
    def __init__(self, pool_owner=PUMPSWAP_PROGRAM, base=MINT, current=0, lp_owner=T22):
        self.accounts = {
            POOL: {"owner": pool_owner, "data": [base64.b64encode(pool_bytes(base)).decode(), "base64"]},
            LP_MINT: {"owner": lp_owner, "data": {"parsed": {"info": {"supply": str(current), "decimals": 9}}}},
        }

    async def get_account_info(self, a):
        return self.accounts.get(a)


def test_layout_parses():
    p = parse_pumpswap_pool(pool_bytes())
    assert p.base_mint == MINT and p.lp_mint == LP_MINT and p.lp_supply == 4_193_388_318_622


def test_fully_burned():
    r = asyncio.run(pumpswap_lp_burn(Rpc(), POOL, MINT))
    assert r["burned_pct"] == 100.0


def test_never_confirms_on_mismatch():
    assert asyncio.run(pumpswap_lp_burn(Rpc(pool_owner="11111111111111111111111111111111"), POOL, MINT)) is None
    assert asyncio.run(pumpswap_lp_burn(Rpc(base="So11111111111111111111111111111111111111112"), POOL, MINT)) is None
    assert asyncio.run(pumpswap_lp_burn(Rpc(lp_owner="11111111111111111111111111111111"), POOL, MINT)) is None
    assert asyncio.run(pumpswap_lp_burn(Rpc(current=9_999_999_999_999), POOL, MINT)) is None  # more than minted


def _verify(rpc):
    pair = make_pair(pair_address=POOL, dex_id="pumpswap")
    la = assess_liquidity(pair, None, [], S)                     # RugCheck down
    assert next(g for g in la.gates if g.name == "lp_lock").status == GateStatus.UNKNOWN
    r = Researcher.__new__(Researcher)
    r.p = type("P", (), {"rpc": rpc})()
    report = {"sources": {}}
    asyncio.run(r._verify_lp_onchain(pair, MINT, la, report))
    return la


def test_research_passes_lp_gate_when_rugcheck_is_down():
    la = _verify(Rpc())
    gate = next(g for g in la.gates if g.name == "lp_lock")
    assert gate.status == GateStatus.PASS and "VERIFIED on-chain" in gate.reason
    assert not any(f.code == "LP_STATUS_UNKNOWN" for f in la.flags)


def test_partial_burn_stays_blocked_and_flagged():
    # v2.5: a MEASURED partial burn is a FAIL, not an UNKNOWN. v2.4 left it UNKNOWN, which
    # meant INSUFFICIENT DATA and an endless re-research of a coin whose LP was known to be
    # half withdrawable. It is still blocked — now for the right reason.
    la = _verify(Rpc(current=4_193_388_318_622 // 2))
    gate = next(g for g in la.gates if g.name == "lp_lock")
    assert gate.status == GateStatus.FAIL and "PARTIAL" in gate.reason
    assert any(f.code == "LP_NOT_LOCKED" for f in la.flags)


def test_rpc_error_leaves_gate_unknown():
    class Broken:
        async def get_account_info(self, a):
            raise RuntimeError("rpc down")
    la = _verify(Broken())
    assert next(g for g in la.gates if g.name == "lp_lock").status == GateStatus.UNKNOWN
