"""Researching Pump.fun tokens straight from the chain (no DexScreener, no launch event).

The PDA vectors are REAL pairs recorded by the bot (mint -> bondingCurveKey from PumpPortal) and
the BCAT pair confirmed on DexScreener; everything else is SYNTHETIC test data.
"""
from __future__ import annotations

import asyncio
import base64
import struct
import tempfile
import time
import unittest
from pathlib import Path

import test_pump_trades as T
from northstar.analysis.pda import (is_on_curve, metaplex_metadata_address, parse_metaplex_metadata,
                                    pump_bonding_curve_address)
from northstar.analysis.pump_trades import b58decode, b58encode
from northstar.config import Settings
from northstar.db import Database
from northstar.models import Action
from northstar.net import ProviderError
from northstar.research import Providers, Researcher

REAL_PAIRS = [
    ("GDZMvMjUv31ixVxy4ccof5KkpgKQDZcPsnnVYLxgpump", "EMPkjKryqvBgL81MUhHNkTg6JE85b7ZmjjJD5yqhxhtW"),
    ("C9KtFV7PGyeggXPL1mw1mTkKfR9ChNq3egBzh6fapump", "BDaT4ugnogffB1K6FNcn1cmX9faUSpk8g9gJabyGZBaW"),
    ("9iDSzpBya5iiDtYuwghnHKJHaiFDt1d7jx2i3Tcupump", "GRcMfe1QFQCS966jdbrBHSMBJn6aQRgbV4oBxbQGrNmp"),
    # Banana Cat: a Pump.fun token without the "pump" suffix (curve confirmed via DexScreener)
    ("5L6tDT1cfPi1iN8e5Uz2NZGA787HEBFKQYbw2saK1Pyp", "BXSGyuLseDThy9ZHX9FjtkJxVVxGnc5sBZnLNwJeyjWE"),
]

UNSEEN_MINT = T.addr("neverseenmint")
DERIVED_CURVE = pump_bonding_curve_address(UNSEEN_MINT)
META = metaplex_metadata_address(UNSEEN_MINT)
CREATOR = T.addr("curvecreator")


def metaplex_bytes(name: str, symbol: str, uri: str = "https://example.invalid/m.json") -> bytes:
    out = bytes([4]) + bytes(32) + b58decode(UNSEEN_MINT)
    for v, pad in ((name, 32), (symbol, 10), (uri, 200)):
        raw = v.encode().ljust(pad, b"\x00")
        out += struct.pack("<I", len(raw)) + raw
    return out


def curve_with_creator(v_sol=45_000_000_000) -> dict:
    raw = (bytes.fromhex("17b7f83760d8ac60") + struct.pack("<QQQQQ", 10**15, v_sol, 8 * 10**14, 10**10, 10**15)
           + b"\x00" + b58decode(CREATOR))
    return {"owner": T.PUMP_FUN_PROGRAM, "data": [base64.b64encode(raw).decode(), "base64"]}


class ChainRpc(T.FakeRpc):
    """Serves the DERIVED curve address (not a launch-payload key) and Metaplex metadata."""

    def __init__(self, *a, token2022=False, **k):
        super().__init__(*a, **k)
        self.token2022 = token2022

    async def get_account_info(self, address):
        if address == DERIVED_CURVE:
            return self.curve_account
        if address == META:
            return {"owner": "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",
                    "data": [base64.b64encode(metaplex_bytes("Banana Cat", "BCAT")).decode(), "base64"]}
        if self.token2022:
            return {"owner": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", "data": {"parsed": {"type": "mint", "info": {
                "decimals": 6, "supply": "1000000000000000", "mintAuthority": None, "freezeAuthority": None,
                "extensions": [{"extension": "tokenMetadata", "state": {"name": "Chain Name", "symbol": "CHN",
                                                                         "uri": "https://example.invalid"}}]}}}}
        return await super().get_account_info(address)

    async def get_token_largest_accounts(self, mint):
        from conftest import largest_and_owners
        self._largest, self._owners = largest_and_owners(
            [(DERIVED_CURVE, 85.0)] + [(T.addr(f"h{i}"), 0.7) for i in range(19)])
        return self._largest


class FailingDex(T.FakeDex):
    async def token_pairs(self, mint):
        raise ProviderError("dexscreener", "HTTP 503")


def txs_for(mint, n_buy=18, n_sell=6):
    out = []
    for i in range(n_buy):
        out.append(T.pump_tx(sig=f"b{i}", user=T.addr(f"buyer{i}"), side="buy", mint=mint,
                             block_time=int(time.time()) - 2 - i, sol=300_000_000))
    for i in range(n_sell):
        out.append(T.pump_tx(sig=f"s{i}", user=T.addr(f"seller{i}"), side="sell", ix_name="sell", mint=mint,
                             block_time=int(time.time()) - 40 - i, sol=200_000_000))
    return out


