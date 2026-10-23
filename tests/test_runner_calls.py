"""v3.0.0 runner calls: momentum + every safety check passed + runner score, no AI key needed.
Plus the attention score, the compact call embed and its More info button. SYNTHETIC data only."""
from __future__ import annotations

import asyncio
import time

from conftest import MINT, MemoryPublisher, addr
from northstar import early_calls as EC
from northstar import runner as RN
from northstar.attention import Attention, parse_telegram_preview
from northstar.config import Settings
from northstar.discord_bot.embeds import MORE_INFO_PREFIX, more_info_embeds, runner_call_embed
from northstar.models import Action
from northstar.research import ResearchOutcome

PASSED = ("mint_authority", "freeze_authority", "token_extensions", "holder_concentration", "creator_holdings",
          "launch_buys", "sniper_holdings", "insider_micro_trades")


def report(**over):
    r = {"mint": MINT, "lifecycle": "PRE_MIGRATION_BONDING_CURVE",
         "identity": {"symbol": "KIMCHI", "name": "Kimchi", "description": "the kimchi coin",
                      "image_url": "https://example.invalid/k.png"},
         "pre_migration": {"market_cap_usd_current": 40_000, "unique_buyers": 30, "curve_complete": False,
                           "socials": {"links": ["https://x.com/kimchi"]}},
         "gates": [{"name": n, "status": "PASS"} for n in PASSED],
         "launch_buys": {"verdict": "ORGANIC", "early_buy_pct": 4.0, "bundle_pct": 1.0, "sniper_held_pct": 2.0},
         "holders": {"top10_adj": 18.0, "creator_pct": 2.0},
         "micro_trades": {"status": "OBSERVED", "insider_micro": False},
         "verdict": {"level": "LOOKS CLEAN", "red": [], "green": [], "unknown": []}}
    r.update(over)
    return r


def radar(db, mint=MINT, mc=40_000, vol=20_000, buys=90, sells=40, age=12, now=None):
    db.upsert_discovery(mint, "curve_radar", 9.0, "Kimchi", "KIMCHI",
                        {"market_cap": mc, "buys_m5": buys, "sells_m5": sells, "vol_m5": vol, "chg_m5": 25.0,
                         "liq": 14_000, "age_min": age, "image_url": None, "seen_at": now or time.time()})


def caller(db, attention=None, **kw):
    s = Settings(_env_file=None, discord_channel_id=111, discord_free_channel_id=222, **kw)
    pub = MemoryPublisher()
    c = EC.EarlyCaller(s, db, pub, None, None)
    c.attention = attention
    return s, pub, c


def out(r):
    return ResearchOutcome(r["mint"], Action.WATCHLIST, r, pair=None)


# ----------------------------------------------------------------------------- runner rules
def test_radar_metrics_are_read_fresh_only(db):
    radar(db)
    m = RN.radar_metrics(db, MINT)
    assert m["market_cap"] == 40_000 and m["vol_m5"] == 20_000
    assert RN.radar_metrics(db, MINT, now=time.time() + 3600) is None      # stale
    assert RN.radar_metrics(db, addr("never")) is None


def test_blockers_name_every_reason(db):
    s = Settings(_env_file=None)
    m = {"market_cap": 40_000, "vol_m5": 20_000, "buys_m5": 90, "sells_m5": 40, "age_min": 12}
    assert RN.blockers(report(), m, s) == []
    assert RN.blockers(report(), None, s) == ["no fresh curve-radar numbers (volume, buys) for this coin"]
    bad = RN.blockers(report(), {**m, "market_cap": 200_000, "vol_m5": 1_000, "sells_m5": 200, "age_min": 90}, s)
    assert any("outside" in x for x in bad) and any("min old" in x for x in bad)
    assert any("5-min volume" in x for x in bad) and any("more sells" in x for x in bad)
    # unknown is not a pass: a required check that did not run blocks the call
    r = report()
    r["gates"] = [g for g in r["gates"] if g["name"] != "launch_buys"]
    assert any("launch buys not passed" in x for x in RN.blockers(r, m, s))
    r = report()
    r["gates"].append({"name": "wallet_network", "status": "FAIL", "reason": "linked group holds 20%"})
    assert any("linked group" in x for x in RN.blockers(r, m, s))
    assert any("launch chart" in x for x in RN.blockers(report(launch={"verdict": "INSTANT_JUMP",
                                                                       "reasons": ["jumped"]}), m, s))
    assert "rug check RUG LIKELY" in RN.blockers(report(verdict={"level": "RUG LIKELY"}), m, s)


