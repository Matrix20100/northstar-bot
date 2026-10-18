"""Tests for the early-watch performance tracker.

EVERYTHING HERE IS SYNTHETIC (fake RPC, fake publisher, throwaway SQLite). It checks the
tracker's logic against the documented Pump.fun curve-account layout; it is NOT live validation.
"""
from __future__ import annotations

import asyncio
import json
import base64
import struct
import tempfile
import time
import unittest
from pathlib import Path

from northstar.analysis.pump_trades import PUMP_FUN_PROGRAM, b58encode
from northstar.config import Settings
from northstar.db import Database
from northstar.discord_bot.publisher import PublishError
from northstar.early_tracker import EarlyWatchTracker
from northstar.net import ProviderError

MINT = b58encode((b"earlymint" * 32)[:32])
CURVE = b58encode((b"earlycurve" * 32)[:32])
SUPPLY = 1_000_000_000_000_000


def curve_account(cap_sol: float, complete: bool = False) -> dict:
    raw = bytearray(8 + 40 + 1 + 40)
    raw[:8] = bytes.fromhex("17b7f83760d8ac60")
    struct.pack_into("<QQQQQ", raw, 8, SUPPLY, int(cap_sol * 1e9), 1, 1, SUPPLY)
    raw[48] = 1 if complete else 0
    return {"owner": PUMP_FUN_PROGRAM, "data": [base64.b64encode(bytes(raw)).decode(), "base64"]}


class FakeRpc:
    def __init__(self):
        self.cap, self.complete, self.mode, self.calls = 100.0, False, "ok", 0

    async def get_multiple_accounts(self, addrs):
        self.calls += 1
        if self.mode == "error":
            raise ProviderError("solana_rpc", "rpc down")
        if self.mode == "none":
            return [None for _ in addrs]
        return [curve_account(self.cap, self.complete) for _ in addrs]


class FakePub:
    def __init__(self):
        self.edits, self.fail, self.posts = [], None, []

    async def publish(self, embed, channel_id=None):
        self.posts.append((channel_id, embed))
        return channel_id, 9000 + len(self.posts)

    async def edit(self, channel_id, message_id, embed):
        if self.fail:
            raise self.fail
        self.edits.append((channel_id, message_id, embed))


def text(embed) -> str:
    return (embed.get("description") or "") + " " + " ".join(
        f"{f['name']} {f['value']}" for f in embed.get("fields", []))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Settings(_env_file=None, data_dir=self.tmp.name, dry_run=True, discord_enabled=False,
                          enable_pumpportal=False, log_json=False)
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.open()
        self.rpc, self.pub = FakeRpc(), FakePub()
        self.tr = EarlyWatchTracker(self.s, self.db, self.pub, self.rpc)
        self.db.upsert_discovery(MINT, "pumpportal_new", 1.0, "Early", "ERLY", {"bondingCurveKey": CURVE})
        report = {"mint": MINT, "identity": {"symbol": "ERLY", "name": "Early"},
                  "pre_migration": {"opportunity_score": 88, "market_cap_sol_current": 100.0,
                                    "trade_events": 15, "buys": 13, "sells": 2, "unique_buyers": 11,
                                    "buy_sol_5m": 3.0, "buy_sell_count_ratio": 6.5,
                                    "qualification_reasons": ["x"]}}
        self.db.create_or_update_early_signal(MINT, 100.0, 88.0, report)
        self.db.update_early_signal_status(MINT, "SENT", 11, 22)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def run_once(self, now=None):
        return asyncio.run(self.tr.run_once(now))

    def row(self):
        return self.db.fetchone("SELECT * FROM early_signals WHERE mint=?", (MINT,))


