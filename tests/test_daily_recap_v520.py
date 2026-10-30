"""v5.2.0: a daily recap of every call's multiple in the free channel, and no stop-loss / drawdown posts.
Operator: "make a recap system that posts in the free channel every day of all the multipliers and remove the
drawdown or stop loss discord msgs"."""
from __future__ import annotations

import asyncio
import calendar
import json

from conftest import MemoryPublisher, addr
from northstar import early_calls as EC
from northstar import recap as R
from northstar import trade_plan as TP
from northstar.config import Settings

DAY_20H = calendar.timegm((2026, 10, 29, 20, 0, 0))       # 20:00 UTC, after the 19:00 recap hour


def S(**kw):
    return Settings(_env_file=None, discord_channel_id=111, discord_free_channel_id=222, **kw)


def runner_call(db, sym, entry, peak, at):
    EC.init_schema(db)
    db.execute("INSERT INTO early_calls(mint, created_at, status, phase, entry_mcap, peak_mcap, current_mcap, report) "
               "VALUES (?,?,?,?,?,?,?,?)", (addr(sym), at, "SENT", "CURVE", entry, peak, peak * 0.5,
                                            json.dumps({"identity": {"symbol": sym}})))


def standard_call(db, sym, entry, peak, at, call_type="STANDARD"):
    db.execute("INSERT INTO alerts(mint, symbol, status, state, created_at, entry_mcap, peak_mcap, max_multiple, "
               "call_type) VALUES (?,?,?,?,?,?,?,?,?)", (addr(sym), sym, "SENT", "TRACKING", at, entry, peak,
                                                        peak / entry, call_type))


def seed(db):
    runner_call(db, "BER", 16_500, 127_100, DAY_20H - 3 * 3600)
    runner_call(db, "DUD", 18_000, 18_000, DAY_20H - 2 * 3600)
    standard_call(db, "BIG", 80_000, 200_000, DAY_20H - 5 * 3600)
    standard_call(db, "SCALP", 80_000, 400_000, DAY_20H - 5 * 3600, call_type="RUG_SCALP")
    runner_call(db, "OLD", 20_000, 200_000, DAY_20H - 30 * 3600)          # older than 24 h


def test_every_call_of_the_day_is_listed_best_first(db):
    seed(db)
    calls = R.calls_between(db, DAY_20H - R.DAY, DAY_20H)
    assert [c["symbol"] for c in calls] == ["BER", "BIG", "DUD"]          # no scalp, nothing older than 24 h
    assert round(calls[0]["x"], 2) == 7.70 and calls[-1]["x"] == 1.0
    desc = R.build(calls, "29 Oct 2026")[0]["description"]
    assert "**3 calls in the last 24 hours** · 2 went 1.4x or more · 2 reached 2x or more · best **$BER 7.70x**" in desc
    assert "🚀 **$BER** — **7.70x** peak ($16.5K → $127.1K)" in desc and "$DUD" not in desc   # v5.5.1: 1.4x+ only


def test_posted_once_a_day_after_the_hour_to_the_free_channel(db):
    seed(db)
    pub = MemoryPublisher()
    s = S()
    assert asyncio.run(R.run_once(db, s, pub, now=DAY_20H - 2 * 3600))["posted"] == 0       # 18:00: not yet
    assert asyncio.run(R.run_once(db, s, pub, now=DAY_20H))["posted"] == 1
    assert pub.sent[-1][0] == 222 and pub.sent[-1][1]["title"].startswith("📊 Daily recap · 29 Oct 2026")
    assert asyncio.run(R.run_once(db, s, pub, now=DAY_20H + 600))["posted"] == 0            # not twice
    runner_call(db, "NEXT", 20_000, 60_000, DAY_20H + 3600)
    assert asyncio.run(R.run_once(db, s, pub, now=DAY_20H + R.DAY))["posted"] == 1          # next day


def test_a_recap_channel_wins_over_the_free_channel(db):
    seed(db)
    pub = MemoryPublisher()
    asyncio.run(R.run_once(db, S(discord_recap_channel_id=333), pub, now=DAY_20H))
    assert pub.sent[-1][0] == 333


def test_a_failed_post_is_tried_again(db):
    seed(db)
    pub = MemoryPublisher()
    pub.fail_next = [RuntimeError("discord down")]
    assert asyncio.run(R.run_once(db, S(), pub, now=DAY_20H))["posted"] == 0
    assert asyncio.run(R.run_once(db, S(), pub, now=DAY_20H + 300))["posted"] == 1


def test_a_day_without_calls_posts_nothing_and_off_means_off(db):
    pub = MemoryPublisher()
    assert asyncio.run(R.run_once(db, S(), pub, now=DAY_20H))["posted"] == 0 and pub.sent == []
    seed(db)
    assert asyncio.run(R.run_once(db, S(daily_recap_enabled=False), pub, now=DAY_20H + R.DAY))["posted"] == 0


def test_a_long_day_is_split_over_several_messages():
    calls = [{"mint": addr(f"c{i}"), "symbol": f"COIN{i}", "entry": 20_000, "peak": 40_000, "x": 2.0, "at": i}
             for i in range(120)]
    embeds = R.build(calls, "29 Oct 2026")
    assert len(embeds) >= 2 and all(len(e["description"]) <= 4096 for e in embeds)
    assert sum(e["description"].count("** — **") for e in embeds) == 120          # one line per call


def test_no_stop_loss_or_drawdown_posts():
    assert TP.posts_to_discord("TP1") and TP.posts_to_discord("TP2")
    for kind in ("STOP", "TIME", "DANGER"):
        assert not TP.posts_to_discord(kind, 1.5)
    assert TP.posts_to_discord("TRAIL", 2.83)          # locks in profit: posted
    assert not TP.posts_to_discord("TRAIL", 0.45)      # under the entry: a loss, not posted
