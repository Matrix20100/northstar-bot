"""Publishing, duplicate protection, Discord errors, post-callout monitoring — MOCK data only."""
from __future__ import annotations

import asyncio
import json
import time

from conftest import CREATOR, MINT, NOW, POOL, MemoryPublisher, make_pair, seed_profile, strong_providers, swap_tx
from northstar.alerts import AlertService
from northstar.discord_bot.embeds import callout_embed, embed_size
from northstar.discord_bot.publisher import DryRunPublisher, PublishError
from northstar.engine import Engine
from northstar.models import Action
from northstar.monitor import Monitor
from northstar.performance import OutcomeTracker, format_report, performance_report
from northstar.research import Researcher


async def qualified(settings, db, providers=None):
    providers = providers or strong_providers()
    seed_profile(db)
    out = await Researcher(settings, db, providers).research(MINT)
    assert out.action == Action.AUTO_POST
    return out, providers


async def published(settings, db):
    out, p = await qualified(settings, db)
    pub = MemoryPublisher()
    svc = AlertService(settings, db, pub, p.dex)
    aid = await svc.publish(out)
    assert aid
    return aid, pub, p, out


async def test_publish_and_duplicate_alert_suppressed(settings, db):
    out, p = await qualified(settings, db)
    pub = MemoryPublisher()
    svc = AlertService(settings, db, pub, p.dex)
    assert await svc.publish(out)
    assert await svc.publish(out) is None, "second alert for same mint inside cooldown must be suppressed"
    assert len(pub.sent) == 1
    e = pub.sent[0][1]
    text = json.dumps(e)
    assert f"solscan.io/token/{MINT}" in text and f"mint:{MINT}" in e["footer"]["text"]
    assert "not a trade recommendation" in e["footer"]["text"]
    risk_field = next(f for f in e["fields"] if f["name"].startswith("Risk"))
    assert "not rug-proof" in risk_field["value"].lower()
    assert embed_size(e) < 6000
    assert db.get_candidate(MINT)["state"] == "PUBLISHED"


async def test_engine_does_not_republish_published_mint(settings, db):
    aid, pub, p, _ = await published(settings, db)
    eng = Engine(settings, db, p, pub)
    db.execute("UPDATE candidates SET next_research_at=? WHERE mint=?", (0, MINT))
    await eng.process(MINT)
    assert len(pub.sent) == 1


async def test_daily_cap_blocks_auto_post(settings, db):
    settings.max_daily_alerts = 0
    seed_profile(db)
    out = await Researcher(settings, db, strong_providers()).research(MINT)
    assert out.action == Action.WATCHLIST
    assert "daily alert cap reached" in out.reasons


async def test_preflight_hold_when_liquidity_drops(settings, db):
    out, p = await qualified(settings, db)
    p.dex.pairs = [make_pair(liquidity_usd=20_000.0)]
    pub = MemoryPublisher()
    assert await AlertService(settings, db, pub, p.dex).publish(out) is None
    assert not pub.sent and db.get_candidate(MINT)["state"] == "WATCHLIST"


async def test_discord_retryable_error_then_failed(settings, db):
    out, p = await qualified(settings, db)
    pub = MemoryPublisher()
    pub.fail_next = [PublishError("HTTP 503", retryable=True)] * 3
    svc = AlertService(settings, db, pub, p.dex)
    assert await svc.publish(out) is None
    a = db.fetchone("SELECT * FROM alerts")
    assert a["status"] == "PENDING" and a["publish_attempts"] == 1
    await svc.resume_pending()
    await svc.resume_pending()
    a = db.fetchone("SELECT * FROM alerts")
    assert a["status"] == "FAILED" and not pub.sent
    assert db.get_candidate(MINT)["state"] == "WATCHLIST"


async def test_discord_retryable_error_then_success_once(settings, db):
    out, p = await qualified(settings, db)
    pub = MemoryPublisher()
    pub.fail_next = [PublishError("HTTP 500", retryable=True)]
    svc = AlertService(settings, db, pub, p.dex)
    await svc.publish(out)
    await svc.resume_pending()
    assert len(pub.sent) == 1
    assert db.fetchone("SELECT status FROM alerts")["status"] == "DRY_RUN"  # settings.dry_run=True