class TrackerTests(Base):
    def test_rise_updates_row_and_edits_message(self):
        self.rpc.cap = 250.0
        c = self.run_once()
        r = self.row()
        self.assertEqual((c["read"], c["edited"]), (1, 1))
        self.assertAlmostEqual(r["entry_market_cap_sol"], 100.0)   # baseline never moves
        self.assertAlmostEqual(r["current_market_cap_sol"], 250.0, places=3)
        self.assertAlmostEqual(r["peak_multiple"], 2.5, places=3)
        ch, msg, embed = self.pub.edits[0]
        self.assertEqual((ch, msg), (11, 22))
        t = text(embed)
        self.assertIn("2.50x", t)
        self.assertIn("250.00 SOL", t)
        self.assertIn("verified", t)

    def test_drop_keeps_peak_and_records_low(self):
        self.rpc.cap = 300.0
        self.run_once()
        self.rpc.cap = 50.0
        self.run_once()
        r = self.row()
        self.assertAlmostEqual(r["peak_multiple"], 3.0, places=3)
        self.assertAlmostEqual(r["min_multiple"], 0.5, places=3)
        self.assertIn("0.50x", text(self.pub.edits[-1][2]))

    def test_tiny_move_does_not_edit_again(self):
        self.rpc.cap = 200.0
        self.run_once()
        self.rpc.cap = 200.2   # 0.1%
        c = self.run_once()
        self.assertEqual(c["edited"], 0)
        self.assertEqual(len(self.pub.edits), 1)

    def test_rpc_error_never_writes_zero_and_goes_stale_after_three(self):
        self.rpc.cap = 150.0
        self.run_once()
        self.rpc.mode = "error"
        for _ in range(2):
            self.run_once()
        self.assertEqual(len(self.pub.edits), 1)           # no edit yet
        self.run_once()                                    # third consecutive failure
        self.assertEqual(len(self.pub.edits), 2)
        t = text(self.pub.edits[-1][2])
        self.assertIn("read failed", t)
        self.assertIn("150.00 SOL", t)                      # last valid value retained
        self.assertAlmostEqual(self.row()["current_market_cap_sol"], 150.0, places=3)

    def test_failure_counter_resets_after_success(self):
        self.rpc.mode = "none"
        self.run_once(); self.run_once()
        self.rpc.mode, self.rpc.cap = "ok", 130.0
        self.run_once()
        self.rpc.mode = "none"
        self.run_once(); self.run_once()
        self.assertFalse(any("read failed" in text(e[2]) for e in self.pub.edits))

    def test_unparseable_account_is_not_a_value(self):
        self.rpc.mode = "none"
        c = self.run_once()
        self.assertEqual((c["read"], c["failed_reads"], c["edited"]), (0, 1, 0))
        self.assertAlmostEqual(self.row()["current_market_cap_sol"], 100.0)

    def test_migration_edits_once_and_stops(self):
        self.rpc.cap, self.rpc.complete = 400.0, True
        c = self.run_once()
        self.assertEqual(c["migrated"], 1)
        self.assertEqual(self.row()["status"], "MIGRATED")
        self.assertIn("Migrated", text(self.pub.edits[-1][2]))
        self.assertEqual(self.run_once()["open"], 0)
        self.assertEqual(len(self.pub.edits), 1)

    def test_closes_after_tracking_window(self):
        far = time.time() + self.s.max_tracking_hours * 3600 + 60
        c = self.run_once(now=far)
        self.assertEqual(c["closed"], 1)
        self.assertEqual(self.row()["status"], "CLOSED")
        self.assertIn("Tracking ended", text(self.pub.edits[-1][2]))
        self.assertEqual(self.rpc.calls, 0)

    def test_posts_show_reject_from_latest_research(self):
        self.db.set_state(MINT, "REJECTED", last_action="REJECT", last_score=81.4,
                          reasons=["market cap already +480% since launch"])
        self.rpc.cap = 120.0
        self.run_once()
        t = text(self.pub.edits[-1][2])
        self.assertIn("REJECT", t)
        self.assertIn("no longer meets", t)
        self.assertIn("+480%", t)

    def test_tracks_without_launch_event_using_derived_curve(self):
        from northstar.analysis.pda import pump_bonding_curve_address
        self.db.execute("DELETE FROM discovery_events WHERE mint=?", (MINT,))
        asked = []
        orig = self.rpc.get_multiple_accounts

        async def spy(addrs):
            asked.extend(addrs)
            return await orig(addrs)
        self.rpc.get_multiple_accounts = spy
        self.rpc.cap = 130.0
        c = self.run_once()
        self.assertEqual(asked, [pump_bonding_curve_address(MINT)])
        self.assertEqual(c["edited"], 1)

    def test_permanent_edit_failure_stops_retrying(self):
        self.pub.fail = PublishError("message/channel not found", False)
        self.rpc.cap = 180.0
        self.run_once()
        self.assertEqual(self.row()["status"], "UNTRACKED")
        self.assertEqual(self.run_once()["open"], 0)

    def test_retryable_edit_failure_retries_next_cycle(self):
        self.pub.fail = PublishError("discord HTTP 503", True)
        self.rpc.cap = 180.0
        self.run_once()
        self.assertEqual(self.row()["status"], "SENT")
        self.pub.fail = None
        self.assertEqual(self.run_once()["edited"], 1)

    def test_pending_or_uncertain_posts_are_ignored(self):
        self.db.update_early_signal_status(MINT, "UNCERTAIN")
        self.assertEqual(self.run_once()["open"], 0)
        self.assertEqual(self.rpc.calls, 0)

    def test_embed_never_exceeds_discord_limits(self):
        self.db.set_state(MINT, "REJECTED", last_action="REJECT", last_score=50.0, reasons=["y" * 5000])
        self.rpc.cap = 500.0
        self.run_once()
        e = self.pub.edits[-1][2]
        self.assertLessEqual(len(e["description"]), 4096)
        self.assertLessEqual(len(e.get("fields", [])), 25)
        self.assertIn("Not financial advice", e["footer"]["text"])
        self.assertIn(f"mint:{MINT}", e["footer"]["text"])
        self.assertEqual(e["_buttons"][0]["url"], f"https://axiom.trade/t/{MINT}")

    def test_compact_layout_without_usd_rate_falls_back_to_sol(self):
        self.rpc.cap = 164.0
        self.run_once()
        d = self.pub.edits[-1][2]["description"]
        for label in ("**Market Cap:**", "**5m Trades:**", "**Unique Buyers:**", "**Activity Score:**",
                      "**Address:**", "**Called At:** 100.00 SOL (USD unavailable)",
                      "**Now:** 164.00 SOL (USD unavailable) (1.64x · +64.0%)", "**Peak:** 1.64x"):
            self.assertIn(label, d)
        self.assertNotIn("Observed performance", d)

    def test_values_shown_in_usd_with_call_time_rate(self):
        # baseline recorded when SOL was $150; SOL is $160 now
        self.db.execute("DELETE FROM early_signals WHERE mint=?", (MINT,))
        report = {"mint": MINT, "identity": {"symbol": "ERLY", "name": "Early"},
                  "pre_migration": {"opportunity_score": 88, "market_cap_sol_current": 100.0, "sol_usd": 150.0,
                                    "trade_events": 15, "buys": 13, "sells": 2, "unique_buyers": 11,
                                    "buy_sol_5m": 3.0}}
        self.db.create_or_update_early_signal(MINT, 100.0, 88.0, report)
        self.db.update_early_signal_status(MINT, "SENT", 11, 22)

        class Price:
            async def get(self):
                return 160.0
        self.tr.sol_price = Price()
        self.rpc.cap = 164.0
        self.run_once()
        d = self.pub.edits[-1][2]["description"]
        self.assertIn("**Market Cap:** $15.0K", d)          # research-time snapshot: 100 SOL x $150
        self.assertIn("**5m Buy Flow:** $450.00", d)
        self.assertIn("**Called At:** $15.0K", d)
        self.assertIn("**Now:** $26.2K (1.75x · +74.9%)", d)   # 164 SOL x $160 vs 100 SOL x $150
        self.assertNotIn("SOL", d.split("**Links:**")[0].replace("Bonding curve", ""))

    def test_call_time_rate_survives_report_replacement(self):
        report = {"mint": MINT, "pre_migration": {"sol_usd": 150.0}}
        self.db.execute("DELETE FROM early_signals WHERE mint=?", (MINT,))
        self.db.create_or_update_early_signal(MINT, 100.0, 88.0, report)
        row, _ = self.db.create_or_update_early_signal(MINT, 120.0, 90.0, {"mint": MINT,
                                                                         "pre_migration": {"sol_usd": 999.0}})
        self.assertEqual(row["entry_sol_usd"], 150.0)


