"""Tests for Pump.fun bonding-curve trade decoding and the pre-migration research path.

EVERYTHING HERE IS SYNTHETIC. Transactions are built to the documented Pump.fun TradeEvent
format and the Solana jsonParsed transaction structure. Passing these tests shows the decoder
behaves as specified on that format; it is NOT live validation against real RPC data.

Written with unittest so it runs under plain `python -m unittest` and under pytest.
"""
from __future__ import annotations

import asyncio
import base64
import json
import struct
import tempfile
import time
import unittest
from pathlib import Path

from northstar.analysis import wallets as W
from northstar.analysis.decision import decide
from northstar.analysis.pump_trades import (EVENT_IX_TAG, PUMP_FUN_PROGRAM, TRADE_EVENT_DISCRIMINATOR, UNVERIFIED,
                                            VERIFIED, b58decode, b58encode, classify_pump_instructions,
                                            decode_curve_transaction, decode_trade_event)
from northstar.config import Settings
from northstar.db import Database
from northstar.models import Action, Confidence, DataStatus, GateResult, GateStatus, RiskLevel, Trade
from northstar.net import ProviderError
from northstar.research import Providers, Researcher

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SYSTEM = "11111111111111111111111111111111"
RAYDIUM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"


def addr(seed: str) -> str:
    return b58encode((seed.encode() * 32)[:32])


MINT = addr("pumpmint")
OTHER_MINT = addr("othermint")
CURVE = addr("bondingcurve")
CREATOR = addr("creatorwallet")
BUYER = addr("buyerwallet")
SELLER = addr("sellerwallet")
RAW_START = 5_000_000_000  # 5,000 tokens at 6 decimals
NOW = time.time()


# --------------------------------------------------------------------------- builders
def event_body(mint=MINT, sol=300_000_000, tok=1_000_000_000, is_buy=True, user=BUYER, ts=None, extra=True) -> bytes:
    body = (b58decode(mint) + struct.pack("<QQ", sol, tok) + bytes([1 if is_buy else 0]) + b58decode(user)
            + struct.pack("<q", int(ts if ts is not None else NOW)))
    if extra:  # reserves + a few trailing fields, as later program versions append
        body += struct.pack("<QQQQ", 30_000_000_000, 1_000_000_000_000_000, 1_000_000_000, 900_000_000_000_000)
        body += b"\x00" * 120
    return body


def ui(raw: int) -> dict:
    return {"amount": str(raw), "decimals": 6, "uiAmount": raw / 1e6, "uiAmountString": str(raw / 1e6)}


def balances(owner, mint, pre, post, idx=1):
    return ({"accountIndex": idx, "mint": mint, "owner": owner, "uiTokenAmount": ui(pre)},
            {"accountIndex": idx, "mint": mint, "owner": owner, "uiTokenAmount": ui(post)})