async def test_discord_forbidden_not_retried(settings, db):
    out, p = await qualified(settings, db)
    pub = MemoryPublisher()
    pub.fail_next = [PublishError("forbidden", retryable=False)]
    await AlertService(settings, db, pub, p.dex).publish(out)
    assert db.fetchone("SELECT status FROM alerts")["status"] == "FAILED"


async def test_uncertain_delivery_never_double_posts(settings, db):
    out, p = await qualified(settings, db)
    pub = MemoryPublisher()
    pub.fail_next = [PublishError("timeout after send", retryable=True, uncertain=True)]
    svc = AlertService(settings, db, pub, p.dex)
    await svc.publish(out)
    # pretend the message did land before the timeout
    pub.messages[777] = callout_embed(out.report)
    await svc.resume_pending()
    a = db.fetchone("SELECT * FROM alerts")
    assert a["message_id"] == 777 and a["status"] in ("SENT", "DRY_RUN")
    assert not pub.sent, "recovered from channel history instead of re-posting"


async def test_restart_recovery_of_pending_alert(settings, db, tmp_path):
    out, p = await qualified(settings, db)
    pub = MemoryPublisher()
    # simulate crash right after the PENDING row was written
    aid = db.create_alert_intent(MINT, 0, snapshot_id=out.snapshot_id, pool_address=POOL, entry_price=0.0012,
                                 entry_liq=120000.0)
    svc2 = AlertService(settings, db, pub, p.dex)
    await svc2.resume_pending()
    assert len(pub.sent) == 1
    assert db.get_alert(aid)["status"] in ("SENT", "DRY_RUN")


async def test_liquidity_collapse_after_publication(settings, db):
    aid, pub, p, _ = await published(settings, db)
    p.dex.pairs = [make_pair(liquidity_usd=30_000.0, price_usd=0.0009)]
    mon = Monitor(settings, db, pub, p.dex, None)
    await mon.run_once()
    a = db.get_alert(aid)
    assert "LIQUIDITY_COLLAPSE" in json.loads(a["warnings"]) and a["state"] == "WARNING"
    assert any("WARNING" in e["title"] for _, e in pub.sent)
    assert pub.edits, "original embed edited, not spammed"
    await mon.run_once()
    assert sum("WARNING" in e["title"] for _, e in pub.sent) == 1, "warning sent once"


async def test_creator_dump_after_publication(settings, db):
    aid, pub, p, _ = await published(settings, db)
    db.update_alert(aid, sent_at=NOW - 600)
    p.rpc.trades = p.rpc.trades + [swap_tx(CREATOR, "sell", 30_000_000, 99_999, int(time.time()))]
    mon = Monitor(settings, db, pub, p.dex, p.rpc)
    await mon.run_once()                      # starts the on-chain check in the background
    await asyncio.gather(*list(mon._slow_tasks.values()))
    await mon.run_once()                      # applies its result
    a = db.get_alert(aid)
    assert "CREATOR_DUMP" in json.loads(a["warnings"])
    warn = [e for _, e in pub.sent if "WARNING" in e["title"]]
    assert warn and "VERIFIED on-chain" in warn[0]["description"]


async def test_milestones_and_performance_math(settings, db):
    settings.embed_edit_min_seconds = 0
    aid, pub, p, out = await published(settings, db)
    entry = out.pair.price_usd
    p.dex.pairs = [make_pair(price_usd=entry * 3.2, market_cap=out.pair.market_cap * 3.2)]
    mon = Monitor(settings, db, pub, p.dex, None)
    await mon.run_once()
    a = db.get_alert(aid)
    assert json.loads(a["milestones_hit"]) == [1.5, 2.0, 3.0]
    assert abs(a["max_multiple"] - 3.2) < 1e-9
    p.dex.pairs = [make_pair(price_usd=entry * 0.6, market_cap=out.pair.market_cap * 0.6)]
    await mon.run_once()
    a = db.get_alert(aid)
    assert abs(a["max_multiple"] - 3.2) < 1e-9
    assert abs(a["max_drawdown_pct"] - 40.0) < 1e-6
    assert len([e for _, e in pub.sent if "hit" in e["title"]]) == 3, "one message per milestone"
    edited = pub.edits[-1][1]
    perf = next(f for f in edited["fields"] if f["name"] == "Since callout")["value"]
    assert "-40.0%" in perf and "0.60x" in perf


