"""Reddit search via the official OAuth API (application-only / client_credentials).

Requires a free Reddit "script" or "web" app (client id + secret). Without credentials this
provider is disabled and narrative attention is marked UNAVAILABLE — it is never guessed.

Endpoints (reddit.com/dev/api):
  POST https://www.reddit.com/api/v1/access_token  grant_type=client_credentials (basic auth)
  GET  https://oauth.reddit.com/search?q=...&sort=new&t=day&limit=100&type=link
Listing children have data.{id, author, title, selftext, subreddit, created_utc, score,
num_comments, url, permalink}.

Reddit coverage of brand-new memecoins is thin. Low counts mean "little evidence", not "bad".
"""
from __future__ import annotations

import base64
import time
from typing import Optional

import httpx

from ..net import ProviderClient, ProviderError


class Reddit:
    TOKEN_URL = "https://www.reddit.com/api/v1/access_token"

    def __init__(self, client: ProviderClient, client_id: str, client_secret: str, user_agent: str,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.client = client
        self._cid = client_id
        self._secret = client_secret
        self._ua = user_agent
        self._token: Optional[str] = None
        self._token_exp = 0.0
        self._transport = transport

    async def _auth(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        basic = base64.b64encode(f"{self._cid}:{self._secret}".encode()).decode()
        async with httpx.AsyncClient(timeout=15, transport=self._transport) as cl:
            r = await cl.post(self.TOKEN_URL, data={"grant_type": "client_credentials"},
                              headers={"Authorization": f"Basic {basic}", "User-Agent": self._ua})
        if r.status_code != 200:
            raise ProviderError("reddit", f"auth failed HTTP {r.status_code}", r.status_code)
        d = r.json()
        self._token = d.get("access_token")
        self._token_exp = time.time() + float(d.get("expires_in", 3600))
        if not self._token:
            raise ProviderError("reddit", "auth response missing token")
        return self._token

    async def search(self, query: str, window: str = "day", limit: int = 100) -> list[dict]:
        token = await self._auth()
        data = await self.client.request(
            "GET", "/search", params={"q": query, "sort": "new", "t": window, "limit": limit, "type": "link"},
            cache_ttl=300, headers={"Authorization": f"bearer {token}", "User-Agent": self._ua})
        children = ((data or {}).get("data") or {}).get("children") or []
        out = []
        for c in children:
            d = (c or {}).get("data") or {}
            if d.get("id"):
                out.append({k: d.get(k) for k in ("id", "author", "title", "selftext", "subreddit",
                                                   "created_utc", "score", "num_comments", "permalink")})
        return out