def pump_tx(user=BUYER, side="buy", tok=1_000_000_000, sol=300_000_000, mint=MINT, slot=100, block_time=None,
            events="inner", balances_ok=True, err=None, sig="sig-1", ix_name="buy", versioned=False,
            event_mint=None, event_user=None, ts=None, extra_pre=(), extra_post=(), top_data="default"):
    block_time = int(NOW - 10) if block_time == "default" or block_time is None else block_time
    is_buy = side == "buy"
    sign = 1 if is_buy else -1
    disc = {"buy": "66063d1201daebea", "sell": "33e685a4017f83ad", "buy_exact_sol_in": "38fc74089edfcd5f",
            "create": "181ec828051c0777", "mystery": "0102030405060708"}[ix_name]
    top_ix = {"programId": PUMP_FUN_PROGRAM, "accounts": [CURVE, user],
              "data": b58encode(bytes.fromhex(disc) + struct.pack("<QQ", tok, sol))}
    if top_data is None:
        top_ix.pop("data")
    body = event_body(event_mint or mint, sol, tok, is_buy, event_user or user, ts)
    cpi = {"programId": PUMP_FUN_PROGRAM, "accounts": [], "data": b58encode(EVENT_IX_TAG + TRADE_EVENT_DISCRIMINATOR + body)}
    inner = [{"index": 0, "instructions": [cpi]}] if events in ("inner", "both") else []
    logs = [f"Program {PUMP_FUN_PROGRAM} invoke [1]", "Program log: Instruction: Buy"]
    if events in ("log", "both"):
        logs.append("Program data: " + base64.b64encode(TRADE_EVENT_DISCRIMINATOR + body).decode())
    logs.append(f"Program {PUMP_FUN_PROGRAM} success")
    pre_b, post_b = [], []
    if balances_ok:
        a, b = balances(user, mint, RAW_START, RAW_START + sign * tok)
        c, d = balances(CURVE, mint, 900_000_000_000_000, 900_000_000_000_000 - sign * tok, idx=2)
        pre_b += [a, c]
        post_b += [b, d]
    pre_b += list(extra_pre)
    post_b += list(extra_post)
    keys = [{"pubkey": user, "signer": True, "source": "transaction"},
            {"pubkey": CURVE, "signer": False, "source": "transaction"}]
    tx = {"slot": slot, "blockTime": block_time,
          "meta": {"err": err, "fee": 5000, "preTokenBalances": pre_b, "postTokenBalances": post_b,
                   "preBalances": [10_000_000_000, 1, 1], "postBalances": [9_000_000_000, 1, 1],
                   "innerInstructions": inner, "logMessages": logs},
          "transaction": {"signatures": [sig], "message": {"accountKeys": keys, "instructions": [top_ix]}}}
    if versioned:
        tx["version"] = 0
        tx["meta"]["loadedAddresses"] = {"writable": [addr("altw")], "readonly": [addr("altr")]}
        keys.append({"pubkey": addr("altw"), "signer": False, "source": "lookupTable"})
        keys.append({"pubkey": addr("altr"), "signer": False, "source": "lookupTable"})
    return tx


