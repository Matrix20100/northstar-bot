"""What the market and the world are paying attention to, as a score (v3.0.0).

Before v3 the live attention feed (events.py: launch waves, news, Google Trends, Wikipedia,
CoinGecko) only fed the AI judge, and with no AI key set nothing used it at all. Now every coin
gets an attention score from 0 to 100 with plain reasons, and it counts in the call score.

Sources (all free; each optional):
  * launch waves       a word suddenly in many pump.fun launches by different creators (events.py)
  * crypto trending    coins trending on CoinGecko, pools trending on GeckoTerminal (the radar)
  * news               Google News (top, world, tech, entertainment) and crypto news RSS
                       (CoinDesk, Cointelegraph, Decrypt), Google Trends, Wikipedia most read
  * KOL Telegram       public channels listed in NORTHSTAR_KOL_TELEGRAM_CHANNELS, read from their
                       public web preview (t.me/s/<channel>): which mints and $TICKERs they post
  * tracked wallets    how many of the operator's tracked wallets hold the coin (research report)
  * promotion          a DexScreener profile, boost or community takeover

The points per source are first settings, not measured: the curve watch stores each coin's
attention score at entry, so `python main.py curve` shows whether attention-led coins ran more.
"""
from __future__ import annotations

import html
import logging
import re
import time
from collections import Counter
from typing import Optional

from .events import _rss_titles, words

log = logging.getLogger("northstar.attention")

CRYPTO_NEWS = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
    "decrypt": "https://decrypt.co/feed",
}
MINT_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
TICKER_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,14})\b")
TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')
MSG_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)

POINTS = {
    "kol_one": 35, "kol_more": 55,              # mint posted by 1 / 2+ KOL channels
    "kol_ticker": 15,                           # its $TICKER posted by a KOL channel
    "wallets_one": 20, "wallets_more": 35,      # 1 / 3+ tracked wallets hold it
    "wave": 30,                                 # a live launch wave (scaled by its strength)
    "crypto_trending": 20,                      # matches a trending coin / pool name
    "news": 15, "trends": 20, "wiki": 10, "crypto_news": 15,
    "promotion": 10, "socials": 5,
}


def _plain(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))


