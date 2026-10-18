"""Shared HTTP layer for Northstar.

Features:
- Per-client request-rate limiting with a small default burst.
- Shared cooldown after HTTP 429 responses.
- Exponential backoff with jitter.
- HTTP timeouts and bounded retries.
- Response caching and duplicate-request coalescing.
- Daily request budgets.
- Circuit breaking and provider health reporting.

Provider failures are surfaced as ProviderError. They must never be
interpreted as proof that a token is safe.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Optional

import httpx

from .logging_setup import redact

log = logging.getLogger("northstar.net")


class ProviderError(Exception):
    def __init__(
        self,
        provider: str,
        message: str,
        status: Optional[int] = None,
    ):
        super().__init__(f"[{provider}] {redact(message)}")
        self.provider = provider
        self.status = status


class RateLimited(ProviderError):
    pass


class BudgetExceeded(ProviderError):
    pass


class CircuitOpen(ProviderError):
    pass


class NotFound(ProviderError):
    pass


class TokenBucket:
    """Async token bucket with a small burst capacity."""

    def __init__(
        self,
        rate_per_minute: float,
        burst: Optional[int] = None,
    ):
        self.rate = max(float(rate_per_minute), 1.0) / 60.0

        # Default to one immediately available request. This reduces bursts.
        self.capacity = max(
            1,
            int(burst) if burst is not None else 1,
        )

        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0

    async def acquire(self) -> None:
        """Wait until a token is available and provider cooldown has ended."""
        while True:
            async with self._lock:
                now = time.monotonic()

                if now < self._blocked_until:
                    delay = self._blocked_until - now
                else:
                    elapsed = max(0.0, now - self.updated)
                    self.tokens = min(
                        float(self.capacity),
                        self.tokens + elapsed * self.rate,
                    )
                    self.updated = now

                    if self.tokens >= 1.0:
                        self.tokens -= 1.0
                        return

                    delay = (1.0 - self.tokens) / self.rate

            await asyncio.sleep(max(0.01, delay))

    async def penalize(self, seconds: float) -> None:
        """Coordinate cooldown across requests sharing this bucket."""
        seconds = max(1.0, min(float(seconds), 120.0))

        async with self._lock:
            now = time.monotonic()
            self._blocked_until = max(
                self._blocked_until,
                now + seconds,
            )

            # Drain the bucket so requests don't burst after cooldown.
            self.tokens = 0.0
            self.updated = self._blocked_until

        log.warning(
            "request bucket cooling down for %.1fs",
            seconds,
        )


@dataclass
class _CacheEntry:
    expires: float
    value: Any


@dataclass
class ProviderClient:
    name: str
    base_url: str
    rpm: float
    daily_budget: int = 0
    timeout: float = 15.0
    max_retries: int = 3
    headers: dict = field(default_factory=dict)
    health_cb: Optional[
        Callable[[str, bool, Optional[str]], None]
    ] = None
    budget_cb: Optional[Callable[[str], int]] = None
    transport: Optional[httpx.AsyncBaseTransport] = None
    circuit_threshold: int = 8
    circuit_cooldown: float = 120.0

    def __post_init__(self) -> None:
        self.bucket = TokenBucket(self.rpm)

        self._cache: dict[str, _CacheEntry] = {}
        self._inflight: dict[str, asyncio.Future] = {}
        self._inflight_lock = asyncio.Lock()

        self._client: Optional[httpx.AsyncClient] = None
        self._client_lock = asyncio.Lock()

        self._consecutive_failures = 0
        self._circuit_until = 0.0
        self.local_budget_used = 0

    async def client(self) -> httpx.AsyncClient:
        """Create one reusable HTTP client."""
        if self._client is not None:
            return self._client

        async with self._client_lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=httpx.Timeout(self.timeout),
                    transport=self.transport,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "northstar-alpha-engine/1.0",
                        **self.headers,
                    },
                )

        return self._client

    async def close(self) -> None:
        async with self._client_lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None

    @property
    def circuit_open(self) -> bool:
        return time.monotonic() < self._circuit_until

    def _key(
        self,
        method: str,
        path: str,
        params: Any,
        body: Any,
    ) -> str:
        raw = json.dumps(
            [method.upper(), path, params, body],
            sort_keys=True,
            default=str,
        )
        return hashlib.sha1(raw.encode()).hexdigest()

    def _record(
        self,
        ok: bool,
        err: Optional[str] = None,
    ) -> None:
        if ok:
            self._consecutive_failures = 0
            self._circuit_until = 0.0
        else:
            self._consecutive_failures += 1

            if self._consecutive_failures >= self.circuit_threshold:
                self._circuit_until = (
                    time.monotonic() + self.circuit_cooldown
                )

                log.warning(
                    "provider %s circuit opened for %.0fs after %d failures",
                    self.name,
                    self.circuit_cooldown,
                    self._consecutive_failures,
                )

        if self.health_cb:
            try:
                self.health_cb(self.name, ok, err)
            except Exception:
                log.exception("health callback failed")

    def _check_budget(self) -> None:
        if not self.daily_budget:
            return

        used = (
            self.budget_cb(self.name)
            if self.budget_cb
            else self.local_budget_used
        )

        if used >= self.daily_budget:
            raise BudgetExceeded(
                self.name,
                f"daily budget {self.daily_budget} exhausted",
            )

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Any = None,
        cache_ttl: float = 0,
        headers: Optional[dict] = None,
    ) -> Any:
        """Perform an HTTP request, optionally caching/coalescing it."""

        method = method.upper()

        key = self._key(
            method,
            path,
            params,
            json_body,
        )

        if cache_ttl > 0:
            now = time.monotonic()
            entry = self._cache.get(key)

            if entry and entry.expires > now:
                return entry.value

            async with self._inflight_lock:
                # Recheck after obtaining the lock.
                entry = self._cache.get(key)

                if entry and entry.expires > time.monotonic():
                    return entry.value

                existing = self._inflight.get(key)

                if existing is None:
                    future = (
                        asyncio.get_running_loop().create_future()
                    )
                    self._inflight[key] = future
                    owner = True
                else:
                    future = existing
                    owner = False

            if not owner:
                return await asyncio.shield(future)

            try:
                value = await self._do(
                    method,
                    path,
                    params,
                    json_body,
                    headers,
                )

                self._cache[key] = _CacheEntry(
                    time.monotonic() + cache_ttl,
                    value,
                )

                if len(self._cache) > 5000:
                    self._evict()

                if not future.done():
                    future.set_result(value)

                return value

            except BaseException as exc:
                if not future.done():
                    future.set_exception(exc)
                    # Retrieve the exception if there are no waiters.
                    future.exception()
                raise

            finally:
                async with self._inflight_lock:
                    if self._inflight.get(key) is future:
                        self._inflight.pop(key, None)

        return await self._do(
            method,
            path,
            params,
            json_body,
            headers,
        )

    def _evict(self) -> None:
        now = time.monotonic()

        expired = [
            key
            for key, entry in self._cache.items()
            if entry.expires <= now
        ]

        for key in expired:
            self._cache.pop(key, None)

        while len(self._cache) > 4000:
            self._cache.pop(next(iter(self._cache)))

    async def _do(
        self,
        method: str,
        path: str,
        params: Optional[dict],
        json_body: Any,
        headers: Optional[dict],
    ) -> Any:
        attempt = 0
        last_err: Optional[Exception] = None

        while attempt <= max(0, self.max_retries):
            if self.circuit_open:
                raise CircuitOpen(
                    self.name,
                    "circuit open after repeated failures",
                )

            self._check_budget()
            await self.bucket.acquire()

            self.local_budget_used += 1

            try:
                client = await self.client()

                response = await client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    headers=headers,
                )

            except asyncio.CancelledError:
                raise

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_err = ProviderError(
                    self.name,
                    f"{type(exc).__name__}: {exc}",
                )
                self._record(False, str(last_err))

            else:
                status = response.status_code

                if status == 429:
                    retry_after = _retry_after(response)

                    # Coordinate the cooldown for all requests using
                    # this ProviderClient.
                    await self.bucket.penalize(retry_after)

                    last_err = RateLimited(
                        self.name,
                        f"HTTP 429 on {path}",
                        429,
                    )

                    # Rate limits are not evidence that the provider
                    # itself is permanently unhealthy.
                    log.warning(
                        "provider %s rate limited; cooldown %.1fs",
                        self.name,
                        retry_after,
                    )

                    attempt += 1

                    if attempt > max(0, self.max_retries):
                        self._record(False, "HTTP 429")
                        raise last_err

                    # Retry through the shared bucket rather than
                    # sleeping independently in every request.
                    continue

                if status == 404:
                    self._record(True)
                    raise NotFound(
                        self.name,
                        f"HTTP 404 on {path}",
                        404,
                    )

                if 400 <= status < 500:
                    self._record(False, f"HTTP {status}")

                    raise ProviderError(
                        self.name,
                        f"HTTP {status} on {path}: "
                        f"{response.text[:200]}",
                        status,
                    )

                if status >= 500:
                    last_err = ProviderError(
                        self.name,
                        f"HTTP {status} on {path}",
                        status,
                    )
                    self._record(False, f"HTTP {status}")

                else:
                    try:
                        data = response.json()
                    except ValueError as exc:
                        self._record(False, "invalid JSON")

                        raise ProviderError(
                            self.name,
                            f"invalid JSON from {path}",
                        ) from exc

                    self._record(True)
                    return data

            attempt += 1

            if attempt <= max(0, self.max_retries):
                delay = min(
                    30.0,
                    0.5 * (2 ** attempt),
                ) + random.uniform(0.0, 0.5)

                await asyncio.sleep(delay)

        if last_err is not None:
            raise last_err

        raise ProviderError(
            self.name,
            "request failed without a usable response",
        )


def _retry_after(resp: httpx.Response) -> float:
    """Parse Retry-After as seconds or an HTTP date."""

    value = resp.headers.get("Retry-After", "").strip()

    if value:
        try:
            return max(1.0, min(float(value), 120.0))
        except ValueError:
            try:
                target = parsedate_to_datetime(value)
                delay = target.timestamp() - time.time()
                return max(1.0, min(delay, 120.0))
            except (TypeError, ValueError, OverflowError):
                pass

    # No valid Retry-After header: use a conservative fallback.
    return 5.0 + random.uniform(0.0, 2.0)