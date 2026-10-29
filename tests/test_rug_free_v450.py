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


# ----------------------------------------------------------------------------- the brain obeys the risk system
def _brain_out(mint, p=0.4, rug=0.1, risk="MODERATE", gates=None):
    import types
    report = {"brain": {"p": p}, "unified": {"version": 1, "rug_odds": rug}, "risk": risk, "gates": gates or []}
    return types.SimpleNamespace(mint=mint, report=report, risk=RiskLevel(risk),
                                 pair=make_pair(base_mint=mint, base_symbol="BRN", market_cap=416_000.0, fdv=416_000.0))


def test_brain_never_buys_what_the_risk_system_rejects(settings, db):
    """Live: the brain paper-bought $McNugget - 79% bundled at launch, already dumped, risk HIGH."""
    from northstar import paper as PT
    bt = PT.BrainTrader(settings, db, None)
    mcnugget = _brain_out(addr("mcn"), risk="ELEVATED", gates=[
        {"name": "launch_buys", "status": "FAIL"}, {"name": "launch_shape", "status": "FAIL"}])
    # v5.1.4: HIGH risk is weighed (conviction), but its failed launch checks still refuse it
    assert bt.consider(mcnugget) == "risk system: failed launch buys, launch shape"
    bundled = _brain_out(addr("bnd"), gates=[{"name": "launch_buys", "status": "FAIL"}])
    assert bt.consider(bundled) == "risk system: failed launch buys"
    flagged = _brain_out(addr("flg"), gates=[{"name": "mint_authority", "status": "FAIL"}])
    assert bt.consider(flagged).startswith("rug flags: mint authority")
    # runner odds are discounted by the rug brain: 20% x (1 - 29%) = 14% < 15%
    assert "= 14%" in bt.consider(_brain_out(addr("mix"), p=0.2, rug=0.29))
    assert bt.consider(_brain_out(addr("ok"))) == "bought"


def test_brain_is_offered_the_risk_systems_verdicts():
    v = B.vector({"unified": {"rug_odds": 0.72}, "risk": "ELEVATED"})
    assert v["rug_odds"] == 0.72 and v["risk_level"] == 2.0
    assert "rug_odds" in B.EXPERIMENTAL_LABELS and "rug_odds" not in B.FEATURES   # adopted only if it helps


# ----------------------------------------------------------------------------- the watch list ends
async def test_watch_list_drops_coins_under_the_minimum_market_cap(settings, db, monkeypatch):
    """Live: $XOGE watched at $3.1K, $FOMC (score 100) at $5K."""
    pair = make_pair(market_cap=5_000.0, liquidity_usd=9_000.0)
    eng = _engine(settings, db, pair)
    db.upsert_discovery(MINT, "dexscreener_profile", 2.0, "Fomc", "FOMC")

    async def research(mint, cand=None, force=False):
        return _out(pair, action=Action.WATCHLIST, reasons=["score 100"])
    monkeypatch.setattr(eng.researcher, "research", research)
    await eng.process(MINT)
    c = db.get_candidate(MINT)
    assert c["state"] == "STALE" and c["next_research_at"] is None
    assert json.loads(c["reasons"])[0] == "left the watch list: market cap $5,000 is under the watch-list minimum $8,000"


def test_watch_list_expires_after_max_hours(db):
    now = time.time()
    ms = [addr("w1"), addr("w2"), addr("w3")]
    for m in ms:
        db.upsert_discovery(m, "dexscreener_profile", 2.0, "w", "W")
    db.set_state(ms[0], "WATCHLIST")
    db.execute("UPDATE candidates SET watch_since = ? WHERE mint = ?", (now - 3 * 3600, ms[0]))
    db.set_state(ms[1], "WATCHLIST")                                     # just added
    db.execute("UPDATE candidates SET state='WATCHLIST', watch_since=NULL, discovered_at=? WHERE mint=?",
               (now - 9 * 3600, ms[2]))                                  # watched before v4.5.0 (no watch_since)
    assert db.expire_watchlist(2.0, now) == 2
    states = {m: db.get_candidate(m)["state"] for m in ms}
    assert states == {ms[0]: "STALE", ms[1]: "WATCHLIST", ms[2]: "STALE"}
    # re-research keeps the original watch time; leaving the list clears it
    db.set_state(ms[1], "RESEARCHING")
    first = db.get_candidate(ms[1])["watch_since"]
    db.set_state(ms[1], "WATCHLIST")
    assert db.get_candidate(ms[1])["watch_since"] == first
    db.set_state(ms[1], "REJECTED")
    assert db.get_candidate(ms[1])["watch_since"] is None