# --------------------------------------------------------------------------- decoder unit tests
class DecoderTests(unittest.TestCase):
    def test_base58_roundtrip_and_leading_zeros(self):
        for raw in (b"\x00\x00abc", bytes(range(1, 33)), b"\x00" * 32):
            self.assertEqual(b58decode(b58encode(raw)), raw)

    def test_documented_constants(self):
        import hashlib
        self.assertEqual(hashlib.sha256(b"event:TradeEvent").digest()[:8], TRADE_EVENT_DISCRIMINATOR)
        self.assertEqual(hashlib.sha256(b"global:buy").digest()[:8].hex(), "66063d1201daebea")
        self.assertEqual(hashlib.sha256(b"global:sell").digest()[:8].hex(), "33e685a4017f83ad")

    def test_buy_decoded_from_inner_instruction_event(self):
        r = decode_curve_transaction(pump_tx(events="inner"), MINT)
        self.assertEqual(r.reason, "decoded")
        (t,) = r.trades
        self.assertEqual((t.side, t.wallet, t.verification), ("buy", BUYER, VERIFIED))
        self.assertAlmostEqual(t.token_amount, 1000.0)
        self.assertAlmostEqual(t.sol_amount, 0.3)  # taken from the event, not from wallet balance guesses
        self.assertEqual(t.signature, "sig-1")

    def test_sell_decoded_from_program_data_log(self):
        tx = pump_tx(user=SELLER, side="sell", events="log", ix_name="sell", sol=250_000_000)
        r = decode_curve_transaction(tx, MINT)
        (t,) = r.trades
        self.assertEqual((t.side, t.wallet), ("sell", SELLER))
        self.assertAlmostEqual(t.sol_amount, 0.25)

    def test_same_event_from_two_sources_counts_once(self):
        r = decode_curve_transaction(pump_tx(events="both"), MINT)
        self.assertEqual(len(r.trades), 1)

    def test_failed_transaction_is_not_a_trade(self):
        r = decode_curve_transaction(pump_tx(err={"InstructionError": [0, "Custom"]}), MINT)
        self.assertEqual((r.reason, r.trades), ("transaction_failed", []))

    def test_event_for_different_mint_is_ignored(self):
        tx = pump_tx(mint=OTHER_MINT)
        r = decode_curve_transaction(tx, MINT)
        self.assertEqual((r.reason, r.trades), ("event_other_mint", []))

    def test_unrelated_token_transfer_cannot_create_a_trade(self):
        # Large balance change for the researched mint, no Pump.fun event and no Pump.fun program.
        pre, post = balances(BUYER, MINT, RAW_START, RAW_START + 777_000_000)
        tx = {"slot": 5, "blockTime": int(NOW), "meta": {"err": None, "preTokenBalances": [pre],
              "postTokenBalances": [post], "logMessages": [f"Program {TOKEN_PROGRAM} invoke [1]",
                                                           f"Program {TOKEN_PROGRAM} success"]},
              "transaction": {"signatures": ["s"], "message": {"accountKeys": [{"pubkey": BUYER, "signer": True}],
                                                              "instructions": [{"programId": TOKEN_PROGRAM, "parsed":
                                                                                {"type": "transfer", "info": {}}}]}}}
        r = decode_curve_transaction(tx, MINT)
        self.assertEqual((r.reason, r.trades), ("no_pump_instruction", []))

    def test_extra_unrelated_balance_changes_do_not_block_a_real_trade(self):
        a, b = balances(addr("bystander"), MINT, 10, 99, idx=3)
        c, d = balances(addr("noise"), OTHER_MINT, 1, 2, idx=4)
        r = decode_curve_transaction(pump_tx(extra_pre=(a, c), extra_post=(b, d)), MINT)
        self.assertEqual(len(r.trades), 1)

    def test_trader_balance_must_match_event_exactly(self):
        tx = pump_tx()
        tx["meta"]["postTokenBalances"][0]["uiTokenAmount"] = ui(RAW_START + 1_000_000_001)
        self.assertEqual(decode_curve_transaction(tx, MINT).reason, "event_balance_mismatch")

    def test_wrong_direction_is_rejected(self):
        tx = pump_tx(side="buy")
        pre, post = balances(BUYER, MINT, RAW_START, RAW_START - 1_000_000_000)  # balances show a SELL
        tx["meta"]["preTokenBalances"][0], tx["meta"]["postTokenBalances"][0] = pre, post
        self.assertEqual(decode_curve_transaction(tx, MINT).reason, "event_balance_mismatch")

    def test_missing_token_balances_reject(self):
        self.assertEqual(decode_curve_transaction(pump_tx(balances_ok=False), MINT).reason, "event_balance_missing")

    def test_trader_not_matching_any_balance_owner_is_rejected(self):
        # Event names a user whose balance entry does not exist: ambiguous/unknown trader.
        tx = pump_tx(event_user=addr("someoneelse"))
        self.assertEqual(decode_curve_transaction(tx, MINT).reason, "event_balance_missing")

    def test_versioned_transaction_with_lookup_table_decodes(self):
        r = decode_curve_transaction(pump_tx(versioned=True), MINT)
        self.assertEqual(len(r.trades), 1)

    def test_lookup_table_keys_are_not_duplicated(self):
        tx = pump_tx(versioned=True)
        keys = W._keys(tx)
        pubs = [k["pubkey"] for k in keys]
        self.assertEqual(len(pubs), len(set(pubs)))
        self.assertEqual(len(pubs), 4)
        # Raw-encoding style response (no lookup-table keys in accountKeys) still gets them appended.
        raw = pump_tx(versioned=True)
        raw["transaction"]["message"]["accountKeys"] = raw["transaction"]["message"]["accountKeys"][:2]
        self.assertEqual(len(W._keys(raw)), 4)

    def test_trade_instruction_without_event_is_unsupported_not_a_trade(self):
        r = decode_curve_transaction(pump_tx(events="none"), MINT)
        self.assertEqual((r.reason, r.trades), ("trade_instruction_without_event", []))

    def test_unknown_instruction_discriminator(self):
        r = decode_curve_transaction(pump_tx(events="none", ix_name="mystery"), MINT)
        self.assertEqual((r.reason, r.trades), ("unknown_pump_instruction", []))

    def test_non_trade_pump_instruction(self):
        r = decode_curve_transaction(pump_tx(events="none", ix_name="create"), MINT)
        self.assertEqual((r.reason, r.trades), ("non_trade_pump_instruction", []))

    def test_missing_instruction_data(self):
        tx = pump_tx(events="none", top_data=None)
        self.assertEqual(classify_pump_instructions(tx), {"missing_data": 1})
        self.assertEqual(decode_curve_transaction(tx, MINT).trades, [])

    def test_truncated_and_malformed_events(self):
        tx = pump_tx(events="none")
        bad = event_body()[:40]
        tx["meta"]["innerInstructions"] = [{"index": 0, "instructions": [
            {"programId": PUMP_FUN_PROGRAM, "data": b58encode(EVENT_IX_TAG + TRADE_EVENT_DISCRIMINATOR + bad)}]}]
        self.assertEqual(decode_curve_transaction(tx, MINT).reason, "event_decode_error")
        with self.assertRaises(ValueError):
            decode_trade_event(event_body()[:88])
        body = bytearray(event_body())
        body[48] = 7  # is_buy must be 0/1
        with self.assertRaises(ValueError):
            decode_trade_event(bytes(body))

    def test_event_logged_by_another_program_is_ignored(self):
        # Same discriminator, but emitted while a different program was executing.
        tx = pump_tx(events="none")
        payload = base64.b64encode(TRADE_EVENT_DISCRIMINATOR + event_body()).decode()
        tx["meta"]["logMessages"] = [f"Program {RAYDIUM} invoke [1]", "Program data: " + payload,
                                      f"Program {RAYDIUM} success"]
        self.assertEqual(decode_curve_transaction(tx, MINT).trades, [])

    def test_event_cpi_from_another_program_is_ignored(self):
        tx = pump_tx(events="none")
        tx["meta"]["innerInstructions"] = [{"index": 0, "instructions": [
            {"programId": RAYDIUM, "data": b58encode(EVENT_IX_TAG + TRADE_EVENT_DISCRIMINATOR + event_body())}]}]
        self.assertEqual(decode_curve_transaction(tx, MINT).trades, [])

    def test_log_event_inside_nested_invoke_requires_pump_on_top(self):
        tx = pump_tx(events="none")
        payload = base64.b64encode(TRADE_EVENT_DISCRIMINATOR + event_body()).decode()
        tx["meta"]["logMessages"] = [f"Program {PUMP_FUN_PROGRAM} invoke [1]", f"Program {SYSTEM} invoke [2]",
                                      "Program data: " + payload, f"Program {SYSTEM} success",
                                      f"Program {PUMP_FUN_PROGRAM} success"]
        self.assertEqual(decode_curve_transaction(tx, MINT).trades, [])

    def test_missing_block_time_falls_back_to_event_timestamp_then_none(self):
        tx = pump_tx(ts=int(NOW) - 7)
        tx["blockTime"] = None
        r = decode_curve_transaction(tx, MINT)
        self.assertEqual(r.trades[0].block_time, int(NOW) - 7)
        self.assertIn("block_time_from_event", r.notes)
        tx2 = pump_tx(ts=0)
        tx2["blockTime"] = None
        self.assertIsNone(decode_curve_transaction(tx2, MINT).trades[0].block_time)

    def test_invalid_inputs_never_raise(self):
        for bad in (None, {}, {"meta": None}, {"meta": {}}, "x", 5):
            self.assertIn(decode_curve_transaction(bad, MINT).reason,
                          ("invalid_transaction", "no_pump_instruction"))

    def test_post_migration_amm_transaction_is_not_decoded_as_a_curve_trade(self):
        pre, post = balances(BUYER, MINT, RAW_START, RAW_START + 123_000_000)
        tx = {"slot": 9, "blockTime": int(NOW), "meta": {"err": None, "preTokenBalances": [pre],
              "postTokenBalances": [post], "preBalances": [10**10, 0], "postBalances": [9 * 10**9, 0],
              "fee": 5000, "logMessages": []},
              "transaction": {"signatures": ["amm"], "message": {
                  "accountKeys": [{"pubkey": BUYER, "signer": True}, {"pubkey": CURVE, "signer": False}],
                  "instructions": [{"programId": RAYDIUM, "program": "raydium", "parsed": {"type": "swap"}}]}}}
        self.assertEqual(decode_curve_transaction(tx, MINT).reason, "no_pump_instruction")
        # The legacy AMM parser still yields a candidate, but it is explicitly UNVERIFIED and the
        # statistics built from it are INFERRED, never VERIFIED.
        cand = W.parse_trade(tx, MINT, {CURVE}, "amm")
        self.assertIsNotNone(cand)
        self.assertEqual(cand.verification, UNVERIFIED)
        ts = W.compute_trade_stats([cand], None, None, 1)
        self.assertEqual(ts.status, DataStatus.INFERRED)
        self.assertNotEqual(ts.status, DataStatus.VERIFIED)