if __name__ == "__main__":
    unittest.main()


def _curve_with_quote(quote: bytes, cap_sol: float = 100.0, real_sol: int = 0, lamports=None) -> dict:
    raw = bytearray(8 + 40 + 1 + 32 + 2 + 32 + 16)
    raw[:8] = bytes.fromhex("17b7f83760d8ac60")
    struct.pack_into("<QQQQQ", raw, 8, SUPPLY, int(cap_sol * 1e9), 1, real_sol, SUPPLY)
    raw[83:115] = quote
    acc = {"owner": PUMP_FUN_PROGRAM, "data": [base64.b64encode(bytes(raw)).decode(), "base64"]}
    if lamports is not None:
        acc["lamports"] = lamports
    return acc


class CurveQuoteTests(unittest.TestCase):
    def test_sol_backing_decides_quote_not_byte_offsets(self):
        from northstar.analysis.pump_trades import b58decode
        from northstar.providers.solana_rpc import USDC_MINT
        from northstar.research import Researcher
        parse = Researcher._parse_pump_curve_account
        legacy = parse(curve_account(100.0))                       # no lamports supplied
        self.assertTrue(legacy["quote_is_sol"])
        self.assertAlmostEqual(legacy["market_cap_sol"], 100.0, places=6)
        # Live regression: bytes 83..115 held another pump mint, but the curve holds its SOL.
        odd = parse(_curve_with_quote(b58decode(MINT), real_sol=30 * 10**9, lamports=30 * 10**9 + 2_000_000))
        self.assertTrue(odd["quote_is_sol"])
        self.assertAlmostEqual(odd["market_cap_sol"], 100.0, places=6)
        fresh = parse(_curve_with_quote(bytes(32), real_sol=0, lamports=1_500_000))
        self.assertTrue(fresh["quote_is_sol"])
        # Token-quoted curve: real reserves far above the account's lamports.
        usdc = parse(_curve_with_quote(b58decode(USDC_MINT), real_sol=50 * 10**9, lamports=2_000_000))
        self.assertFalse(usdc["quote_is_sol"])
        self.assertEqual(usdc["quote_mint"], USDC_MINT)
        self.assertIsNone(usdc["market_cap_sol"])
        unknown = parse(_curve_with_quote(b58decode(MINT), real_sol=50 * 10**9, lamports=2_000_000))
        self.assertFalse(unknown["quote_is_sol"])
        self.assertEqual(unknown["quote_mint"], "UNKNOWN")      # unrecognised field not trusted

    def test_tracker_does_not_read_non_sol_curve_as_sol(self):
        from northstar.analysis.pump_trades import b58decode
        from northstar.providers.solana_rpc import USDC_MINT
        base = Base("run")
        base.setUp()
        try:
            async def accounts(addrs):
                return [_curve_with_quote(b58decode(USDC_MINT), 999.0, real_sol=50 * 10**9, lamports=2_000_000)
                        for _ in addrs]
            base.rpc.get_multiple_accounts = accounts
            c = base.run_once()
            self.assertEqual((c["read"], c["failed_reads"]), (0, 1))
            self.assertAlmostEqual(base.row()["current_market_cap_sol"], 100.0)
        finally:
            base.tearDown()