def parse_telegram_preview(page: str) -> list[dict]:
    """Messages from a public t.me/s/<channel> page: [{"text", "mints", "tickers", "ts"}]."""
    out = []
    parts = page.split('class="tgme_widget_message_wrap')
    for part in parts[1:]:
        m = MSG_RE.search(part)
        if not m:
            continue
        text = _plain(m.group(1))
        ts = None
        t = TIME_RE.search(part)
        if t:
            try:
                from datetime import datetime
                ts = datetime.fromisoformat(t.group(1).replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = None
        mints = {x for x in MINT_RE.findall(text + " " + part) if len(x) >= 32}
        out.append({"text": text[:400], "mints": sorted(mints), "tickers": sorted({x.upper() for x in TICKER_RE.findall(text)}),
                    "ts": ts})
    return out


class Attention:
    def __init__(self, s, events=None, radar=None, fetch=None):
        self.s, self.events, self.radar = s, events, radar
        self._fetch = fetch or (events._get if events is not None else None)
        self.crypto_news: list[dict] = []
        self.kol_mints: dict[str, dict] = {}      # mint -> {"channels": set, "first": ts, "last": ts}
        self.kol_tickers: dict[str, dict] = {}    # TICKER -> {"channels": set, "last": ts}
        self.status: dict[str, dict] = {}
        self._last: dict[str, float] = {}

    def channels(self) -> list[str]:
        raw = str(getattr(self.s, "kol_telegram_channels", "") or "")
        out = []
        for c in raw.replace("\n", ",").split(","):
            c = c.strip().lstrip("@")
            c = c.split("t.me/")[-1].split("/")[0] if "t.me/" in c else c
            if re.fullmatch(r"[A-Za-z0-9_]{4,64}", c):
                out.append(c)
        return out[:40]

    async def refresh(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        if self._fetch is None:
            return
        if getattr(self.s, "attention_crypto_news", True) and now - self._last.get("crypto_news", 0) >= 600:
            self._last["crypto_news"] = now
            items = []
            for name, url in CRYPTO_NEWS.items():
                text = await self._fetch(url)
                if text:
                    items += [{**x, "source": name} for x in _rss_titles(text, 30)]
            if items:
                self.crypto_news = items[:90]
            self.status["crypto_news"] = {"ok": bool(items), "items": len(self.crypto_news), "at": now}
        chans = self.channels()
        if chans and now - self._last.get("kol", 0) >= float(getattr(self.s, "kol_refresh_seconds", 120)):
            self._last["kol"] = now
            ok = 0
            horizon = now - float(getattr(self.s, "kol_memory_hours", 6.0)) * 3600
            for ch in chans:
                page = await self._fetch(f"https://t.me/s/{ch}")
                if not page:
                    continue
                ok += 1
                for msg in parse_telegram_preview(page):
                    ts = msg["ts"] or now
                    if ts < horizon:
                        continue
                    for mint in msg["mints"]:
                        e = self.kol_mints.setdefault(mint, {"channels": set(), "first": ts, "last": ts})
                        e["channels"].add(ch)
                        e["first"], e["last"] = min(e["first"], ts), max(e["last"], ts)
                    for t in msg["tickers"]:
                        e = self.kol_tickers.setdefault(t, {"channels": set(), "last": ts})
                        e["channels"].add(ch)
                        e["last"] = max(e["last"], ts)
            self.kol_mints = {k: v for k, v in self.kol_mints.items() if v["last"] >= horizon}
            self.kol_tickers = {k: v for k, v in self.kol_tickers.items() if v["last"] >= horizon}
            self.status["kol"] = {"ok": ok > 0, "channels": len(chans), "read": ok, "mints": len(self.kol_mints),
                                  "at": now}

    # ------------------------------------------------------------------ scoring
    def _trending_names(self) -> list[str]:
        names = []
        ev = self.events
        if ev is not None:
            names += [f"{c.get('symbol', '')} {c.get('name', '')}" for c in getattr(ev, "cg_coins", []) or []]
        tr = getattr(self.radar, "trending", None) if self.radar is not None else None
        for t in (tr or {}).values():
            if isinstance(t, dict):
                names.append(f"{t.get('symbol') or ''} {t.get('name') or ''}")
        return names

    def score(self, mint: str, name: Optional[str], symbol: Optional[str], description: Optional[str] = None,
              report: Optional[dict] = None, sources: Optional[list] = None) -> dict:
        """{"score": 0-100, "reasons": [...], "parts": {source: points}}. Never raises."""
        parts: dict[str, float] = {}
        reasons: list[str] = []
        try:
            coin = words(name, symbol, (description or "")[:300])
            k = self.kol_mints.get(mint)
            if k:
                n = len(k["channels"])
                parts["kol"] = POINTS["kol_more"] if n >= 2 else POINTS["kol_one"]
                reasons.append(f"posted by {n} KOL channel{'s' if n != 1 else ''}: " + ", ".join(sorted(k["channels"])[:3]))
            elif symbol and symbol.upper() in self.kol_tickers:
                parts["kol"] = POINTS["kol_ticker"]
                reasons.append(f"${symbol.upper()} mentioned by " + ", ".join(sorted(self.kol_tickers[symbol.upper()]["channels"])[:3]))
            held = ((report or {}).get("tracked_wallets") or {}).get("holding_count") or 0
            if held:
                parts["wallets"] = POINTS["wallets_more"] if held >= 3 else POINTS["wallets_one"]
                reasons.append(f"{held} of your tracked wallets hold it")
            ev = self.events
            if ev is not None and coin:
                rel = ev.related(name, symbol, description)
                if rel.get("waves"):
                    w = rel["waves"][0]
                    strength = min(1.0, (w.get("lift") or 3) / 8) * min(1.0, (w.get("creators") or 3) / 6)
                    parts["wave"] = round(POINTS["wave"] * max(0.5, strength), 1)
                    reasons.append(f"launch wave \"{w['term']}\": {w['launches_30m']} launches by "
                                   f"{w['creators']} creators in 30 min")
                for key, label in (("trends", "Google Trends"), ("news", "in the news"), ("wiki", "Wikipedia most read")):
                    if rel.get(key):
                        parts[key] = POINTS[key]
                        reasons.append(f"{label}: {rel[key][0][:80]}")
                hot = [n for n in self._trending_names() if coin & words(n)]
                if hot:
                    parts["crypto_trending"] = POINTS["crypto_trending"]
                    reasons.append(f"matches trending: {hot[0].strip()[:40]}")
            if coin:
                cn = [x["title"] for x in self.crypto_news if coin & words(x["title"])]
                if cn:
                    parts["crypto_news"] = POINTS["crypto_news"]
                    reasons.append(f"crypto news: {cn[0][:80]}")
            src = " ".join(json_list(sources))
            if any(x in src for x in ("dexscreener_boost", "dexscreener_cto", "dexscreener_profile")):
                parts["promotion"] = POINTS["promotion"]
                reasons.append("DexScreener profile / boost / takeover")
            soc = ((report or {}).get("pre_migration") or {}).get("socials") or {}
            if soc.get("links"):
                parts["socials"] = POINTS["socials"]
        except Exception:  # noqa: BLE001 - attention is evidence, never a crash
            log.debug("attention score failed for %s", mint, exc_info=True)
        total = round(min(100.0, sum(parts.values())), 1)
        return {"score": total, "reasons": reasons[:5], "parts": parts}

    def quick(self, mint: str, name: Optional[str], symbol: Optional[str]) -> float:
        """Score from name/ticker/KOL only (no research report), for the curve watch."""
        return self.score(mint, name, symbol)["score"]

    def snapshot(self) -> dict:
        top_kol = sorted(self.kol_mints.items(), key=lambda kv: (-len(kv[1]["channels"]), -kv[1]["last"]))[:15]
        return {"crypto_news": [x["title"] for x in self.crypto_news[:20]],
                "kol_mints": [{"mint": m, "channels": sorted(v["channels"]), "first": v["first"]} for m, v in top_kol],
                "kol_tickers": Counter({t: len(v["channels"]) for t, v in self.kol_tickers.items()}).most_common(15),
                "trending": self._trending_names()[:20], "status": self.status}


def json_list(x) -> list:
    if isinstance(x, list):
        return [str(i) for i in x]
    return []
