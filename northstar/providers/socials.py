"""Free social evidence: token metadata links and public Telegram member counts.

Nothing here uses a paid API or a login:
  * the token's own metadata JSON (the ``uri`` stored on-chain; Pump.fun puts ``twitter``,
    ``telegram`` and ``website`` there), fetched once per mint and cached;
  * the public t.me preview page of a Telegram channel/group, which shows
    "N subscribers" / "N members". Counts can be botted, so they are WEAK evidence.

X/Twitter content (posts, followers, engagement) is NOT read: there is no free X read access.
Every failure returns None, so callers report the data as UNAVAILABLE instead of guessing.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import time
from typing import Optional
from urllib.parse import urlparse

import httpx

log = logging.getLogger("northstar.socials")

MAX_BYTES = 256_000
IPFS_GATEWAY = "https://ipfs.io/ipfs/"
_TG_COUNT = re.compile(r"([\d][\d\s  ,.]*)\s*(subscribers|members)\b", re.I)


def _safe_https(url: str) -> Optional[str]:
    """https URL on a public hostname, or None. ipfs:// is mapped to a public gateway."""
    url = (url or "").strip()
    if url.startswith("ipfs://"):
        url = IPFS_GATEWAY + url[len("ipfs://"):].lstrip("/")
    try:
        u = urlparse(url)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host or host == "localhost" or host.endswith(".local") or "." not in host:
        return None
    try:
        ipaddress.ip_address(host)
        return None  # raw IPs are never fetched
    except ValueError:
        pass
    return url


def parse_tg_count(html: str) -> Optional[int]:
    m = _TG_COUNT.search(html or "")
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


class FreeSocials:
    def __init__(self, timeout: float = 6.0, min_interval: float = 1.0, ttl: float = 3600,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.timeout, self.min_interval, self.ttl = timeout, min_interval, ttl
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, transport=self._transport,
                                             headers={"User-Agent": "northstar-research/1.0"})
        return self._client

    async def _get(self, url: str) -> Optional[str]:
        url = _safe_https(url)
        if not url:
            return None
        hit = self._cache.get(url)
        if hit and hit[0] > time.monotonic():
            return hit[1]  # type: ignore[return-value]
        async with self._lock:  # one request at a time, politely spaced
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
        text: Optional[str] = None
        try:
            client = await self._http()
            r = await client.get(url)
            if r.status_code == 200 and len(r.content) <= MAX_BYTES and _safe_https(str(r.url)):
                text = r.text
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("free social fetch failed for %s: %s", url, exc)
        self._cache[url] = (time.monotonic() + (self.ttl if text is not None else 300), text)
        if len(self._cache) > 5000:
            for k in sorted(self._cache, key=lambda k: self._cache[k][0])[:1000]:
                self._cache.pop(k, None)
        return text

    async def metadata(self, uri: str) -> Optional[dict]:
        text = await self._get(uri)
        if not text:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    async def telegram_members(self, handle: str) -> Optional[int]:
        if not re.fullmatch(r"[A-Za-z0-9_]{4,64}", handle or ""):
            return None
        return parse_tg_count(await self._get(f"https://t.me/{handle}") or "")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
