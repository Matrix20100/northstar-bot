"""Entry/exit plan rules and how the monitors post them. SYNTHETIC data only."""
from __future__ import annotations

import json
import time

from conftest import NOW, make_pair
from test_alerts_monitoring import published
from northstar import trade_plan as TP
from northstar.config import Settings
from northstar.discord_bot.embeds import callout_embed, money
from northstar.monitor import Monitor

S = Settings(_env_file=None)
P = TP.build_plan(100_000, S)


async def test_slow_onchain_checks_never_delay_exits_or_edits(settings, db):
    import asyncio

    class SlowRpc:
        def __getattr__(self, name):
            async def slow(*a, **k):
                await asyncio.sleep(30)
            return slow
    settings.embed_edit_min_seconds = 0
    aid, pub, p, out = await published(settings, db)
    p.dex.pairs = [make_pair(price_usd=out.pair.price_usd * 2.1, market_cap=out.pair.market_cap * 2.1)]
    mon = Monitor(settings, db, pub, p.dex, SlowRpc())
    started = time.monotonic()
    await mon.run_once()
    assert time.monotonic() - started < 5, "monitor waited for the slow RPC checks"
    assert any(e["title"].startswith("🎯 TAKE PROFIT 1") for _, e in pub.sent)
    assert pub.edits, "the call embed was edited in the same cycle"
    for t in list(mon._slow_tasks.values()):
        t.cancel()


async def test_call_uses_market_cap_at_post_time(settings, db):
    from conftest import MINT, MemoryPublisher, seed_profile, strong_providers
    from northstar.alerts import AlertService
    from northstar.models import Action
    from northstar.research import Researcher
    p = strong_providers()
    seed_profile(db)
    out = await Researcher(settings, db, p).research(MINT)
    assert out.action == Action.AUTO_POST
    researched_mc = out.pair.market_cap
    p.dex.pairs = [make_pair(price_usd=out.pair.price_usd * 1.17, market_cap=researched_mc * 1.17)]
    pub = MemoryPublisher()
    aid = await AlertService(settings, db, pub, p.dex).publish(out)
    assert abs(db.get_alert(aid)["entry_mcap"] - researched_mc * 1.17) < 1
    e = pub.sent[0][1]
    first = e["description"].split("\n")[0]
    assert first.startswith("**Callout price:**") and first.endswith(f"{money(researched_mc * 1.17)} MC")
    assert db.fetchone("SELECT report FROM research_snapshots WHERE id=?", (out.snapshot_id,)) is not None
fmt = money


def kinds(events):
    return [k for k, _ in events]


def test_plan_levels_and_text():
    assert P.max_entry_mcap == 125_000
    lines = "\n".join(TP.plan_lines(P, fmt))
    for part in ("up to $125.0K", "TP1:** 1.5x ($150.0K) → sell 50%", "TP2:** 2x ($200.0K) → sell 25%",
                 "Stop:** $70.0K (-30%)", "below 1.3x after 30 min"):
        assert part in lines, part
    assert TP.build_plan(None, S) is None and TP.build_plan(0, S) is None


def test_take_profits_then_trailing_exit():
    sent = set()
    ev = TP.evaluate(P, 1.6, 1.6, 120, sent, fmt)
    assert kinds(ev) == ["TP1"]
    sent |= {"TP1"}
    assert kinds(TP.evaluate(P, 2.1, 2.1, 300, sent, fmt)) == ["TP2"]
    sent |= {"TP2"}
    assert TP.evaluate(P, 1.9, 2.1, 400, sent, fmt) == []           # 10% off the high: hold
    ev = TP.evaluate(P, 1.4, 2.1, 500, sent, fmt)                     # 33% off the high: exit
    assert kinds(ev) == ["TRAIL"] and "2.10x high" in ev[0][1]
    assert TP.evaluate(P, 1.0, 2.1, 600, sent | {"TRAIL"}, fmt) == []  # plan finished: silence


def test_break_even_after_tp1():
    ev = TP.evaluate(P, 0.98, 1.55, 600, {"TP1"}, fmt)
    assert kinds(ev) == ["TRAIL"]


def test_stop_loss_and_time_stop():
    assert kinds(TP.evaluate(P, 0.65, 1.1, 120, set(), fmt)) == ["STOP"]
    assert TP.evaluate(P, 1.1, 1.2, 29 * 60, set(), fmt) == []
    ev = TP.evaluate(P, 1.1, 1.2, 31 * 60, set(), fmt)
    assert kinds(ev) == ["TIME"] and "isn't holding 1.3x" in ev[0][1]
    # a past wick to 1.35x does not cancel the time stop: it is judged on where the coin is now
    assert kinds(TP.evaluate(P, 1.1, 1.35, 31 * 60, set(), fmt)) == ["TIME"]
    assert "TIME" not in kinds(TP.evaluate(P, 1.4, 1.45, 31 * 60, set(), fmt))   # holding above 1.3x


