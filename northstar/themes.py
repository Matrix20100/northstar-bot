"""Narratives: what a coin is about, whether that is live right now, and whether it runs (v4.2.0).

Operator: "When scanning a coin, narrative must be analyzed and understood." Without a paid AI model
the bot reads the narrative three ways, all from data it already has:

  1. THEME   the coin's name, ticker and description are matched against theme word lists
             (animals, politics, celebrities, AI/tech, crypto culture, events/news, food, ...).
  2. LIVE    whether the coin's words are live right now: pump.fun launch waves, news, Google Trends,
             trending coins (attention.py) and the operator's Twitter tracker feed (social_feed.py).
  3. LEARNED every coin that enters the early band is stored with its theme (curve_watch.theme), so
             `theme_stats()` measures, per theme, how often coins hit 2x and how often they fell to
             0.3x over the last week. `theme_points()` turns a theme that beats the band's average into
             attention points (up to +15) and one that does worse into a deduction (down to -10).
             A theme needs `MIN_THEME_COINS` finished coins before it counts.

The word lists are a first setting, kept short and literal; the learned numbers decide what they are
worth. Nothing here claims to understand a joke: it measures what the market did with similar coins.
"""
from __future__ import annotations

import re
import time
from typing import Optional

from . import curve_watch as CW

THEMES: dict[str, tuple[str, ...]] = {
    "animal": ("dog", "doge", "shib", "inu", "cat", "kitty", "frog", "pepe", "monkey", "ape", "chimp", "bear",
               "bull", "pig", "hamster", "penguin", "duck", "goat", "fish", "whale", "shark", "wolf", "fox",
               "bird", "owl", "horse", "cow", "rat", "mouse", "bunny", "rabbit", "tiger", "lion", "panda",
               "otter", "seal", "capybara", "moodeng", "hippo", "gorilla", "snake", "dragon"),
    "politics": ("trump", "maga", "biden", "kamala", "elon", "musk", "doge", "president", "vote", "election",
                 "congress", "senate", "usa", "america", "patriot", "melania", "barron", "vance", "rfk",
                 "putin", "xi", "milei"),
    "celebrity": ("kanye", "ye", "drake", "taylor", "swift", "mrbeast", "beast", "ronaldo", "messi", "lebron",
                  "kardashian", "tate", "speed", "ishowspeed", "kai", "cenat", "snoop", "rihanna", "diddy"),
    "ai_tech": ("ai", "gpt", "agent", "agents", "bot", "robot", "neural", "llm", "claude", "grok", "openai",
                "deepseek", "quantum", "chip", "nvidia", "tesla", "spacex", "mars", "rocket", "cyber", "meta",
                "apple", "google"),
    "crypto_culture": ("moon", "pump", "wagmi", "gm", "ngmi", "degen", "chad", "based", "giga", "sigma", "wojak",
                       "jeet", "hodl", "lambo", "sol", "solana", "bonk", "wif", "fartcoin", "meme", "memecoin"),
    "event_news": ("war", "strike", "crash", "fed", "rate", "tariff", "storm", "hurricane", "election", "olympics",
                   "worldcup", "superbowl", "halloween", "christmas", "thanksgiving", "nfl", "nba", "ufc"),
    "food": ("pizza", "burger", "taco", "sushi", "kimchi", "ramen", "coffee", "tea", "bread", "cheese", "banana",
             "apple", "cookie", "candy", "donut", "beer", "wine", "milk"),
    "culture_meme": ("skibidi", "rizz", "gyatt", "sigma", "npc", "brainrot", "ohio", "aura", "slay", "goon",
                     "chill", "vibe", "lol", "lmao", "bruh", "cope", "seethe"),
}
MIN_THEME_COINS = 15
WORD_RE = re.compile(r"[a-z][a-z0-9]{1,20}")


def words(*texts) -> set[str]:
    out: set[str] = set()
    for t in texts:
        if t:
            out |= set(WORD_RE.findall(str(t).lower()))
    return out


def classify(name: Optional[str], symbol: Optional[str], description: Optional[str] = None) -> str:
    """The best-matching theme ("other" when nothing matches). Name and ticker count double."""
    head = words(name, symbol)
    body = words((description or "")[:300])
    best, score = "other", 0
    for theme, vocab in THEMES.items():
        v = set(vocab)
        sc = 2 * len(head & v) + len(body & v)
        # also catch joined tickers like BABYPEPE / TRUMPCAT
        joined = (str(symbol or "") + " " + str(name or "")).lower()
        sc += sum(1 for w in vocab if len(w) >= 4 and w in joined and w not in head)
        if sc > score:
            best, score = theme, sc
    return best


def theme_stats(db, days: float = 7.0, now: Optional[float] = None) -> dict:
    """Per theme over finished early-band coins: {"n", "hit_2x", "fell_0_3x"} plus the band average."""
    now = now or time.time()
    try:
        cols = {r["name"] for r in db.fetchall("PRAGMA table_info(curve_watch)")}
        if "theme" not in cols:
            return {"all": {"n": 0}, "themes": {}}
        keep = "AND COALESCE(excluded, 0) = 0 " if "excluded" in cols else ""
        rows = db.fetchall(f"SELECT theme, symbol, entry_mc, peak_mc, low_mc FROM curve_watch WHERE done = 1 {keep}"
                           "AND entered_at > ? AND entry_mc > 0", (now - days * 86400,))
    except Exception:  # noqa: BLE001
        return {"all": {"n": 0}, "themes": {}}

    def rates(rs):
        n = len(rs)
        if not n:
            return {"n": 0}
        return {"n": n, "hit_2x": round(100 * sum((r["peak_mc"] or 0) / r["entry_mc"] >= 2 for r in rs) / n, 1),
                "fell_0_3x": round(100 * sum(CW.fell(r) for r in rs) / n, 1)}

    by: dict[str, list] = {}
    for r in rows:
        # coins recorded before v4.2.0 have no stored theme: classify them from their ticker now
        by.setdefault(r["theme"] or classify(None, r.get("symbol")), []).append(r)
    return {"all": rates(rows), "themes": {t: rates(rs) for t, rs in sorted(by.items(), key=lambda kv: -len(kv[1]))}}


_cache: dict = {"at": 0.0, "stats": None}


def theme_points(db, theme: str, now: Optional[float] = None) -> tuple[float, Optional[str]]:
    """Attention points for a theme from its measured week (cached 10 min): (points, reason)."""
    now = now or time.time()
    if _cache["stats"] is None or now - _cache["at"] > 600:
        _cache.update(at=now, stats=theme_stats(db, 7.0, now))
    st = _cache["stats"]
    t, base = st["themes"].get(theme) or {}, st["all"]
    if not t or t.get("n", 0) < MIN_THEME_COINS or not base.get("n"):
        return 0.0, None
    lift = (t["hit_2x"] - base["hit_2x"]) - 0.5 * (t["fell_0_3x"] - base["fell_0_3x"])
    pts = max(-10.0, min(15.0, lift * 0.75))
    if abs(pts) < 2:
        return 0.0, None
    label = theme.replace("_", " ")
    return round(pts, 1), (f"{label} coins this week: {t['hit_2x']:.0f}% hit 2x, {t['fell_0_3x']:.0f}% fell to 0.3x "
                           f"(all coins {base['hit_2x']:.0f}% / {base['fell_0_3x']:.0f}%, {t['n']} {label} coins)")