# --------------------------------------------------------------------------- sampler tests
class FakeRpc:
    def __init__(self, txs=None, sigs=None, curve_account=None, fail_sigs=False, fail_tx=()):
        self.txs = {t["transaction"]["signatures"][0]: t for t in (txs or [])}
        self.sigs = sigs
        self.curve_account = curve_account
        self.fail_sigs = fail_sigs
        self.fail_tx = set(fail_tx)
        self.tx_calls = []
        self.sig_calls = 0

    async def get_signatures(self, address, limit=100, before=None):
        self.sig_calls += 1
        if self.fail_sigs:
            raise ProviderError("fake", "signatures unavailable")
        if self.sigs is not None:
            return self.sigs[:limit]
        rows = sorted(self.txs.values(), key=lambda t: -t["blockTime"])
        return [{"signature": t["transaction"]["signatures"][0], "slot": t["slot"], "err": t["meta"]["err"],
                 "blockTime": t["blockTime"]} for t in rows][:limit]

    async def get_transaction(self, sig):
        self.tx_calls.append(sig)
        if sig in self.fail_tx:
            raise ProviderError("fake", "boom")
        return self.txs.get(sig)

    holders = None  # list of (owner, pct); default: curve vault + a healthy spread

    async def get_token_largest_accounts(self, mint):
        from conftest import largest_and_owners
        rows = self.holders or ([(CURVE, 80.0)] + [(addr(f"holder{i}"), 1.0) for i in range(19)])
        self._largest, self._owners = largest_and_owners(rows)
        return self._largest

    async def get_multiple_accounts(self, addrs):
        return self._owners[: len(addrs)]

    async def get_account_info(self, address):
        if address == CURVE:
            return self.curve_account
        return {"owner": TOKEN_PROGRAM, "data": {"program": "spl-token", "parsed": {"type": "mint", "info": {
            "decimals": 6, "supply": "1000000000000000", "isInitialized": True,
            "mintAuthority": None, "freezeAuthority": None}}}}