def test_entry_closed_and_jump_straight_through_levels():
    assert kinds(TP.evaluate(P, 1.3, 1.3, 60, set(), fmt)) == ["ENTRY_CLOSED"]
    assert kinds(TP.evaluate(P, 2.5, 2.5, 60, set(), fmt)) == ["TP1", "TP2"]
    assert TP.evaluate(P, None, 1.0, 60, set(), fmt) == []


def test_danger_event_text():
    kind, text = TP.danger_event("Creator sold 40% of supply.")
    assert kind == "DANGER" and text.startswith("Creator sold") and "Sell now" in text


def test_invalid_plan_levels_reported():
    bad = Settings(_env_file=None, plan_stop=1.2)
    assert any("trade plan levels" in p for p in bad.config_problems())
    slow = Settings(_env_file=None, monitor_interval_seconds=120)
    assert any("exit signals are checked only every 120s" in p for p in slow.config_problems())
    assert not any("trade plan" in p or "exit signals" in p for p in S.config_problems())


def test_callout_embed_includes_plan():
    report = {"mint": "M" * 32, "identity": {"symbol": "T", "name": "T"},
              "market": {"market_cap": {"value": 100_000}}}
    e = callout_embed(report, plan=TP.plan_lines(P, fmt))
    plan = next(f for f in e["fields"] if f["name"] == "📋 Trade plan")
    assert "Stop:** $70.0K" in plan["value"]
    assert not any(f["name"] == "📋 Trade plan" for f in callout_embed(report)["fields"])


# ---------------------------------------------------------------- monitor integration
async def test_monitor_posts_tp_then_exit_once(settings, db):
    settings.embed_edit_min_seconds = 0
    aid, pub, p, out = await published(settings, db)
    first = pub.sent[0][1]
    assert "**Plan**:" in first["description"], "callout carries the plan"
    entry_mc = out.pair.market_cap
    mon = Monitor(settings, db, pub, p.dex, None)
    p.dex.pairs = [make_pair(price_usd=out.pair.price_usd * 1.6, market_cap=entry_mc * 1.6)]
    await mon.run_once()
    titles = [e["title"] for _, e in pub.sent]
    assert any(t.startswith("🎯 TAKE PROFIT 1") for t in titles)
    p.dex.pairs = [make_pair(price_usd=out.pair.price_usd * 1.05, market_cap=entry_mc * 1.05)]
    await mon.run_once()
    await mon.run_once()
    exits = [e for _, e in pub.sent if e["title"].startswith("🚪 EXIT")]
    # v5.2.0: a trailing stop that sells above the entry (here 1.05x) locks in profit and is posted, once
    assert len(exits) == 1 and "keep the profit" in exits[0]["description"]
    a = db.get_alert(aid)
    assert {"PLAN:TP1", "PLAN:TRAIL"} <= set(json.loads(a["warnings"]))
    assert a["state"] != "WARNING", "plan events are not risk warnings"
    assert not any("exited" in x for x in pub.edits[-1][1]["description"].split("\n"))


async def test_monitor_time_stop_is_recorded_not_posted(settings, db):
    settings.post_drawdowns = True                   # v5.2.0: retired, posts nothing even when on
    aid, pub, p, out = await published(settings, db)
    db.update_alert(aid, sent_at=time.time() - 31 * 60)
    p.dex.pairs = [make_pair(price_usd=out.pair.price_usd * 1.1, market_cap=out.pair.market_cap * 1.1)]
    await Monitor(settings, db, pub, p.dex, None).run_once()
    assert not any(e["title"].startswith("⏱️ EXIT — TIME STOP") for _, e in pub.sent)
    assert "PLAN:TIME" in json.loads(db.get_alert(aid)["warnings"])


async def test_monitor_liquidity_collapse_is_an_exit(settings, db):
    settings.post_drawdowns = True
    aid, pub, p, _ = await published(settings, db)
    p.dex.pairs = [make_pair(liquidity_usd=30_000.0, price_usd=0.0009)]
    await Monitor(settings, db, pub, p.dex, None).run_once()
    assert not any(e["title"].startswith("🚨 EXIT NOW") for _, e in pub.sent)      # v5.2.0: not posted
    assert "PLAN:DANGER" in json.loads(db.get_alert(aid)["warnings"])               # but recorded as an exit


async def test_plan_can_be_disabled(settings, db):
    settings.trade_plan_enabled = False
    aid, pub, p, out = await published(settings, db)
    assert "**Plan**:" not in pub.sent[0][1]["description"]
    p.dex.pairs = [make_pair(price_usd=out.pair.price_usd * 1.6, market_cap=out.pair.market_cap * 1.6)]
    await Monitor(settings, db, pub, p.dex, None).run_once()
    assert not any("TAKE PROFIT" in e["title"] for _, e in pub.sent)


