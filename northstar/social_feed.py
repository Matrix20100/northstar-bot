"""Twitter attention from the operator's tracker feed in Discord (v4.2.0).

Operator: "The bot must have twitter trackers like J7 or uxento to understand exactly where all
attention is." J7 / uxento post their alerts (tweets by tracked accounts, contract addresses, tickers)
into a Discord channel the operator already has. Northstar's own Discord bot reads that channel.

  * every `twitter_feed_seconds` (20 s) the channels in `NORTHSTAR_TWITTER_FEED_CHANNEL_IDS` are read
    through Discord's REST API with the bot's token: GET /channels/{id}/messages?after=<last seen>
  * each message (its text, its embeds' title / description / fields / author / url) is parsed for
      - contract addresses (Solana mints),
      - $TICKERs,
      - the X account that posted (an x.com / twitter.com link or the embed author),
      - words, so a term many tweets use shows up as a live Twitter trend;
  * everything is stored in `social_mentions` (kept 3 days) and feeds the attention score
    (attention.py): a mint posted by a tracked account, its $TICKER, or its name matching a word many
    tweets use right now;
  * a contract address posted in the feed is researched at once (it skips the queue, like a radar
    surge). Every safety check still applies.

v4.4.0: Uxento Vision posts reach this channel through the Northstar Vision Forwarder Chrome extension
(tools/vision-forwarder): a webhook embed per post, with the X account in the embed author URL and contract
addresses in the text, which `parse()` already reads.

Needs: the Northstar bot in that Discord server with View Channel + Read Message History on the
channel, and "Message Content Intent" switched on for the bot (Discord Developer Portal -> Bot ->
Privileged Gateway Intents). Without it Discord hands bots empty message text; the feed reports that.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from typing import Callable, Optional

log = logging.getLogger("northstar.social_feed")

API = "https://discord.com/api/v10"
MINT_RE = re.compile(r"(?<![A-Za-z0-9])([1-9A-HJ-NP-Za-km-z]{32,44})(?![A-Za-z0-9])")
TICKER_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,14})\b")
HANDLE_RE = re.compile(r"(?:x|twitter)\.com/(?!i/|search|home|intent)([A-Za-z0-9_]{2,15})", re.I)
AT_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{2,15})\b")
WORD_RE = re.compile(r"[a-z][a-z0-9]{2,20}")
STOP = set("the and for you are with this that from have not but all was will just get has its out new now "
           "can our who what when your more like they them been about into than then also some very only "
           "over https http www com status twitter tweet tweets retweet replied reply quote posted "
           "token coin pump fun sol solana ca dev chart buy sell".split())

SCHEMA = """
CREATE TABLE IF NOT EXISTS social_mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    channel TEXT NOT NULL,
    msg_id TEXT NOT NULL,
    account TEXT,
    mints TEXT,
    tickers TEXT,
    words TEXT,
    text TEXT,
    UNIQUE(channel, msg_id)
);
CREATE INDEX IF NOT EXISTS ix_social_ts ON social_mentions(ts);
"""


def init_schema(db) -> None:
    with db._lock:
        db._conn.executescript(SCHEMA)


def channels(s) -> list[str]:
    raw = str(getattr(s, "twitter_feed_channel_ids", "") or "")
    return [c for c in (x.strip() for x in raw.replace("\n", ",").split(",")) if c.isdigit()][:20]


def _message_text(msg: dict) -> tuple[str, Optional[str]]:
    """All readable text of a Discord message (content + embeds) and the X account if one is named."""
    parts = [str(msg.get("content") or "")]
    account = None
    for e in msg.get("embeds") or []:
        if not isinstance(e, dict):
            continue
        a = e.get("author") or {}
        parts += [str(e.get("title") or ""), str(e.get("description") or ""), str(e.get("url") or ""),
                  str(a.get("name") or ""), str(a.get("url") or ""), str((e.get("footer") or {}).get("text") or "")]
        for f in e.get("fields") or []:
            if isinstance(f, dict):
                parts += [str(f.get("name") or ""), str(f.get("value") or "")]
        for url in (a.get("url"), e.get("url")):
            m = HANDLE_RE.search(str(url or ""))
            if m and account is None:
                account = m.group(1)
    text = "\n".join(p for p in parts if p)
    if account is None:
        m = HANDLE_RE.search(text)
        account = m.group(1) if m else None
    return text, account


def parse(msg: dict, is_mint: Optional[Callable[[str], bool]] = None) -> dict:
    """{"mints", "tickers", "account", "words", "text"} from one Discord message."""
    text, account = _message_text(msg)
    mints = []
    for m in MINT_RE.findall(text):
        if m in mints:
            continue
        if is_mint is None or is_mint(m):
            mints.append(m)
    tickers = sorted({t.upper() for t in TICKER_RE.findall(text)})
    clean = re.sub(r"https?://\S+", " ", text.lower())
    clean = MINT_RE.sub(" ", clean)
    words = sorted({w for w in WORD_RE.findall(clean) if w not in STOP})[:40]
    return {"mints": mints[:5], "tickers": tickers[:10], "account": account, "words": words, "text": text[:600]}


class TwitterFeed:
    """Reads the operator's tracker channels. `fetch(url, headers)` -> (status, json) is injectable."""

    def __init__(self, s, db, token: Optional[str], fetch=None, is_mint=None):
        self.s, self.db, self.token = s, db, token
        self._fetch = fetch
        self.is_mint = is_mint
        self.last_id: dict[str, str] = {}
        self.status: dict[str, dict] = {}
        self._seen_mints: dict[str, float] = {}
        init_schema(db)
        for ch in channels(s):                      # resume after a restart, never re-read old messages
            row = db.fetchone("SELECT MAX(CAST(msg_id AS INTEGER)) AS m FROM social_mentions WHERE channel = ?", (ch,))
            if row and row["m"]:
                self.last_id[ch] = str(row["m"])

    def enabled(self) -> bool:
        return bool(self.token and channels(self.s))

    async def _get(self, url: str):
        if self._fetch is not None:
            return await self._fetch(url, {"Authorization": f"Bot {self.token}"})
        import httpx
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(url, headers={"Authorization": f"Bot {self.token}", "User-Agent": "Northstar"})
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, None

    async def poll(self, now: Optional[float] = None) -> list[dict]:
        """Read new messages on every channel. Returns new contract-address mentions [{"mint", "account", "why"}]."""
        now = now or time.time()
        signals = []
        for ch in channels(self.s):
            after = self.last_id.get(ch)
            url = f"{API}/channels/{ch}/messages?limit=50" + (f"&after={after}" if after else "")
            try:
                status, data = await self._get(url)
            except Exception as e:  # noqa: BLE001 - retried next poll
                self.status[ch] = {"ok": False, "error": f"{type(e).__name__}", "at": now}
                continue
            if status != 200 or not isinstance(data, list):
                why = {401: "the bot token was refused", 403: "the bot cannot read this channel (needs View Channel "
                       "+ Read Message History)", 404: "channel not found (wrong id, or the bot is not in that server)"
                       }.get(status, f"Discord answered {status}")
                if self.status.get(ch, {}).get("error") != why:
                    log.warning("twitter feed: channel %s: %s", ch, why)
                self.status[ch] = {"ok": False, "error": why, "at": now}
                continue
            msgs = sorted(data, key=lambda m: int(m.get("id") or 0))
            if after is None:
                # first read: start from now (the newest message), never replay a channel's history
                if msgs:
                    self.last_id[ch] = str(msgs[-1]["id"])
                self.status[ch] = {"ok": True, "read": 0, "at": now, "empty_text": 0}
                continue
            empty = 0
            for m in msgs:
                self.last_id[ch] = str(m.get("id"))
                p = parse(m, self.is_mint)
                if not p["text"].strip():
                    empty += 1
                    continue
                self.db.execute("INSERT OR IGNORE INTO social_mentions(ts, channel, msg_id, account, mints, tickers, "
                                "words, text) VALUES (?,?,?,?,?,?,?,?)",
                                (now, ch, str(m.get("id")), p["account"], json.dumps(p["mints"]),
                                 json.dumps(p["tickers"]), json.dumps(p["words"]), p["text"]))
                for mint in p["mints"]:
                    if now - self._seen_mints.get(mint, 0) < 600:
                        continue
                    self._seen_mints[mint] = now
                    who = f"@{p['account']}" if p["account"] else "your Twitter tracker"
                    signals.append({"mint": mint, "account": p["account"], "why": f"{who} posted its contract address"})
            if msgs and empty == len(msgs):
                log.warning("twitter feed: channel %s: %d message(s) with no readable text. Switch on 'Message "
                            "Content Intent' for the bot (Discord Developer Portal -> Bot -> Privileged Gateway "
                            "Intents)", ch, empty)
            self.status[ch] = {"ok": True, "read": len(msgs), "empty_text": empty, "at": now}
        if len(self._seen_mints) > 5000:
            self._seen_mints = {k: v for k, v in self._seen_mints.items() if now - v < 3600}
        return signals


