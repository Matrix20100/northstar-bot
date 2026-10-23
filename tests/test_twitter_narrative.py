"""v4.2.0: J7 / uxento tracker feed from Discord; narrative themes learned from outcomes. SYNTHETIC."""
from __future__ import annotations

import asyncio
import json
import time

from conftest import MINT, MemoryPublisher, addr, make_pair, strong_providers
from northstar import social_feed as SF
from northstar import themes as TH
from northstar.config import Settings

CH = "123456789012345678"


def tweet(i, text="", embed=None):
    m = {"id": str(1000 + i), "content": text}
    if embed:
        m["embeds"] = [embed]
    return m


J7 = {"author": {"name": "Ansem (@blknoiz06)", "url": "https://x.com/blknoiz06"},
      "description": f"new kimchi coin is cooking $KIMCHI {MINT} https://x.com/blknoiz06/status/1"}


def test_parse_reads_text_and_embeds():
    p = SF.parse(tweet(1, "", J7))
    assert p["mints"] == [MINT] and p["tickers"] == ["KIMCHI"] and p["account"] == "blknoiz06"
    assert "kimchi" in p["words"] and "https" not in p["words"]
    assert SF.parse(tweet(2, "gm frens"))["mints"] == []


class FakeDiscord:
    def __init__(self, pages):
        self.pages, self.calls = list(pages), []

    async def __call__(self, url, headers):
        self.calls.append(url)
        assert headers["Authorization"].startswith("Bot ")
        return self.pages.pop(0) if self.pages else (200, [])


def feed(db, pages, **kw):
    s = Settings(_env_file=None, twitter_feed_channel_ids=CH, **kw)
    return SF.TwitterFeed(s, db, "token-not-real", fetch=FakeDiscord(pages))


def test_first_read_starts_now_then_new_posts_are_stored_and_signal(db):
    f = feed(db, [(200, [tweet(1, "old post")]), (200, [tweet(2, "", J7), tweet(3, "$KIMCHI again")])])
    assert asyncio.run(f.poll()) == []                          # history is never replayed
    sigs = asyncio.run(f.poll())
    assert sigs == [{"mint": MINT, "account": "blknoiz06", "why": "@blknoiz06 posted its contract address"}]
    assert "after=1001" in f._fetch.calls[1]
    assert db.fetchone("SELECT COUNT(*) n FROM social_mentions")["n"] == 2
    tr = SF.trending(db, time.time())
    assert tr["mints"][0] == (MINT, 1) and ("KIMCHI", 2) in tr["tickers"]
    a = SF.coin_attention(db, MINT, "KIMCHI", {"kimchi"}, time.time())
    assert a["ca_posts"] == 1 and a["accounts"] == ["blknoiz06"] and a["ticker_posts"] == 2


def test_feed_reports_access_problems_and_missing_message_content(db):
    f = feed(db, [(403, {"message": "Missing Access"})])
    asyncio.run(f.poll())
    assert f.status[CH]["ok"] is False and "View Channel" in f.status[CH]["error"]
    g = feed(db, [(200, [tweet(1, "x")]), (200, [tweet(2, ""), tweet(3, "")])])
    asyncio.run(g.poll())
    asyncio.run(g.poll())
    assert g.status[CH]["empty_text"] == 2 and g.status[CH]["read"] == 2


def test_attention_counts_the_twitter_feed(db):
    from northstar.attention import Attention
    f = feed(db, [(200, [tweet(1, "x")]), (200, [tweet(2, "", J7), tweet(3, "kimchi kimchi everywhere"),
                                               tweet(4, "who has kimchi")])])
    asyncio.run(f.poll())
    asyncio.run(f.poll())
    a = Attention(Settings(_env_file=None))
    a.db = db
    res = a.score(MINT, "Kimchi", "KIMCHI")
    assert res["parts"]["twitter"] == 35 and res["parts"]["twitter_terms"] == 20
    assert res["reasons"][0] == "contract posted on Twitter by @blknoiz06"
    assert res["theme"] == "food"