# ---------------------------------------------------------------- market-cap floor / extended toggle
def test_min_callout_market_cap_and_extended_toggle():
    from northstar.analysis.decision import decide
    from northstar.models import Action, Confidence, RiskLevel
    s = Settings(_env_file=None, min_opportunity_score=40, watchlist_min_score=40, min_confidence="MEDIUM",
                 max_allowed_risk="MODERATE", min_narrative_score=0, min_narrative_evidence_items=0,
                 min_callout_market_cap=250_000, block_extended_moves=False)
    ok = decide(42, RiskLevel.MODERATE, Confidence.MEDIUM, [], 0, 0, True, s, market_cap=300_000)
    assert ok.action == Action.AUTO_POST, ok.blockers
    low = decide(42, RiskLevel.MODERATE, Confidence.MEDIUM, [], 0, 0, False, s, market_cap=120_000)
    assert low.action != Action.AUTO_POST and any("below $250,000" in b for b in low.blockers)
    unknown = decide(42, RiskLevel.MODERATE, Confidence.MEDIUM, [], 0, 0, False, s, market_cap=None)
    assert any("UNKNOWN" in b for b in unknown.blockers)
    strict = Settings(_env_file=None)
    assert any("extended" in b for b in decide(90, RiskLevel.LOW_DETECTED, Confidence.HIGH, [], 20, 3, True,
                                               strict, market_cap=1e6).blockers)


def test_momentum_check():
    from northstar.analysis.decision import decide
    from northstar.models import Action, Confidence, RiskLevel
    s = Settings(_env_file=None, min_opportunity_score=40, watchlist_min_score=40, min_confidence="MEDIUM",
                 max_allowed_risk="MODERATE", min_narrative_score=0, min_narrative_evidence_items=0,
                 min_callout_market_cap=100_000, block_extended_moves=False,
                 min_volume_acceleration=1.0, min_buy_sell_ratio_h1=1.2)
    args = (42, RiskLevel.MODERATE, Confidence.MEDIUM, [], 0, 0, False, s)
    assert decide(*args, market_cap=150_000, volume_acceleration=4.4, buy_sell_ratio=3.3).action == Action.AUTO_POST
    fading = decide(*args, market_cap=362_900, volume_acceleration=0.42, buy_sell_ratio=1.079)   # the $mucron call
    assert fading.action != Action.AUTO_POST
    assert any("volume fading" in b for b in fading.blockers) and any("weak buying" in b for b in fading.blockers)
    assert decide(*args, market_cap=150_000, volume_acceleration=2, buy_sell_ratio=float("inf")).action == Action.AUTO_POST
    assert decide(*args, market_cap=150_000, volume_acceleration=None, buy_sell_ratio=2).action != Action.AUTO_POST
    off = Settings(_env_file=None)
    assert not any("fading" in b for b in decide(90, RiskLevel.LOW_DETECTED, Confidence.HIGH, [], 20, 3, False, off,
                                                  volume_acceleration=0.1, buy_sell_ratio=0.5).blockers)


async def test_v4_free_channel_gets_every_call_20_minutes_later_with_milestones_at_once(settings, db):
    """Operator: "the free channel gets VIP callouts 20 minutes later, with all milestones posting at once"."""
    from northstar.alerts import AlertService
    settings.discord_free_channel_id = 555
    assert Settings(_env_file=None).free_callout_delay_minutes == 20
    aid, pub, p, out = await published(settings, db)
    row = db.fetchone("SELECT due_at FROM free_channel_posts WHERE alert_id=?", (aid,))
    assert row and abs(row["due_at"] - (time.time() + settings.free_callout_delay_minutes * 60)) < 5
    mon = Monitor(settings, db, pub, p.dex, None)
    p.dex.pairs = [make_pair(price_usd=out.pair.price_usd * 3.2, market_cap=out.pair.market_cap * 3.2)]
    await mon.run_once()                                     # 1.5x, 2x, 3x reached before the copy
    assert not any(ch == 555 for ch, _ in pub.sent), "nothing in free before the delay"
    db.execute("UPDATE free_channel_posts SET due_at=? WHERE alert_id=?", (time.time() - 1, aid))
    await AlertService(settings, db, pub, p.dex).publish_due_free_posts()
    free = [e for ch, e in pub.sent if ch == 555]
    assert len(free) == 1 + len(json.loads(db.get_alert(aid)["milestones_hit"]))   # the call + milestones at once
    assert free[0]["description"].startswith("**Callout price:**") and "hit" in free[-1]["title"]
    p.dex.pairs = [make_pair(price_usd=out.pair.price_usd * 5.5, market_cap=out.pair.market_cap * 5.5)]
    await mon.run_once()                                     # a new milestone after the copy: free too
    assert any(ch == 555 and "5x" in e["title"] for ch, e in pub.sent)


def test_shipped_runner_plan_is_valid():
    import os
    from conftest import OLD_PLAN
    from northstar.config import Settings
    from northstar.trade_plan import plan_values, valid_values
    saved = {k: os.environ.pop(k) for k in OLD_PLAN}
    try:
        s = Settings(_env_file=None)
    finally:
        os.environ.update(saved)
    v = plan_values(s, "standard")
    assert (v["tp1"], v["tp1_sell_pct"], v["tp2"], v["tp2_sell_pct"], v["stop"], v["trail_pct"]) == (2.0, 33, 4.0, 33, 0.65, 40.0)
    assert valid_values(v) and valid_values(plan_values(s, "scalp"))
    assert not s.config_problems()