def test_score_parts_and_attention_weight():
    m = {"market_cap": 40_000, "vol_m5": 24_000, "buys_m5": 90, "sells_m5": 40, "chg_m5": 10}
    a = RN.score(report(), m, {"score": 0})
    b = RN.score(report(), m, {"score": 100})
    assert a["momentum"] == 45.0 and b["attention"] == 25.0 and b["score"] == a["score"] + 25
    assert 0 < a["safety"] <= 30
    weak = RN.score(report(), {**m, "vol_m5": 2_000, "buys_m5": 10, "sells_m5": 20, "chg_m5": -5}, {"score": 0})
    assert weak["momentum"] < 15


# ----------------------------------------------------------------------------- the caller
def test_runner_call_posts_compact_embed_without_any_ai(db):
    radar(db)
    s, pub, c = caller(db)
    row = asyncio.run(c.consider(out(report())))
    assert row and row["entry_mcap"] == 40_000 and pub.sent[0][0] == 111       # VIP only
    e = pub.sent[0][1]
    assert e["title"].startswith("🎯 NORTHSTAR CALL") and "$KIMCHI" in e["title"]
    assert "**Market Cap**: $40.0K" in e["description"] and "**Score**:" in e["description"]
    assert e["thumbnail"]["url"] == "https://example.invalid/k.png"
    assert e["_buttons"][0] == {"label": "More info", "custom_id": f"{MORE_INFO_PREFIX}{MINT}"}
    assert not [x for x in pub.sent if x[0] == 222]                             # free gets proof only
    assert asyncio.run(c.consider(out(report()))) is None                       # never twice


def test_runner_not_called_when_a_check_fails_or_score_is_low(db):
    radar(db)
    s, pub, c = caller(db)
    r = report()
    r["gates"][0]["status"] = "UNKNOWN"
    assert asyncio.run(c.consider(out(r))) is None
    assert r["runner"]["called"] is False and "mint authority not passed" in r["runner"]["blockers"][0]
    s, pub, c = caller(db, runner_min_score=99)
    r = report()
    assert asyncio.run(c.consider(out(r))) is None and any("runner score" in x for x in r["runner"]["blockers"])
    assert pub.sent == []


def test_runner_hourly_cap(db):
    s, pub, c = caller(db, runner_max_per_hour=1)
    radar(db)
    assert asyncio.run(c.consider(out(report())))
    other = addr("second")
    radar(db, mint=other)
    r = report(mint=other)
    assert asyncio.run(c.consider(out(r))) is None and "hourly cap" in r["runner"]["blockers"][-1]


def test_more_info_rebuilds_the_full_research():
    r = report()
    r["runner"] = {"score": 70, "metrics": {"market_cap": 40_000}, "blockers": [], "called": True}
    embeds = more_info_embeds(r)
    assert embeds and all(isinstance(e, dict) and e.get("title") for e in embeds)
    e = runner_call_embed(r, {"entry_mcap": 40_000, "current_mcap": 90_000, "peak_mcap": 100_000,
                              "sent_at": time.time()})
    assert "2.25x" in e["description"] and "**Peak**: $100.0K (2.50x)" in e["description"]


# ----------------------------------------------------------------------------- attention
PAGE = """
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message_text js-message_text">
New gem $KIMCHI <a href="https://pump.fun/coin/{mint}">{mint}</a> aping</div>
<time datetime="{iso}" class="time">12:00</time></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message_text">gm &amp; $SOL news</div></div>
"""


def page(mint=MINT, ts=None):
    iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(ts or time.time()))
    return PAGE.replace("{mint}", mint).replace("{iso}", iso)


def test_telegram_preview_parsing():
    msgs = parse_telegram_preview(page())
    assert len(msgs) == 2
    assert MINT in msgs[0]["mints"] and msgs[0]["tickers"] == ["KIMCHI"] and msgs[0]["ts"]
    assert msgs[1]["tickers"] == ["SOL"] and "gm & $SOL" in msgs[1]["text"]


