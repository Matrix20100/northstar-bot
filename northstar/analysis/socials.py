"""Free social checks on a token's own links.

What this can and cannot say:
  * It reads the links a token publishes (DexScreener profile/pair info, on-chain metadata JSON).
  * It classifies X links: an account, a community, a single post (often someone else's post the
    token is "about"), or junk.
  * RECYCLED links: the same X account/community/Telegram/website used by other tokens recently
    is a classic serial-launcher sign. This is checked against Northstar's own database.
  * Telegram member counts from the public preview page are WEAK evidence (members can be bought).
  * X activity itself (posts, followers, engagement) is never measured.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

RESERVED_X = {"home", "i", "search", "intent", "share", "explore", "hashtag", "settings", "login", "signup",
              "messages", "notifications", "compose", "tos", "privacy"}
LINK_HOSTS = {"linktr.ee", "pump.fun", "dexscreener.com", "t.me", "x.com", "twitter.com", "discord.gg",
              "discord.com", "youtube.com", "tiktok.com", "instagram.com", "github.com", "medium.com"}


@dataclass
class SocialLink:
    kind: str   # x_account | x_community | x_post | telegram | website | discord
    key: str    # normalised identity used for reuse checks
    url: str
    handle: Optional[str] = None


def _url(raw) -> Optional[str]:
    if isinstance(raw, dict):
        raw = raw.get("url") or raw.get("link") or ""
    raw = str(raw or "").strip()
    if not raw:
        return None
    if raw.startswith("@"):
        raw = "https://x.com/" + raw[1:]
    if not re.match(r"^[a-z]+://", raw, re.I):
        raw = "https://" + raw
    return raw


def classify(raw, hint: str = "") -> Optional[SocialLink]:
    url = _url(raw)
    if not url:
        return None
    try:
        u = urlparse(url)
    except ValueError:
        return None
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("mobile.")
    parts = [p for p in u.path.split("/") if p]
    if host in ("x.com", "twitter.com"):
        if len(parts) >= 3 and parts[0] == "i" and parts[1] == "communities" and parts[2].isdigit():
            return SocialLink("x_community", parts[2], url)
        if len(parts) >= 3 and parts[1] == "status" and parts[2].isdigit():
            handle = parts[0].lower()
            return SocialLink("x_post", parts[2], url, None if handle in RESERVED_X else handle)
        if parts and re.fullmatch(r"[A-Za-z0-9_]{1,15}", parts[0]) and parts[0].lower() not in RESERVED_X:
            return SocialLink("x_account", parts[0].lower(), url, parts[0].lower())
        return None
    if host in ("t.me", "telegram.me"):
        if parts and parts[0] not in ("s", "joinchat", "share") and re.fullmatch(r"[A-Za-z0-9_]{4,64}", parts[0]):
            return SocialLink("telegram", parts[0].lower(), url, parts[0])
        if parts and parts[0] == "s" and len(parts) > 1:
            return SocialLink("telegram", parts[1].lower(), url, parts[1])
        if parts and (parts[0] == "joinchat" or parts[0].startswith("+")):
            return SocialLink("telegram", "/".join(parts).lower(), url)  # private invite: no public count
        return None
    if host in ("discord.gg", "discord.com"):
        return SocialLink("discord", "/".join(parts).lower(), url) if parts else None
    if host and "." in host and host not in LINK_HOSTS and not host.endswith(("ipfs.io", "pinata.cloud")):
        return SocialLink("website", host, url)
    return None


def collect(*sources) -> list[SocialLink]:
    """Links from DexScreener socials/websites lists and metadata JSON dicts, de-duplicated."""
    seen, out = set(), []

    def add(raw, hint=""):
        link = classify(raw, hint)
        if link and (link.kind, link.key) not in seen:
            seen.add((link.kind, link.key))
            out.append(link)

    for src in sources:
        if not src:
            continue
        if isinstance(src, dict):  # metadata JSON
            for k in ("twitter", "telegram", "website", "discord"):
                if src.get(k):
                    add(src[k], k)
            ext = src.get("extensions")
            if isinstance(ext, dict):
                for k in ("twitter", "telegram", "website", "discord"):
                    if ext.get(k):
                        add(ext[k], k)
            continue
        for item in src:
            add(item)
    return out


@dataclass
class SocialEvidence:
    links: list[SocialLink] = field(default_factory=list)
    reuse: dict = field(default_factory=dict)          # "kind:key" -> other mints using it
    telegram_members: Optional[int] = None
    telegram_handle: Optional[str] = None

    @property
    def recycled(self) -> list[str]:
        return [k for k, n in self.reuse.items() if n >= RECYCLE_MIN_OTHERS]

    def has(self, *kinds) -> bool:
        return any(l.kind in kinds for l in self.links)

    def to_dict(self) -> dict:
        return {"links": [{"kind": l.kind, "url": l.url} for l in self.links],
                "reused_by_other_tokens": self.reuse, "recycled": self.recycled,
                "telegram_members": self.telegram_members,
                "telegram_members_status": "OBSERVED (weak; can be botted)" if self.telegram_members is not None
                else "UNAVAILABLE",
                "x_activity": "NOT MEASURED (no free X read access)"}


RECYCLE_MIN_OTHERS = 2       # same link on 2+ other tokens in the lookback window
REUSE_KINDS = ("x_account", "x_community", "telegram", "website")


def score_attention(ev: Optional[SocialEvidence]) -> tuple[float, list[str], list[str], int]:
    """(points out of 4, evidence lines, penalty lines, evidence items) for the free part of attention."""
    if ev is None:
        return 0.0, [], [], 0
    good, bad, items, pts = [], [], 0, 0.0
    if ev.recycled:
        bad.append("recycled socials: " + ", ".join(f"{k} (used by {ev.reuse[k]} other tokens)" for k in ev.recycled)
                   + " — serial-launcher pattern, no social credit given")
        return 0.0, good, bad, 0
    if ev.has("x_account", "x_community"):
        pts += 1
        good.append("own X account/community linked (presence only; X activity not measured)")
    elif ev.has("x_post"):
        good.append("links an X post rather than its own account (references a post; not proof of attention)")
    m = ev.telegram_members
    if m is not None:
        if m >= 2000:
            pts += 3
        elif m >= 500:
            pts += 2
        elif m >= 150:
            pts += 1
        if m >= 500:
            items += 1
        good.append(f"Telegram @{ev.telegram_handle}: {m:,} members (public count; can be botted)")
    return min(pts, 4.0), good, bad, items
