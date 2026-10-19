"""Live attention feed: what is the world (and pump.fun) paying attention to RIGHT NOW?

Four free sources, each optional and refreshed on its own timer:

  1. LAUNCH WAVES  - the pump.fun launch stream the bot already receives. When an event goes viral,
     dozens of coins get named after it within minutes ("ROADSTER", "STARMAN"...). Words in launch
     names/tickers in the last 30 minutes are compared with the 6 hours before: a word that suddenly
     appears in many launches is a live narrative, whatever caused it.
  2. NEWS          - Google News RSS (top stories + world, technology, entertainment).
  3. TRENDS        - Google Trends "trending now" RSS (what people are searching).
  4. WIKIPEDIA     - most-read English Wikipedia articles (viral people, events, memes).
  5. COINGECKO     - which crypto NARRATIVES are hot: trending searches and the sectors with the
     biggest 24h market-cap moves ("AI agents", "memecoins", "DePIN"). Free with the Demo key the
     bot already uses. This is crypto-sector attention; it complements the real-world feeds above
     rather than replacing them (CoinGecko will not tell you a Tesla Roadster just flew).

``snapshot()`` returns everything for the AI judge and the dashboard; ``related(name, symbol,
description)`` finds which live events a coin's name/description overlaps with. Nothing here
decides a call on its own: it is context for the AI narrative judge.
"""
from __future__ import annotations

import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Optional

import httpx

log = logging.getLogger("northstar.events")

UA = {"User-Agent": "NorthstarBot/2.4 (memecoin research; contact via Discord)"}
# Wikimedia's REST API returns 403 to a User-Agent without a contact URL, per their policy:
# https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy
WIKI_UA = {"User-Agent": "NorthstarBot/2.4 (+https://github.com/northstar-bot; research tool) httpx",
           "Accept": "application/json"}
NEWS_FEEDS = {
    "top": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
    "world": "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en",
    "tech": "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=en-US&gl=US&ceid=US:en",
    "entertainment": "https://news.google.com/rss/headlines/section/topic/ENTERTAINMENT?hl=en-US&gl=US&ceid=US:en",
}
TRENDS_FEED = "https://trends.google.com/trending/rss?geo=US"
WIKI_FEED = "https://en.wikipedia.org/api/rest_v1/feed/featured/{y}/{m}/{d}"

STOP = set("""the a an of and or to in on for with is are be by at it this that from as coin token sol solana pump
fun inu meme official real new my our your his her we us you me i of de la el le doge pepe cat dog
ai gpt the1 x 1 2 3 one two first last v2 v3 jr baby mini mega super king queen""".split())
WORD = re.compile(r"[a-z][a-z0-9]{2,}")


def words(*texts: Optional[str]) -> set[str]:
    out: set[str] = set()
    for t in texts:
        for w in WORD.findall((t or "").lower()):
            if w not in STOP:
                out.add(w)
    return out


def _rss_titles(xml_text: str, limit: int = 40) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        # Google News titles end with " - Publisher"
        title = re.sub(r"\s+-\s+[^-]{2,60}$", "", html.unescape(title))
        traffic = None
        for child in item:
            if child.tag.endswith("approx_traffic"):
                traffic = (child.text or "").strip()
        out.append({"title": title, "traffic": traffic, "published": (item.findtext("pubDate") or "").strip()})
        if len(out) >= limit:
            break
    return out