async def test_missing_price_keeps_last_valid_never_zero(settings, db):
    aid, pub, p, out = await published(settings, db)
    p.dex.pairs = []  # pool vanished
    mon = Monitor(settings, db, pub, p.dex, None)
    await mon.run_once()
    a = db.get_alert(aid)
    assert a["current_price"] == out.pair.price_usd
    assert a["price_status"] == "UNAVAILABLE"
    assert db.fetchone("SELECT price FROM price_history WHERE alert_id=? ORDER BY ts DESC", (aid,))["price"] is None
    db.update_alert(aid, last_valid_price_at=time.time() - 4000)
    await mon.run_once()
    assert db.get_alert(aid)["state"] == "STALE"


async def test_tracking_closes_after_window(settings, db):
    aid, pub, p, _ = await published(settings, db)
    db.update_alert(aid, created_at=time.time() - (settings.max_tracking_hours + 1) * 3600)
    await Monitor(settings, db, pub, p.dex, None).run_once()
    assert db.get_alert(aid)["state"] == "CLOSED"
    assert db.get_candidate(MINT)["state"] == "CLOSED"


# ---------------------------------------------------------------- historical evaluation
async def test_outcomes_tracked_for_rejected_and_dead_pools(settings, db):
    # v2.5.5: rejected coins are tracked as a sample (reject_tracking_sample_pct); 100% here so the
    # dead-pool accounting itself is tested. The sampling rule is tested in test_tracking_policy.py.
    settings = settings.model_copy(update={"reject_tracking_sample_pct": 100.0})
    p = strong_providers()
    p.dex.pairs = [make_pair(liquidity_usd=8_000.0)]
    seed_profile(db)
    out = await Researcher(settings, db, p).research(MINT)
    assert out.action == Action.REJECT
    db.execute("UPDATE outcomes SET due_at=0")
    p.dex.pairs = []  # pool died
    tracker = OutcomeTracker(db, p.dex)
    assert await tracker.run_once() == 5
    rows = db.fetchall("SELECT status FROM outcomes")
    assert {r["status"] for r in rows} == {"UNAVAILABLE"}
    rep = performance_report(db)
    g = rep["groups"]["REJECT"]
    assert g["horizons"]["1h"]["unavailable"] == 1
    assert g["horizons"]["1h"]["median_return_conservative_pct"] == -100.0
    assert "too few" in g["sample_warning"]
    assert "rug checks never change" in format_report(rep)


async def test_outcome_fetch_failure_does_not_mark_unavailable(settings, db):
    from northstar.net import ProviderError
    p = strong_providers()
    seed_profile(db)
    await Researcher(settings, db, p).research(MINT)
    db.execute("UPDATE outcomes SET due_at=0")
    p.dex.fail = ProviderError("dexscreener", "MOCK down")
    assert await OutcomeTracker(db, p.dex).run_once() == 0
    assert {r["status"] for r in db.fetchall("SELECT status FROM outcomes")} == {"PENDING"}


async def test_dry_run_publisher_writes_file(tmp_path):
    pub = DryRunPublisher(tmp_path / "a.jsonl")
    ch, mid = await pub.publish({"title": "t", "footer": {"text": f"mint:{MINT}"}})
    assert await pub.find_message_with(f"mint:{MINT}") == mid
    await pub.edit(ch, mid, {"title": "t2"})
    assert len((tmp_path / "a.jsonl").read_text().splitlines()) == 2