def run(coro):
    return asyncio.run(coro)


class SamplerTests(unittest.TestCase):
    def test_duplicate_signatures_fetched_and_counted_once(self):
        tx = pump_tx(sig="dup", block_time=int(NOW) - 5)
        sigs = [{"signature": "dup", "blockTime": int(NOW) - 5, "err": None, "slot": 1}] * 3
        rpc = FakeRpc([tx], sigs=sigs)
        out = run(W.sample_curve_trades(rpc, CURVE, MINT, 50, min_block_time=NOW - 300))
        self.assertEqual(len(out.trades), 1)
        self.assertEqual(rpc.tx_calls, ["dup"])
        self.assertEqual(out.counters["signature_duplicate"], 2)

    def test_old_signatures_are_not_fetched(self):
        txs = [pump_tx(sig=f"old{i}", block_time=int(NOW) - 3600 - i, user=addr(f"u{i}")) for i in range(30)]
        rpc = FakeRpc(txs)
        out = run(W.sample_curve_trades(rpc, CURVE, MINT, 100, min_block_time=NOW - 300))
        self.assertEqual(rpc.tx_calls, [])
        self.assertEqual(out.counters["outside_window"], 30)
        self.assertTrue(out.window_covered)
        self.assertEqual(out.trades, [])

    def test_full_page_inside_window_is_reported_as_lower_bound(self):
        txs = [pump_tx(sig=f"n{i}", block_time=int(NOW) - 2, user=addr(f"u{i}")) for i in range(20)]
        out = run(W.sample_curve_trades(FakeRpc(txs), CURVE, MINT, 20, min_block_time=NOW - 300))
        self.assertFalse(out.window_covered)
        self.assertIn("lower bound", out.describe())

    def test_failed_signatures_and_fetch_failures_are_counted(self):
        good = pump_tx(sig="good", block_time=int(NOW) - 5)
        failed = pump_tx(sig="failed", block_time=int(NOW) - 6, err={"InstructionError": [0, "x"]})
        flaky = pump_tx(sig="flaky", block_time=int(NOW) - 7, user=addr("flaky"))
        rpc = FakeRpc([good, failed, flaky], fail_tx={"flaky"})
        out = run(W.sample_curve_trades(rpc, CURVE, MINT, 50, min_block_time=NOW - 300))
        self.assertEqual(len(out.trades), 1)
        self.assertEqual(out.counters["signature_failed"], 1)
        self.assertEqual(out.counters["fetch_failed"], 1)
        self.assertNotIn("failed", rpc.tx_calls)

    def test_signature_failure_is_reported_not_raised(self):
        out = run(W.sample_curve_trades(FakeRpc(fail_sigs=True), CURVE, MINT, 50))
        self.assertEqual(out.trades, [])
        self.assertIn("signatures unavailable", out.error)

    def test_undecodable_transactions_never_become_trades(self):
        txs = [pump_tx(sig=f"x{i}", block_time=int(NOW) - 3, events="none", user=addr(f"x{i}")) for i in range(12)]
        out = run(W.sample_curve_trades(FakeRpc(txs), CURVE, MINT, 50, min_block_time=NOW - 300))
        self.assertEqual(out.trades, [])
        self.assertEqual(out.counters["trade_instruction_without_event"], 12)
        self.assertEqual(out.undecoded(), 12)