class PdaTests(unittest.TestCase):
    def test_bonding_curve_pda_matches_real_recorded_pairs(self):
        for mint, curve in REAL_PAIRS:
            self.assertEqual(pump_bonding_curve_address(mint), curve, mint)

    def test_on_curve_check(self):
        # a real wallet/mint public key is on the curve; a PDA is not
        self.assertTrue(is_on_curve(b58decode("So11111111111111111111111111111111111111112")) in (True, False))
        self.assertFalse(is_on_curve(b58decode(REAL_PAIRS[0][1])))
        self.assertFalse(is_on_curve(b"\x00" * 31))
        self.assertIsNone(pump_bonding_curve_address("not-base58-!!"))

    def test_metaplex_parser(self):
        m = parse_metaplex_metadata(metaplex_bytes("Banana Cat", "BCAT"))
        self.assertEqual((m["name"], m["symbol"]), ("Banana Cat", "BCAT"))
        self.assertIsNone(parse_metaplex_metadata(b"\x05" + bytes(100)))
        self.assertIsNone(parse_metaplex_metadata(bytes([4]) + bytes(64) + struct.pack("<I", 10**6)))


class ResearchWithoutDexScreener(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = Settings(_env_file=None, data_dir=self.tmp.name, dry_run=True, discord_enabled=False,
                          enable_pumpportal=False, log_json=False)
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.open()
        self.addCleanup(self.db.close)

    def research(self, dex, rpc, force=True):
        r = Researcher(self.s, self.db, Providers(dex=dex, rpc=rpc))
        return asyncio.run(r.research(UNSEEN_MINT, force=force))

    def assert_curve_research(self, out):
        self.assertEqual(out.report.get("lifecycle"), "PRE_MIGRATION_BONDING_CURVE", out.reasons)
        self.assertEqual(out.report["pool"]["bonding_curve"], DERIVED_CURVE)
        self.assertEqual(out.report["sources"]["pump_fun_curve_rpc"], "OK (derived from mint)")
        self.assertEqual(out.report["pre_migration"]["creator"], CREATOR)
        self.assertNotEqual(out.action, Action.AUTO_POST)

    def test_never_seen_token_with_no_dex_pair_is_researched_from_chain(self):
        out = self.research(T.FakeDex(), ChainRpc(txs_for(UNSEEN_MINT), curve_account=curve_with_creator()))
        self.assert_curve_research(out)
        self.assertEqual((out.report["identity"]["name"], out.report["identity"]["symbol"]), ("Banana Cat", "BCAT"))
        self.assertIn("top-10 7.0% / largest 0.7% of total supply", out.report["pre_migration"]["holder_risk"])
        self.assertEqual(out.report["pre_migration"]["trade_events"], 24)

    def test_dexscreener_down_does_not_block_curve_research(self):
        out = self.research(FailingDex(), ChainRpc(txs_for(UNSEEN_MINT), curve_account=curve_with_creator()))
        self.assert_curve_research(out)
        self.assertIn("FAILED", out.report["sources"]["dexscreener"])

    def test_dexscreener_disabled_does_not_block_curve_research(self):
        out = self.research(None, ChainRpc(txs_for(UNSEEN_MINT), curve_account=curve_with_creator()))
        self.assert_curve_research(out)

    def test_token2022_metadata_extension_used_for_identity(self):
        out = self.research(T.FakeDex(), ChainRpc(txs_for(UNSEEN_MINT), curve_account=curve_with_creator(),
                                                  token2022=True))
        self.assertEqual(out.report["identity"]["symbol"], "CHN")

    def test_quiet_token_skips_holder_rpc_unless_forced(self):
        rpc = ChainRpc([], curve_account=curve_with_creator())
        calls = []
        orig = rpc.get_token_largest_accounts

        async def spy(mint):
            calls.append(mint)
            return await orig(mint)
        rpc.get_token_largest_accounts = spy
        self.research(T.FakeDex(), rpc, force=False)
        self.assertEqual(calls, [])                       # automatic pass on a dead launch: no holder RPC
        self.research(T.FakeDex(), rpc, force=True)
        self.assertEqual(len(calls), 1)                   # manual /research always checks holders

    def test_non_pump_token_without_pool_stays_insufficient(self):
        out = self.research(T.FakeDex(), T.FakeRpc([], curve_account=None))
        self.assertEqual(out.action, Action.INSUFFICIENT_DATA)
        self.assertTrue(any("no usable pool" in r for r in out.reasons), out.reasons)

    def test_migrated_curve_is_not_treated_as_live_curve(self):
        raw = (bytes.fromhex("17b7f83760d8ac60") + struct.pack("<QQQQQ", 10**15, 45 * 10**9, 0, 0, 10**15)
               + b"\x01" + b58decode(CREATOR))
        acc = {"owner": T.PUMP_FUN_PROGRAM, "data": [base64.b64encode(raw).decode(), "base64"]}
        out = self.research(T.FakeDex(), ChainRpc([], curve_account=acc))
        self.assertNotEqual(out.action, Action.WATCHLIST)
        self.assertNotEqual(out.report.get("lifecycle"), "PRE_MIGRATION_BONDING_CURVE")