def test_themes_classify():
    assert TH.classify("Baby Pepe", "BPEPE") == "animal"
    assert TH.classify("Trump 2028", "TRUMP28") == "politics"
    assert TH.classify("Grok Agent", "GROKAI") == "ai_tech"
    assert TH.classify("Skibidi Rizz", "RIZZ") == "culture_meme"
    assert TH.classify("Qwerty", "QWE") == "other"


def test_theme_points_come_from_measured_outcomes(db):
    from northstar import curve_watch as CW
    CW.init_schema(db)
    now = time.time()
    rows = [("animal", 3.0, 0.9)] * 20 + [("other", 1.1, 0.2)] * 40     # animals ran, the rest rugged
    for i, (theme, peak, low) in enumerate(rows):
        db.execute("INSERT INTO curve_watch(mint, entered_at, entry_mc, peak_mc, low_mc, low_before_peak, last_mc, "
                   "last_ts, done, theme) VALUES (?,?,?,?,?,?,?,?,1,?)",
                   (f"m{i}", now - 6 * 3600, 40_000, 40_000 * peak, 40_000 * low, 40_000 * low, 40_000, now, theme))
    st = TH.theme_stats(db, now=now)
    assert st["themes"]["animal"]["hit_2x"] == 100.0 and st["all"]["n"] == 60
    TH._cache.update(at=0.0, stats=None)
    pts, why = TH.theme_points(db, "animal", now)
    assert pts == 15.0 and "animal coins this week: 100% hit 2x" in why
    pts2, _ = TH.theme_points(db, "other", now)
    assert pts2 < 0
    TH._cache.update(at=0.0, stats=None)


def test_curve_watch_stores_the_theme(db):
    from northstar import curve_watch as CW
    p = make_pair(base_mint=MINT, base_name="Baby Pepe", base_symbol="BPEPE", dex_id="pumpfun",
                  market_cap=30_000.0, fdv=30_000.0, pair_created_at=time.time() - 600)
    CW.observe(db, {MINT: [p]}, time.time(), Settings(_env_file=None))
    assert db.fetchone("SELECT theme FROM curve_watch WHERE mint=?", (MINT,))["theme"] == "animal"


async def test_a_contract_posted_in_the_feed_is_researched_now(settings, db):
    from northstar.engine import Engine
    settings.twitter_feed_channel_ids = CH
    settings.discord_token = "token-not-real"
    p = strong_providers()

    async def tokens_batch(mints):
        return [make_pair(base_mint=m, dex_id="pumpfun", market_cap=40_000.0, fdv=40_000.0) for m in mints]
    p.dex.tokens_batch = tokens_batch
    eng = Engine(settings, db, p, MemoryPublisher())
    assert eng.twitter is not None and eng.twitter.enabled()
    eng.twitter._fetch = FakeDiscord([(200, [tweet(1, "x")]), (200, [tweet(2, "", J7)])])
    researched = []

    async def fake_research(mint):
        researched.append(mint)
    eng._radar_research = fake_research
    await eng._twitter_feed()
    await eng._twitter_feed()
    await asyncio.sleep(0.01)
    assert researched == [MINT] and eng.stats["twitter_signals"] == 1
    assert "twitter_feed" in db.get_candidate(MINT)["sources"]


def test_dashboard_social_and_narratives(db):
    from test_dashboard import serve
    f = feed(db, [(200, [tweet(1, "x")]), (200, [tweet(2, "", J7)])])
    asyncio.run(f.poll())
    asyncio.run(f.poll())
    srv, c = serve(db, "pw")
    c.req("/api/login", {"password": "pw"})
    d = json.loads(c.req("/api/social")[1])
    assert d["trending"]["mints"][0][0] == MINT and d["min_theme_coins"] == TH.MIN_THEME_COINS
    srv.server.shutdown()