# --------------------------------------------------------------------------- research integration
def curve_account(v_sol=45_000_000_000, complete=False) -> dict:
    raw = (bytes.fromhex("17b7f83760d8ac60") + struct.pack("<QQQQQ", 1_000_000_000_000_000, v_sol,
           800_000_000_000_000, 10_000_000_000, 1_000_000_000_000_000) + bytes([1 if complete else 0]) + b"\x00" * 32)
    return {"owner": PUMP_FUN_PROGRAM, "data": [base64.b64encode(raw).decode(), "base64"]}


class FakeDex:
    async def token_pairs(self, mint):
        return []

    async def trending_metas(self):
        raise ProviderError("dex", "n/a")


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = Settings(_env_file=None, data_dir=self.tmp.name, dry_run=True, discord_enabled=False,
                          enable_pumpportal=False, log_json=False)
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.open()
        self.addCleanup(self.db.close)

    def seed_launch(self, launch_cap=30.0):
        self.db.upsert_discovery(MINT, "pumpportal_new", 5.0, name="Velum", symbol="VELUM", payload={
            "mint": MINT, "txType": "create", "bondingCurveKey": CURVE, "marketCapSol": launch_cap,
            "traderPublicKey": CREATOR})

    def research(self, rpc):
        r = Researcher(self.s, self.db, Providers(dex=FakeDex(), rpc=rpc))
        cand = {"sources": ["pumpportal_new"], "symbol": "VELUM", "name": "Velum", "state": "DISCOVERED"}
        return run(r.research(MINT, cand=cand, force=True))

    def active_txs(self, buys=18, sells=6, buyers=12):
        txs = []
        for i in range(buys):
            txs.append(pump_tx(sig=f"b{i}", user=addr(f"buyer{i % buyers}"), side="buy",
                               block_time=int(time.time()) - 2 - i, sol=300_000_000))
        for i in range(sells):
            txs.append(pump_tx(sig=f"s{i}", user=addr(f"seller{i}"), side="sell", ix_name="sell",
                               block_time=int(time.time()) - 40 - i, sol=200_000_000))
        return txs

    def test_verified_activity_can_reach_watchlist_but_never_auto_post(self):
        self.seed_launch(30.0)  # curve account below gives 45 SOL: +50%
        out = self.research(FakeRpc(self.active_txs(), curve_account=curve_account()))
        pm = out.report["pre_migration"]
        self.assertEqual(out.action, Action.WATCHLIST)
        self.assertNotEqual(out.action, Action.AUTO_POST)
        self.assertEqual(out.risk, RiskLevel.INCOMPLETE)
        self.assertEqual(pm["source"], "Solana RPC verified Pump.fun trades")
        self.assertEqual((pm["trade_events"], pm["buys"], pm["sells"]), (24, 18, 6))
        self.assertAlmostEqual(pm["buy_sol_5m"], 5.4, places=3)
        self.assertEqual(out.report["pre_migration_rpc_diagnostics"]["counters"]["decoded"], 24)

    def test_extended_move_is_not_rewarded_or_qualified(self):
        self.seed_launch(4.0)  # 45 SOL vs 4 SOL launch = +1025%
        out = self.research(FakeRpc(self.active_txs(), curve_account=curve_account()))
        pm = out.report["pre_migration"]
        self.assertEqual(out.action, Action.REJECT)
        self.assertTrue(pm["extended_move"])
        self.assertEqual(pm["score_components"]["market_cap_momentum"], 0.0)
        self.assertTrue(any("late" in r for r in out.reasons))

    def test_reject_names_the_failed_threshold(self):
        self.seed_launch(30.0)
        # plenty of activity, but sells dominate: ratio 18/30 = 0.6 < 1.35
        out = self.research(FakeRpc(self.active_txs(buys=18, sells=30), curve_account=curve_account()))
        self.assertEqual(out.action, Action.REJECT)
        self.assertTrue(any("buy/sell ratio 0.60 < 1.35" in r for r in out.reasons), out.reasons)

    def test_momentum_is_unchanged_below_100_pct_and_tapers_above(self):
        scores = {}
        for launch, label in ((45 / 1.5, "+50%"), (45 / 3.0, "+200%")):
            self.db.execute("DELETE FROM discovery_events")
            self.seed_launch(launch)
            out = self.research(FakeRpc(self.active_txs(), curve_account=curve_account()))
            scores[label] = out.report["pre_migration"]["score_components"]["market_cap_momentum"]
        self.assertEqual(scores["+50%"], 25.0)
        self.assertLess(scores["+200%"], 25.0)
        self.assertGreater(scores["+200%"], 0.0)

    def test_undecodable_rpc_data_is_insufficient_and_explained(self):
        self.seed_launch(30.0)
        txs = [pump_tx(sig=f"u{i}", events="none", user=addr(f"u{i}"), block_time=int(time.time()) - 3)
               for i in range(40)]
        out = self.research(FakeRpc(txs, curve_account=curve_account()))
        self.assertEqual(out.action, Action.INSUFFICIENT_DATA)
        self.assertEqual(out.report["pre_migration"]["trade_events"], 0)
        diag = out.report["pre_migration_rpc_diagnostics"]
        self.assertEqual(diag["counters"]["trade_instruction_without_event"], 40)
        self.assertIn("could not be verified", out.reasons[0])
        self.assertEqual(out.report["pre_migration"]["data_status"], "UNAVAILABLE")

    def test_balance_only_candidates_cannot_pass_as_trades(self):
        self.seed_launch(30.0)
        txs = []
        for i in range(30):  # old-style AMM-like candidates: balance deltas only, no Pump.fun event
            pre, post = balances(addr(f"w{i}"), MINT, RAW_START, RAW_START + 10**9)
            txs.append({"slot": i, "blockTime": int(time.time()) - 5, "meta": {
                "err": None, "preTokenBalances": [pre], "postTokenBalances": [post], "logMessages": [],
                "preBalances": [10**10], "postBalances": [9 * 10**9], "fee": 5000},
                "transaction": {"signatures": [f"c{i}"], "message": {
                    "accountKeys": [{"pubkey": addr(f"w{i}"), "signer": True}, {"pubkey": CURVE, "signer": False}],
                    "instructions": []}}})
        out = self.research(FakeRpc(txs, curve_account=curve_account()))
        self.assertEqual(out.action, Action.INSUFFICIENT_DATA)
        self.assertEqual(out.report["pre_migration"]["trade_events"], 0)

    def test_quiet_token_costs_no_transaction_fetches_and_is_honest(self):
        self.seed_launch(30.0)
        txs = [pump_tx(sig=f"q{i}", user=addr(f"q{i}"), block_time=int(time.time()) - 1800) for i in range(6)]
        rpc = FakeRpc(txs, curve_account=curve_account())
        out = self.research(rpc)
        self.assertEqual(rpc.tx_calls, [])
        self.assertEqual(out.action, Action.INSUFFICIENT_DATA)
        self.assertTrue(out.report["pre_migration_rpc_diagnostics"]["window_covered"])
        self.assertIn("window fully covered", out.reasons[0])

    def test_stream_rows_and_rpc_rows_are_deduplicated_by_signature(self):
        self.seed_launch(30.0)
        txs = self.active_txs()
        for t in txs[:8]:  # the same transactions were also stored from the PumpPortal stream
            sig = t["transaction"]["signatures"][0]
            self.db.record_curve_snapshot(MINT, {"txType": "buy", "signature": sig, "traderPublicKey": BUYER,
                                                 "solAmount": 0.3, "tokenAmount": 1000, "marketCapSol": 40.0})
        out = self.research(FakeRpc(txs, curve_account=curve_account()))
        self.assertEqual(out.report["pre_migration"]["trade_events"], 24)
        self.assertEqual(out.report["pre_migration"]["source"],
                         "PumpPortal stream + Solana RPC verified Pump.fun trades")

    def test_rpc_failure_is_insufficient_not_clean(self):
        self.seed_launch(30.0)
        out = self.research(FakeRpc(fail_sigs=True, curve_account=curve_account()))
        self.assertEqual(out.action, Action.INSUFFICIENT_DATA)
        self.assertIn("RPC sampling failed", out.reasons[0])

    def test_research_rejects_unverified_trades_even_if_the_sampler_returns_them(self):
        # Defence in depth: a future sampler bug must not let balance-delta guesses count.
        self.seed_launch(30.0)
        t = int(time.time()) - 5
        guesses = [Trade(f"g{i}", i, t, addr(f"g{i}"), "buy", 100.0, 0.5) for i in range(30)]  # default = UNVERIFIED
        self.assertTrue(all(g.verification == UNVERIFIED for g in guesses))

        async def fake_sampler(*a, **k):
            return W.CurveSample(trades=guesses, signatures_examined=30, signatures_in_window=30, window_covered=True)

        original = W.sample_curve_trades
        W.sample_curve_trades = fake_sampler
        try:
            out = self.research(FakeRpc([], curve_account=curve_account()))
        finally:
            W.sample_curve_trades = original
        self.assertEqual(out.action, Action.INSUFFICIENT_DATA)
        self.assertEqual(out.report["pre_migration"]["trade_events"], 0)

    def test_incomplete_evidence_cannot_bypass_strict_callout_gates(self):
        # Perfect score and confidence cannot override INCOMPLETE risk or an UNKNOWN critical gate.
        d = decide(100.0, RiskLevel.INCOMPLETE, Confidence.HIGH, [], 25.0, 5, False, self.s)
        self.assertNotEqual(d.action, Action.AUTO_POST)
        unknown = [GateResult("lp_status", GateStatus.UNKNOWN, "not verified")]
        d = decide(100.0, RiskLevel.LOW_DETECTED, Confidence.HIGH, unknown, 25.0, 5, False, self.s)
        self.assertEqual(d.action, Action.INSUFFICIENT_DATA)


if __name__ == "__main__":
    unittest.main()
