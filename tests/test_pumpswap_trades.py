"""PumpSwap swap verification (post-migration pools).

EVERYTHING HERE IS SYNTHETIC: transactions follow the documented PumpSwap BuyEvent/SellEvent
layout and the Solana jsonParsed structure. Passing tests show the decoder behaves as specified
on that format; they are NOT live validation against real RPC data.
"""
from __future__ import annotations

import base64
import copy

from conftest import MINT, POOL, addr, healthy_trades, seed_profile, strong_providers, swap_tx
from northstar.analysis import wallets as W
from northstar.analysis.pump_trades import EVENT_IX_TAG
from northstar.analysis.pumpswap_trades import (BUY_EVENT_DISCRIMINATOR, PUMPSWAP_PROGRAM,
                                                VERIFIED_PUMPSWAP, decode_pumpswap_transaction)
from northstar.models import Action, DataStatus
from northstar.research import Researcher

import base58

WALLET = addr("swapper")
T0 = 1_790_000_000


async def run(settings, db, providers):
    seed_profile(db)
    return await Researcher(settings, db, providers).research(MINT)


def _event_data(tx: dict) -> bytes:
    ix = tx["meta"]["innerInstructions"][0]["instructions"][0]
    return base58.b58decode(ix["data"])


def test_buy_and_sell_verified():
    for side in ("buy", "sell"):
        tx = swap_tx(WALLET, side, 1234.5, 10, T0)
        r = decode_pumpswap_transaction(tx, MINT, POOL)
        assert r.reason == "decoded", r
        (t,) = r.trades
        assert t.side == side and t.wallet == WALLET and t.verification == VERIFIED_PUMPSWAP
        assert abs(t.token_amount - 1234.5) < 1e-9 and abs(t.sol_amount - 0.5) < 1e-12


def test_event_from_log_line_while_pumpswap_executes():
    tx = swap_tx(WALLET, "buy", 100, 10, T0)
    payload = _event_data(tx)[len(EVENT_IX_TAG):]
    tx["meta"]["innerInstructions"] = []
    tx["meta"]["logMessages"] = [f"Program {PUMPSWAP_PROGRAM} invoke [1]",
                                 "Program data: " + base64.b64encode(payload).decode(),
                                 f"Program {PUMPSWAP_PROGRAM} success"]
    assert decode_pumpswap_transaction(tx, MINT, POOL).reason == "decoded"


def test_same_event_in_cpi_and_log_counted_once():
    tx = swap_tx(WALLET, "buy", 100, 10, T0)
    payload = _event_data(tx)[len(EVENT_IX_TAG):]
    tx["meta"]["logMessages"] = [f"Program {PUMPSWAP_PROGRAM} invoke [1]",
                                 "Program data: " + base64.b64encode(payload).decode(),
                                 f"Program {PUMPSWAP_PROGRAM} success"]
    assert len(decode_pumpswap_transaction(tx, MINT, POOL).trades) == 1


def test_spoofed_event_from_other_program_ignored():
    tx = swap_tx(WALLET, "buy", 100, 10, T0)
    tx["meta"]["innerInstructions"][0]["instructions"][0]["programId"] = addr("evilprogram")
    assert decode_pumpswap_transaction(tx, MINT, POOL).reason == "pumpswap_without_swap_event"
    # spoofed log line while another program executes
    payload = _event_data(swap_tx(WALLET, "buy", 100, 10, T0))[len(EVENT_IX_TAG):]
    tx["meta"]["logMessages"] = [f"Program {PUMPSWAP_PROGRAM} invoke [1]",
                                 f"Program {addr('evil')} invoke [2]",
                                 "Program data: " + base64.b64encode(payload).decode(),
                                 f"Program {addr('evil')} success",
                                 f"Program {PUMPSWAP_PROGRAM} success"]
    assert decode_pumpswap_transaction(tx, MINT, POOL).trades == []


def test_other_pool_not_counted():
    tx = swap_tx(WALLET, "buy", 100, 10, T0)
    assert decode_pumpswap_transaction(tx, MINT, addr("otherpool")).reason == "event_other_pool"