def test_attention_counts_kol_channels_wallets_and_news(db):
    pages = {"https://t.me/s/alpha_calls": page(), "https://t.me/s/beta_calls": page()}

    async def fetch(url):
        if url in pages:
            return pages[url]
        if "coindesk" in url:
            return "<rss><channel><item><title>Kimchi coin trends after K-drama star launch</title></item></channel></rss>"
        return None

    s = Settings(_env_file=None, kol_telegram_channels="@alpha_calls, https://t.me/beta_calls, bad name!")
    a = Attention(s, fetch=fetch)
    assert a.channels() == ["alpha_calls", "beta_calls"]
    asyncio.run(a.refresh())
    assert a.status["kol"]["read"] == 2 and a.status["crypto_news"]["ok"]
    res = a.score(MINT, "Kimchi", "KIMCHI", report={"tracked_wallets": {"holding_count": 3}})
    assert res["parts"]["kol"] == 55 and res["parts"]["wallets"] == 35 and res["parts"]["crypto_news"] == 15
    assert res["score"] == 100.0 and "2 KOL channels" in res["reasons"][0]
    # the ticker alone (a different mint) earns less than the mint itself
    assert a.score(addr("other"), "Kimchi", "KIMCHI")["parts"]["kol"] == 15
    assert a.score(addr("other"), "Plain Dog", "PDOG")["score"] == 0
    assert a.snapshot()["kol_mints"][0]["mint"] == MINT


def test_old_kol_posts_are_forgotten(db):
    async def fetch(url):
        return page(ts=time.time() - 10 * 3600) if "t.me" in url else None

    a = Attention(Settings(_env_file=None, kol_telegram_channels="alpha_calls"), fetch=fetch)
    asyncio.run(a.refresh())
    assert a.kol_mints == {} and a.score(MINT, "Kimchi", "KIMCHI")["score"] == 0


def test_attention_never_raises():
    class Broken:
        def related(self, *a):
            raise RuntimeError("boom")
    a = Attention(Settings(_env_file=None), events=Broken(), fetch=lambda u: None)
    assert a.score(MINT, "Kimchi", "KIMCHI")["score"] == 0


# ----------------------------------------------------------------------------- v3.2.2
def test_a_typical_safe_active_coin_with_no_attention_clears_the_default_score():
    """No KOL channel set means attention is usually 0. A safe coin with ordinary momentum
    (25% of its market cap traded in 5 min, 4 buys for 3 sells) must still be callable."""
    m = {"market_cap": 40_000, "vol_m5": 10_000, "buys_m5": 80, "sells_m5": 60, "chg_m5": 5}
    sc = RN.score(report(), m, {"score": 0})
    assert sc["score"] >= Settings(_env_file=None).runner_min_score
    weak = RN.score(report(), {**m, "vol_m5": 7_500, "market_cap": 85_000, "buys_m5": 60, "sells_m5": 60,
                               "chg_m5": -10}, {"score": 0})
    assert weak["score"] < Settings(_env_file=None).runner_min_score


def test_runner_summary_counts_candidates_and_names_the_blockers(db):
    radar(db)
    s, pub, c = caller(db)
    r = report()
    r["gates"][0]["status"] = "UNKNOWN"
    asyncio.run(c.consider(out(r)))
    asyncio.run(c.consider(out(report())))
    line = c.runner_summary()
    assert line.startswith("runner check: 2 coin(s) in the band, 1 called")
    assert "mint authority not passed x1" in line
    assert c.runner_summary() is None                        # reset after each report


def test_dashboard_counts_and_lists_runner_calls(db):
    import json as _json
    from test_dashboard import serve
    radar(db)
    s, pub, c = caller(db)
    asyncio.run(c.consider(out(report())))
    srv, cl = serve(db, "pw")
    cl.req("/api/login", {"password": "pw"})
    st = _json.loads(cl.req("/api/status")[1])
    assert st["calls_24h"] == 1 and st["open_calls"] == 1
    calls = _json.loads(cl.req("/api/calls")[1])["calls"]
    assert calls[0]["call_type"] == "RUNNER" and calls[0]["symbol"] == "KIMCHI"
    srv.server.shutdown()


# ----------------------------------------------------------------------------- v3.4.0 recheck or drop
def blocked(**over):
    r = report(**over)
    r["runner"] = {"score": 70, "metrics": {"market_cap": 40_000, "age_min": 12}, "blockers": ["x"], "called": False}
    return r


def test_follow_up_rechecks_a_high_scorer_a_few_times_then_drops_it(db):
    s, pub, c = caller(db, runner_max_rechecks=3)
    r = blocked()
    r["gates"].append({"name": "holder_concentration", "status": "FAIL", "reason": "top-10 30%"})  # can change
    r["runner"]["blockers"] = ["holder concentration not passed: top-10 30%"]
    for i in range(3):
        kind, why = c.follow_up(MINT, r)
        assert kind == "RECHECK" and f"recheck {i + 1} of 3" in why
    kind, why = c.follow_up(MINT, r)
    assert kind == "DROP" and "after 3 recheck" in why
    assert c.runner_stats["rechecks"] == 3 and c.runner_stats["dropped"] == 1


