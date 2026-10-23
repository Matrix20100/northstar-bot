"""v4.5.0: confirmed rugs are blocked for good, the queue stops churning on dead coins, rug scalps go
to their own channel, and the Watching tab shows only live coins that passed safety.

Live 24h funnel the operator sent: 24,382 discovered, 29,638 "not ready" researches,
919 x "prefilter: liquidity $4 below prefilter $5,000", rug coins on the Watching tab and radar.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from conftest import MINT, MemoryPublisher, addr, make_pair
from northstar import brain as B
from northstar import rugs as RG
from northstar.discovery import Discovery
from northstar.models import Action, Confidence, RiskLevel
from northstar.radar import SurgeRadar
from northstar.research import Providers, ResearchOutcome


@pytest.fixture(autouse=True)
def _dead_line():
    old = B.DEAD_MCAP
    B.DEAD_MCAP = 5_000.0
    yield
    B.DEAD_MCAP = old


# ----------------------------------------------------------------------------- the rule
def test_confirmed_rug_rules(settings):
    assert RG.confirmed(4_000, 900, True, 40_000, 12_000, settings).startswith("died")
    assert RG.confirmed(30_000, 1_000, True, 60_000, 25_000, settings).startswith("liquidity pulled")
    assert RG.confirmed(9_000, 4, True, None, None, settings) == "dead pool: $4 liquidity"   # the 919 x "$4"
    assert RG.confirmed(30_000, 9_000, True, 60_000, 25_000, settings) is None              # down, not dead
    assert RG.confirmed(3_000, None, False, None, None, settings) is None                   # fresh launch
    assert RG.confirmed(4_500, None, False, 17_000, None, settings).startswith("died")       # curve coin died


def test_flags_are_not_blocks(settings):
    report = {"gates": [{"name": "mint_authority", "status": "FAIL"}, {"name": "liquidity", "status": "FAIL"}],
              "unified": {"rug_odds": 0.72}}
    assert RG.flags(report, settings) == ["mint authority", "rug odds 72%"]
    assert RG.flags({"gates": [{"name": "holder_concentration", "status": "FAIL"}]}, settings) == []


# ----------------------------------------------------------------------------- engine
def _engine(settings, db, pair):
    from northstar.engine import Engine

    class Dex:
        pairs = [pair]

        async def token_pairs(self, mint):
            return self.pairs

        async def tokens_batch(self, mints):
            return self.pairs
    return Engine(settings, db, Providers(dex=Dex()), MemoryPublisher())


def _out(pair, action=Action.REJECT, gates=None, reasons=None):
    report = {"identity": {"symbol": "RUG", "name": "Rug"}, "gates": gates or [],
              "market": {"market_cap": {"value": pair.market_cap}, "liquidity_usd": {"value": pair.liquidity_usd}}}
    return ResearchOutcome(MINT, action, report, "h", pair, reasons=reasons or ["prefilter: liquidity"],
                           risk=RiskLevel.ELEVATED, confidence=Confidence.LOW)


async def test_dead_pool_is_blocked_for_good(settings, db, monkeypatch):
    pair = make_pair(market_cap=9_000.0, liquidity_usd=4.0)
    eng = _engine(settings, db, pair)
    db.upsert_discovery(MINT, "pumpportal_migration", 3.0, "Rug", "RUG")
    calls = []

    async def research(mint, cand=None, force=False):
        calls.append(mint)
        return _out(pair)
    monkeypatch.setattr(eng.researcher, "research", research)
    await eng.process(MINT)
    c = db.get_candidate(MINT)
    assert c["state"] == "RUGGED" and c["next_research_at"] is None
    assert json.loads(c["reasons"])[0] == "confirmed rug: dead pool: $4 liquidity"
    await eng.process(MINT)
    assert calls == [MINT], "a blocked coin is never researched again"
    assert eng.stats["rugs_blocked"] == 1


async def test_coin_that_died_after_its_high_is_blocked(settings, db, monkeypatch):
    from northstar import curve_watch as CW
    CW.init_schema(db)
    db.execute("INSERT INTO curve_watch(mint, symbol, entered_at, entry_mc, peak_mc, low_mc, done) "
               "VALUES (?, 'RUG', ?, 17000, 37800, 4400, 1)", (MINT, time.time() - 3600))
    pair = make_pair(market_cap=4_500.0, liquidity_usd=6_000.0)
    eng = _engine(settings, db, pair)
    db.upsert_discovery(MINT, "pumpportal_migration", 3.0, "Rug", "RUG")

    async def research(mint, cand=None, force=False):
        return _out(pair)
    monkeypatch.setattr(eng.researcher, "research", research)
    await eng.process(MINT)
    c = db.get_candidate(MINT)
    assert c["state"] == "RUGGED" and "died: market cap $4,500 after a $37,800 high" in c["reasons"]


async def test_flagged_coin_is_rejected_not_blocked_and_peaks_are_kept(settings, db, monkeypatch):
    pair = make_pair(market_cap=60_000.0, liquidity_usd=20_000.0)
    eng = _engine(settings, db, pair)
    db.upsert_discovery(MINT, "pumpportal_migration", 3.0, "Flag", "FLAG")

    async def research(mint, cand=None, force=False):
        return _out(pair, gates=[{"name": "mint_authority", "status": "FAIL"}], reasons=["gate failed: mint_authority"])
    monkeypatch.setattr(eng.researcher, "research", research)
    await eng.process(MINT)
    c = db.get_candidate(MINT)
    assert c["state"] != "RUGGED"
    assert json.loads(c["rug_flags"]) == ["mint authority"]
    assert c["peak_mcap"] == 60_000 and c["peak_liq"] == 20_000


# ----------------------------------------------------------------------------- queue churn
def test_trades_only_pull_forward_scheduled_coins(settings, db):
    d = Discovery(settings, db, None)
    now = time.time()
    rows = {"sched": ("REJECTED", now + 600, None), "final": ("REJECTED", None, None),
            "launch": ("DISCOVERED", None, None), "fresh": ("WATCHLIST", now + 600, now - 10),
            "rug": ("RUGGED", None, None)}
    mints = {}
    for i, (k, (state, nxt, last)) in enumerate(rows.items()):
        m = addr(k)
        mints[k] = m
        db.upsert_discovery(m, "pumpportal_new", 1.0, k, k)
        db.execute("UPDATE candidates SET state=?, next_research_at=?, last_research_at=? WHERE mint=?",
                   (state, nxt, last, m))
    for m in mints.values():
        asyncio.run(d.on_pumpportal("trade", {"mint": m, "txType": "buy", "marketCapSol": 50}))
    got = {k: db.get_candidate(m) for k, m in mints.items()}
    assert got["sched"]["next_research_at"] <= time.time() + 3            # pulled forward
    assert got["final"]["next_research_at"] is None                         # a finished reject stays finished
    assert got["launch"]["next_research_at"] is None                        # waits for the curve radar
    assert got["fresh"]["next_research_at"] > time.time() + 500             # researched 10 s ago: throttled
    assert got["rug"]["state"] == "RUGGED" and got["rug"]["next_research_at"] is None


# ----------------------------------------------------------------------------- surge radar
def test_radar_blocks_dead_coins_without_research(settings, db):
    dead = make_pair(market_cap=8_000.0, liquidity_usd=4.0)

    class Dex:
        async def tokens_batch(self, mints):
            return [dead]
    db.upsert_discovery(MINT, "pumpportal_migration", 3.0, "Rug", "RUG")
    db.execute("UPDATE candidates SET pool_address = 'pool'")
    radar = SurgeRadar(settings, db, Dex())
    assert asyncio.run(radar.run_once()) == []
    c = db.get_candidate(MINT)
    assert c["state"] == "RUGGED" and "surge radar" in c["reasons"]


# ----------------------------------------------------------------------------- scalps
async def test_scalp_follow_ups_go_to_the_scalp_channel(settings, db):
    from northstar.monitor import Monitor
    settings.discord_scalp_channel_id = 777
    pub = MemoryPublisher()
    db.upsert_discovery(MINT, "pumpportal_migration", 3.0, "Rug", "RUG")
    aid = db.create_alert_intent(MINT, 0, call_type="RUG_SCALP", entry_mcap=100_000.0, entry_price=1.0)
    db.update_alert(aid, status="SENT", channel_id=777, message_id=5)
    mon = Monitor(settings, db, pub, None, None)
    await mon._send({"title": "milestone"}, aid)
    assert pub.sent[-1][0] == 777
    await mon._send({"title": "warning"}, aid, channel=settings.discord_warning_channel_id)
    assert pub.sent[-1][0] == 777


# ----------------------------------------------------------------------------- Watching tab
def test_watching_shows_only_live_safe_coins(db):
    from northstar import learning as L
    from northstar.dashboard import view_watching
    L.init_schema(db)
    now = time.time()
    names = {"safe": "S", "flag": "F", "rug": "R", "queued": "Q"}
    mints = {}
    for i, k in enumerate(names):
        m = addr(k + "w")
        mints[k] = m
        db.upsert_discovery(m, "dexscreener_profile", 2.0, k, k.upper())
    db.execute("UPDATE candidates SET state='WATCHLIST', last_score=70, last_research_at=?, rug_flags='[]', "
               "reasons=? WHERE mint=?", (now, json.dumps(["score 70 < 75"]), mints["safe"]))
    db.execute("UPDATE candidates SET state='WATCHLIST', last_score=80, last_research_at=?, "
               "rug_flags='[\"mint authority\"]' WHERE mint=?", (now, mints["flag"]))
    db.execute("UPDATE candidates SET state='RUGGED', last_research_at=?, updated_at=? WHERE mint=?",
               (now, now, mints["rug"]))
    for k in ("safe", "flag", "rug"):
        db.execute("INSERT INTO radar_obs(mint, ts, pool, price, metrics, fired) VALUES (?,?,?,?,?,1)",
                   (mints[k], now - 60, "p", 1.0, json.dumps({"spike": 3})))
    w = view_watching(db._conn, now + 1)
    assert [q["mint"] for q in w["queue"]] == [mints["safe"]]
    assert w["queue"][0]["why"] == "score 70 < 75"
    assert [s["mint"] for s in w["surges"]] == [mints["safe"]]
    assert (w["watched"], w["flagged_hidden"], w["rugs_blocked_24h"]) == (1, 1, 1)
    assert w["queued"] == 3            # everything with a next check: 2 re-checks + 1 new coin (never the rug)


def test_blocked_rugs_are_remembered_for_30_days(db):
    now = time.time()
    db.upsert_discovery(MINT, "pumpportal_migration", 3.0, "Rug", "RUG")
    db.execute("UPDATE candidates SET state='RUGGED', next_research_at=NULL, updated_at=?", (now - 10 * 86400,))
    db.prune_candidates(7.0, now)
    assert db.get_candidate(MINT) is not None
    db.execute("UPDATE candidates SET updated_at=?", (now - 31 * 86400,))
    db.prune_candidates(7.0, now)
    assert db.get_candidate(MINT) is None