class SilentTracking(Base):
    """Early watches kept out of every channel are still measured, never edited."""

    def test_silent_signal_is_tracked_without_editing(self):
        self.db.execute("UPDATE early_signals SET status='SILENT', channel_id=NULL, message_id=NULL WHERE mint=?",
                        (MINT,))
        self.rpc.cap = 250.0
        counts = asyncio.run(self.tr.run_once())
        self.assertEqual((counts["open"], counts["read"], counts["edited"]), (1, 1, 0))
        self.assertEqual(self.pub.edits, [])
        row = self.db.fetchone("SELECT * FROM early_signals WHERE mint=?", (MINT,))
        self.assertAlmostEqual(row["peak_multiple"], 2.5)
        self.assertEqual(row["status"], "SILENT")
        self.rpc.complete = True
        asyncio.run(self.tr.run_once())
        self.assertEqual(self.db.fetchone("SELECT status FROM early_signals WHERE mint=?", (MINT,))["status"],
                         "MIGRATED")
        self.assertEqual(self.pub.edits, [])


class TradePlanMessages(Base):
    """Early watches post TP / stop messages as new messages in the same channel."""

    def test_tp_then_trailing_exit_posted_once(self):
        self.rpc.cap = 160.0
        asyncio.run(self.tr.run_once())
        titles = [e["title"] for _, e in self.pub.posts]
        self.assertTrue(any(t.startswith("🎯 TAKE PROFIT 1") for t in titles), titles)
        self.assertEqual(self.pub.posts[0][0], 11)                       # same channel as the watch
        self.rpc.cap = 105.0
        asyncio.run(self.tr.run_once())
        asyncio.run(self.tr.run_once())
        exits = [e for _, e in self.pub.posts if e["title"].startswith("🚪 EXIT")]
        self.assertEqual(len(exits), 1)
        rep = json.loads(self.db.fetchone("SELECT report FROM early_signals WHERE mint=?", (MINT,))["report"])
        self.assertEqual(rep["early_tracking"]["plan_sent"], ["TP1", "TRAIL"])
        self.assertIn("Plan status", text(self.pub.edits[-1][2]))

    def test_stop_loss(self):
        self.rpc.cap = 60.0
        asyncio.run(self.tr.run_once())
        self.assertTrue(any(e["title"].startswith("🛑 EXIT — STOP LOSS") for _, e in self.pub.posts))

    def test_silent_watch_posts_nothing(self):
        self.db.execute("UPDATE early_signals SET status='SILENT', channel_id=NULL, message_id=NULL WHERE mint=?",
                        (MINT,))
        self.rpc.cap = 160.0
        asyncio.run(self.tr.run_once())
        self.assertEqual(self.pub.posts, [])