def test_follow_up_drops_low_scorers_and_coins_that_can_never_pass(db):
    s, pub, c = caller(db)
    low = blocked()
    low["runner"]["score"] = 30
    assert c.follow_up(addr("low"), low)[0] == "DROP"
    bundled = blocked()
    bundled["gates"] = [g for g in bundled["gates"] if g["name"] != "launch_buys"] + [
        {"name": "launch_buys", "status": "FAIL", "reason": "9 wallet(s) bought 20.0% in the creation block"}]
    kind, why = c.follow_up(addr("bund"), bundled)
    assert kind == "DROP" and "can never pass: launch buys" in why
    jumped = blocked(launch={"verdict": "INSTANT_JUMP", "reasons": ["launch jumped 7.2x"]})
    assert "launch chart" in c.follow_up(addr("jump"), jumped)[1]
    called = blocked()
    called["runner"]["called"] = True
    assert c.follow_up(addr("ok"), called) is None
    assert c.follow_up(addr("none"), report()) is None             # never reached the runner check


async def test_engine_drops_or_reschedules_and_the_radar_never_brings_a_dropped_coin_back(settings, db):
    from conftest import MemoryPublisher as MP, make_pair, strong_providers
    from northstar.engine import Engine
    eng = Engine(settings, db, strong_providers(), MP())
    hi, bund = addr("hiscore"), addr("bundled")
    for m in (hi, bund):
        db.upsert_discovery(m, "pumpportal_new", 1.0, "C", "C", {})

    async def fake_research(mint, cand=None, force=False):
        r = blocked(mint=mint)
        if mint == bund:
            r["gates"] = [g for g in r["gates"] if g["name"] != "launch_buys"] + [
                {"name": "launch_buys", "status": "FAIL", "reason": "bundled"}]
        else:
            r["gates"].append({"name": "sniper_holdings", "status": "FAIL", "reason": "snipers hold 20%"})
        return ResearchOutcome(mint, Action.REJECT, r, pair=None)

    async def no_call(out, now=None, sources=None):
        return None
    eng.researcher.research = fake_research
    eng.early.consider = no_call
    for m in (hi, bund):
        await eng.process(m)
    a, b = db.get_candidate(hi), db.get_candidate(bund)
    assert a["state"] == "WATCHLIST" and a["next_research_at"] > time.time() + 100
    assert b["state"] == "DROPPED" and b["next_research_at"] is None
    assert "dropped from tracking: can never pass" in b["reasons"]

    class Dex:
        async def tokens_batch(self, mints):
            return [make_pair(base_mint=m, dex_id="pumpfun", market_cap=40_000.0, fdv=40_000.0,
                              vol={"m5": 20_000.0, "h1": 1.0, "h6": 1.0, "h24": 1.0},
                              txns={"m5": {"buys": 90, "sells": 30}, "h1": {"buys": 1, "sells": 1}})
                    for m in mints]
    found = await EC.CurveRadar(settings, db, Dex()).run_once()
    assert bund not in found and hi in found
    assert db.retrack(bund) and db.get_candidate(bund)["state"] == "DISCOVERED"     # undo from the panel


async def test_radar_scans_only_launches_young_enough_to_call(db):
    """v3.4.2: every 30 s over the last hour (was every 2 min over 3 h): coins over 45 min old can't
    be runner-called, and young coins cross the $15K-$90K band in under 2 minutes."""
    from conftest import make_pair
    s = Settings(_env_file=None)
    assert s.curve_radar_seconds == 30 and s.curve_radar_hours == 1.0
    young, old = addr("young"), addr("old2h")
    for m in (young, old):
        db.upsert_discovery(m, "pumpportal_new", 1.0, "C", "C", {})
    db.execute("UPDATE candidates SET discovered_at=? WHERE mint=?", (time.time() - 2 * 3600, old))

    class Dex:
        async def tokens_batch(self, mints):
            return [make_pair(base_mint=m, dex_id="pumpfun", market_cap=40_000.0, fdv=40_000.0,
                              vol={"m5": 20_000.0, "h1": 1.0, "h6": 1.0, "h24": 1.0},
                              txns={"m5": {"buys": 90, "sells": 30}, "h1": {"buys": 1, "sells": 1}}) for m in mints]
    found = await EC.CurveRadar(s, db, Dex()).run_once()
    assert found == [young]