# ----------------------------------------------------------------------------- reading (attention, dashboard)
def mentions(db, now: float, window_min: float = 60.0) -> list[dict]:
    try:
        rows = db.fetchall("SELECT ts, account, mints, tickers, words FROM social_mentions WHERE ts > ?",
                           (now - window_min * 60,))
    except Exception:  # noqa: BLE001 - table not created yet
        return []
    out = []
    for r in rows:
        try:
            out.append({"ts": r["ts"], "account": r["account"], "mints": json.loads(r["mints"] or "[]"),
                        "tickers": json.loads(r["tickers"] or "[]"), "words": json.loads(r["words"] or "[]")})
        except (TypeError, ValueError):
            continue
    return out


def coin_attention(db, mint: str, symbol: Optional[str], name_words: set, now: float, window_min: float = 60.0) -> dict:
    """What the tracker feed says about one coin: {"accounts", "ca_posts", "ticker_posts", "terms"}."""
    ms = mentions(db, now, window_min)
    sym = (symbol or "").upper()
    accounts = {m["account"] or "?" for m in ms if mint in m["mints"]}
    ca = sum(1 for m in ms if mint in m["mints"])
    tk = sum(1 for m in ms if sym and sym in m["tickers"])
    counts = Counter(w for m in ms for w in m["words"])
    terms = sorted([(w, counts[w]) for w in name_words if counts.get(w, 0) >= 2], key=lambda x: -x[1])
    return {"accounts": sorted(accounts), "ca_posts": ca, "ticker_posts": tk, "terms": terms[:3]}


def trending(db, now: float, window_min: float = 60.0, limit: int = 15) -> dict:
    ms = mentions(db, now, window_min)
    mints = Counter(x for m in ms for x in m["mints"])
    tickers = Counter(x for m in ms for x in m["tickers"])
    words = Counter(w for m in ms for w in m["words"])
    accounts = Counter(m["account"] for m in ms if m["account"])
    return {"messages": len(ms), "mints": mints.most_common(limit), "tickers": tickers.most_common(limit),
            "terms": [(w, n) for w, n in words.most_common(limit * 2) if n >= 2][:limit],
            "accounts": accounts.most_common(limit)}


def prune(db, now: Optional[float] = None, days: float = 3.0) -> int:
    now = now or time.time()
    try:
        init_schema(db)
        with db._lock:
            return db._conn.execute("DELETE FROM social_mentions WHERE ts < ?", (now - days * 86400,)).rowcount
    except Exception:  # noqa: BLE001
        return 0