def test_balance_mismatch_and_missing_rejected():
    tx = swap_tx(WALLET, "buy", 100, 10, T0)
    tx["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"] = str(
        int(tx["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"]) + 1)
    assert decode_pumpswap_transaction(tx, MINT, POOL).reason == "event_balance_mismatch"
    tx = swap_tx(WALLET, "buy", 100, 10, T0)
    for side in ("preTokenBalances", "postTokenBalances"):
        tx["meta"][side] = [b for b in tx["meta"][side] if b["owner"] != WALLET]
    assert decode_pumpswap_transaction(tx, MINT, POOL).reason == "event_balance_missing"


def test_wrong_direction_rejected():
    tx = swap_tx(WALLET, "buy", 100, 10, T0)
    sell = swap_tx(WALLET, "sell", 100, 10, T0)
    tx["meta"]["preTokenBalances"], tx["meta"]["postTokenBalances"] = (
        sell["meta"]["preTokenBalances"], sell["meta"]["postTokenBalances"])
    assert decode_pumpswap_transaction(tx, MINT, POOL).trades == []


def test_failed_tx_and_garbage_inputs():
    tx = swap_tx(WALLET, "buy", 100, 10, T0)
    tx["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    assert decode_pumpswap_transaction(tx, MINT, POOL).reason == "transaction_failed"
    assert decode_pumpswap_transaction(None, MINT, POOL).reason == "invalid_transaction"
    assert decode_pumpswap_transaction({"meta": None}, MINT, POOL).reason == "invalid_transaction"
    no_ps = swap_tx(WALLET, "buy", 100, 10, T0, pumpswap_event=False)
    assert decode_pumpswap_transaction(no_ps, MINT, POOL).reason == "no_pumpswap_instruction"


def test_truncated_event_is_decode_error():
    tx = swap_tx(WALLET, "buy", 100, 10, T0)
    data = _event_data(tx)[:60]
    tx["meta"]["innerInstructions"][0]["instructions"][0]["data"] = base58.b58encode(data).decode()
    assert decode_pumpswap_transaction(tx, MINT, POOL).reason == "event_decode_error"


def test_missing_block_time_falls_back_to_event_timestamp():
    tx = swap_tx(WALLET, "buy", 100, 10, T0)
    tx["blockTime"] = None
    r = decode_pumpswap_transaction(tx, MINT, POOL)
    assert r.trades[0].block_time == T0 and "block_time_from_event" in r.notes


def test_verified_stats_refuse_unverified_trades():
    tx = swap_tx(WALLET, "buy", 100, 10, T0, pumpswap_event=False)
    cand = W.parse_trade(tx, MINT, {POOL})
    assert cand is not None and cand.verification not in W.VERIFIED_LABELS
    try:
        W.compute_trade_stats([cand], None, 1e9, 1, status=DataStatus.VERIFIED)
    except ValueError:
        pass
    else:
        raise AssertionError("VERIFIED stats must reject unverified candidates")


async def test_research_without_events_stays_inferred_and_never_auto_posts(settings, db):
    p = strong_providers()
    p.rpc.trades = [swap_tx(t["transaction"]["message"]["accountKeys"][0]["pubkey"],
                            "buy" if i % 4 != 3 else "sell", 1000 + i, 1000 + i * 3, T0 + i * 25,
                            pumpswap_event=False)
                    for i, t in enumerate(healthy_trades())]
    out = await run(settings, db, p)
    assert out.report["trades"]["status"] == "INFERRED"
    assert out.action != Action.AUTO_POST


async def test_research_with_verified_swaps_is_verified(settings, db):
    out = await run(settings, db, strong_providers())
    t = out.report["trades"]
    assert t["status"] == "VERIFIED" and "verified from PumpSwap" in t["note"]


async def test_too_few_verified_swaps_stays_inferred(settings, db):
    p = strong_providers()
    trades = healthy_trades()
    p.rpc.trades = [copy.deepcopy(t) for t in trades[:10]] + [
        swap_tx(addr(f"plain{i}"), "buy", 500 + i, 5000 + i, T0 + i, pumpswap_event=False) for i in range(110)]
    out = await run(settings, db, p)
    assert out.report["trades"]["status"] == "INFERRED"
    assert "below the 15 required" in out.report["trades"]["note"]
    assert out.action != Action.AUTO_POST