class EventFeeds:
    def __init__(self, s, db, transport: Optional[httpx.AsyncBaseTransport] = None, cg_markets=None):
        self.s, self.db = s, db
        self._transport = transport
        self.cg = cg_markets
        self.news: list[dict] = []
        self.trends: list[dict] = []
        self.wiki: list[dict] = []
        self.waves: list[dict] = []
        self.metas: list[dict] = []          # hot crypto sectors (CoinGecko categories)
        self.cg_coins: list[dict] = []       # coins trending on CoinGecko
        self.status: dict[str, dict] = {}
        self._last: dict[str, float] = {}

    async def _get(self, url: str, headers: Optional[dict] = None) -> Optional[str]:
        try:
            async with httpx.AsyncClient(timeout=12, headers=headers or UA, transport=self._transport,
                                         follow_redirects=True) as c:
                r = await c.get(url)
            if r.status_code != 200:
                raise httpx.HTTPError(f"HTTP {r.status_code}")
            return r.text
        except Exception as e:  # noqa: BLE001 - every feed is optional
            log.info("event feed %s failed: %s", url.split("?")[0], e)
            return None

    def _due(self, name: str, every: float, now: float) -> bool:
        if now - self._last.get(name, 0) < every:
            return False
        self._last[name] = now
        return True

    def _mark(self, name: str, ok: bool, n: int, now: float) -> None:
        self.status[name] = {"ok": ok, "items": n, "at": now}

    async def refresh(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        if self._due("waves", 60, now):
            self.waves = self.launch_waves(now)
            self._mark("waves", True, len(self.waves), now)
        if self.s.events_news and self._due("news", self.s.events_news_seconds, now):
            items, ok = [], False
            for cat, url in NEWS_FEEDS.items():
                text = await self._get(url)
                if text:
                    ok = True
                    items += [{**x, "category": cat} for x in _rss_titles(text, 25)]
            if ok:
                seen, news = set(), []
                for x in items:
                    k = x["title"].lower()
                    if k not in seen:
                        seen.add(k)
                        news.append(x)
                self.news = news[:80]
            self._mark("news", ok, len(self.news), now)
        if self.s.events_trends and self._due("trends", self.s.events_news_seconds, now):
            text = await self._get(TRENDS_FEED)
            if text:
                self.trends = _rss_titles(text, 30)
            self._mark("trends", bool(text), len(self.trends), now)
        if self.s.events_wikipedia and self._due("wiki", 3600, now):
            got = None
            for back in (0, 1):
                t = time.gmtime(now - back * 86400)
                text = await self._get(
                    WIKI_FEED.format(y=t.tm_year, m=f"{t.tm_mon:02d}", d=f"{t.tm_mday:02d}"), WIKI_UA)
                if text:
                    try:
                        import json
                        arts = (json.loads(text).get("mostread") or {}).get("articles") or []
                    except ValueError:
                        arts = []
                    if arts:
                        got = [{"title": a.get("normalizedtitle") or a.get("title", "").replace("_", " "),
                                "views": a.get("views"), "about": (a.get("description") or "")[:120]}
                               for a in arts[:40] if a.get("title") not in ("Main_Page",)]
                        break
            if got:
                self.wiki = got
            self._mark("wiki", bool(got), len(self.wiki), now)
        if self.s.events_coingecko and self.cg is not None and self._due("coingecko", 900, now):
            ok = False
            try:
                tr = await self.cg.trending()
                cats = await self.cg.hot_categories()
            except Exception:  # noqa: BLE001 - optional feed
                tr, cats = None, None
            if tr and (tr.get("coins") or tr.get("categories")):
                self.cg_coins = tr["coins"]
                ok = True
            if cats:
                self.metas = cats
                ok = True
            self._mark("coingecko", ok, len(self.metas) + len(self.cg_coins), now)

    # ------------------------------------------------------------------ launch waves
    def launch_waves(self, now: Optional[float] = None, recent_min: float = 30, base_h: float = 6,
                     min_count: int = 4, min_creators: int = 3) -> list[dict]:
        """Words that suddenly appear in many pump.fun launches (30 min vs the 6 h before), made by
        several DIFFERENT creators (one bot spamming 30 copies of its own ticker is not a narrative)."""
        now = now or time.time()
        try:
            base_rows = self.db.fetchall("SELECT name, symbol FROM candidates WHERE discovered_at > ? AND discovered_at <= ?",
                                         (now - base_h * 3600, now - recent_min * 60))
            recent_rows = self.db.fetchall(
                "SELECT c.name, c.symbol, c.mint, d.payload FROM candidates c LEFT JOIN discovery_events d "
                "ON d.mint = c.mint AND d.source = 'pumpportal_new' WHERE c.discovered_at > ? AND c.discovered_at <= ?",
                (now - recent_min * 60, now))
        except Exception:  # noqa: BLE001
            return []
        base = Counter()
        for r in base_rows:
            for w in words(r.get("name"), r.get("symbol")):
                base[w] += 1
        recent, creators, examples = Counter(), {}, {}
        seen_mints = set()
        for r in recent_rows:
            if r["mint"] in seen_mints:
                continue
            seen_mints.add(r["mint"])
            m = re.search(r'"traderPublicKey":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"', r.get("payload") or "")
            creator = m.group(1) if m else r["mint"]
            for w in words(r.get("name"), r.get("symbol")):
                recent[w] += 1
                creators.setdefault(w, set()).add(creator)
                ex = examples.setdefault(w, [])
                sym = f"${r.get('symbol') or '?'}"
                if sym not in ex and len(ex) < 5:
                    ex.append(sym)
        base_windows = max(1.0, (base_h * 60 - recent_min) / recent_min)
        out = []
        for w, n in recent.items():
            nc = len(creators.get(w, ()))
            if n < min_count or nc < min_creators:
                continue
            expected = base[w] / base_windows
            lift = n / max(0.5, expected)
            if lift >= 3:
                out.append({"term": w, "launches_30m": n, "creators": nc, "expected_30m": round(expected, 2),
                            "lift": round(lift, 1), "examples": examples.get(w, [])})
        out.sort(key=lambda x: -(x["lift"] * x["creators"]))
        return out[:25]

    # ------------------------------------------------------------------ matching
    def related(self, name: Optional[str], symbol: Optional[str], description: Optional[str] = None) -> dict:
        """Live events whose words overlap with the coin's name / ticker / description."""
        coin = words(name, symbol, (description or "")[:300])
        if not coin:
            return {"waves": [], "news": [], "trends": [], "wiki": []}
        hit = lambda text: bool(coin & words(text))  # noqa: E731
        return {
            "waves": [w for w in self.waves if w["term"] in coin][:5],
            "news": [n["title"] for n in self.news if hit(n["title"])][:5],
            "trends": [t["title"] for t in self.trends if hit(t["title"])][:5],
            "wiki": [w["title"] for w in self.wiki if hit(w["title"] + " " + (w.get("about") or ""))][:5],
            "metas": [f"{m['name']} {m['change_24h']:+.1f}% (24h)" for m in self.metas if hit(m["name"])][:3],
        }

    def snapshot(self, news: int = 25, trends: int = 20, wiki: int = 20) -> dict:
        return {"metas": [f"{m['name']} {m['change_24h']:+.1f}% (24h market cap)" for m in self.metas[:10]],
                "cg_coins": [f"{c['symbol']} ({c['name']})" for c in self.cg_coins[:10]],
                "waves": self.waves[:15], "news": [n["title"] for n in self.news[:news]],
                "trends": [f"{t['title']}" + (f" ({t['traffic']})" if t.get("traffic") else "") for t in self.trends[:trends]],
                "wiki": [f"{w['title']}" + (f" — {w['about']}" if w.get("about") else "") for w in self.wiki[:wiki]],
                "status": self.status}
