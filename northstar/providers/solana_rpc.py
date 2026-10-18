"""Standard Solana JSON-RPC client for public endpoints and Helius.

Provides standard Solana JSON-RPC methods, bounded retries for transient
failures, and sanitized diagnostics. Persistent errors remain errors:
unavailable data is never treated as clean.

Retries are deliberately bounded to avoid overwhelming rate-limited RPCs.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import random
import time
from collections import Counter
from typing import Any, Optional

from ..net import ProviderClient, ProviderError

log = logging.getLogger("northstar.solana_rpc")

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_MINTS = {WSOL_MINT: "SOL", USDC_MINT: "USDC", USDT_MINT: "USDT"}
BURN_ADDRESSES = {"1nc1nerator11111111111111111111111111111111"}

# Only retry failures that may resolve on a subsequent request.
_TRANSIENT_HINTS = (
    "429",
    "rate limit",
    "too many requests",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "service unavailable",
    "internal error",
    "gateway",
    "502",
    "503",
    "504",
    "connection reset",
    "connection refused",
    "server disconnected",
    "remote protocol error",
)


class RpcError(ProviderError):
    """Sanitized JSON-RPC error."""


class SolanaRpc:
    def __init__(
        self,
        client: ProviderClient,
        path: str = "/",
        max_retries: int = 2,
        retry_base_seconds: float = 0.35,
        retry_max_seconds: float = 2.0,
    ):
        self.client = client
        self.path = path
        self._ids = itertools.count(1)
        self.max_retries = max(0, min(int(max_retries), 4))
        self.retry_base_seconds = max(0.05, float(retry_base_seconds))
        self.retry_max_seconds = max(
            self.retry_base_seconds, float(retry_max_seconds)
        )
        self._stats = Counter()
        self._last_error_at: dict[str, float] = {}

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(hint in message for hint in _TRANSIENT_HINTS)

    def stats_snapshot(self) -> dict[str, int]:
        """Return a copy of RPC request counters for health/debug reporting."""
        return dict(self._stats)

    async def call(
        self,
        method: str,
        params: list,
        cache_ttl: float = 0,
    ) -> Any:
        body = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params,
        }

        # Do not log params: they can contain account identifiers or other
        # request data. Never log the endpoint URL, which may contain a key.
        attempts = self.max_retries + 1

        for attempt in range(attempts):
            self._stats[f"{method}.attempts"] += 1
            try:
                # A stable ID is fine because the request is made once per
                # attempt; cache identity should not depend on the JSON-RPC ID.
                data = await self.client.request(
                    "POST",
                    self.path,
                    json_body=body | {"id": 1},
                    cache_ttl=cache_ttl,
                )

                if not isinstance(data, dict):
                    raise RpcError(
                        self.client.name,
                        f"{method}: malformed JSON-RPC response",
                    )

                if data.get("error"):
                    err = data["error"]
                    code = err.get("code")
                    message = str(err.get("message") or "unknown RPC error")
                    # RPC error messages should not contain the request URL,
                    # but redact it if a provider unexpectedly echoes it.
                    message = message.replace(self.path, "[RPC_PATH]")
                    raise RpcError(
                        self.client.name,
                        f"{method}: RPC code={code}, message={message}",
                    )

                if "result" not in data:
                    raise RpcError(
                        self.client.name,
                        f"{method}: JSON-RPC response missing result",
                    )

                self._stats[f"{method}.success"] += 1
                return data["result"]

            except Exception as exc:
                # Preserve cancellation; never turn a cancelled task into a
                # retry or a normal RPC error.
                if isinstance(exc, asyncio.CancelledError):
                    raise

                is_transient = self._is_transient(exc)
                final_attempt = attempt >= attempts - 1

                if is_transient and not final_attempt:
                    self._stats[f"{method}.retry"] += 1
                    delay = min(
                        self.retry_max_seconds,
                        self.retry_base_seconds * (2 ** attempt),
                    )
                    delay += random.uniform(0, delay * 0.2)
                    await asyncio.sleep(delay)
                    continue

                self._stats[f"{method}.failure"] += 1
                category = "transient" if is_transient else "non_transient"
                now = time.monotonic()

                # Rate-limit repetitive logging per method. The counter still
                # tracks every failure for diagnostics.
                last = self._last_error_at.get(method, 0.0)
                if now - last >= 15:
                    self._last_error_at[method] = now
                    log.warning(
                        "RPC request failed: method=%s category=%s "
                        "attempts=%d error_type=%s error=%s",
                        method,
                        category,
                        attempt + 1,
                        type(exc).__name__,
                        str(exc)[:350],
                    )

                if isinstance(exc, ProviderError):
                    raise

                raise RpcError(
                    self.client.name,
                    f"{method}: request failed after {attempt + 1} "
                    f"attempt(s): {type(exc).__name__}: {str(exc)[:250]}",
                ) from exc

        # Defensive; the loop always returns or raises.
        raise RpcError(self.client.name, f"{method}: exhausted retries")

    async def get_account_info(self, address: str) -> Optional[dict]:
        r = await self.call(
            "getAccountInfo",
            [
                address,
                {"encoding": "jsonParsed", "commitment": "confirmed"},
            ],
            cache_ttl=60,
        )
        return (r or {}).get("value")

    async def get_multiple_accounts(
        self, addresses: list[str]
    ) -> list[Optional[dict]]:
        out: list[Optional[dict]] = []
        for i in range(0, len(addresses), 100):
            r = await self.call(
                "getMultipleAccounts",
                [
                    addresses[i : i + 100],
                    {
                        "encoding": "jsonParsed",
                        "commitment": "confirmed",
                    },
                ],
                cache_ttl=60,
            )
            out.extend((r or {}).get("value") or [])
        return out

    async def get_token_supply(self, mint: str) -> Optional[dict]:
        r = await self.call(
            "getTokenSupply",
            [mint, {"commitment": "confirmed"}],
            cache_ttl=60,
        )
        return (r or {}).get("value")

    async def get_token_largest_accounts(self, mint: str) -> list[dict]:
        r = await self.call(
            "getTokenLargestAccounts",
            [mint, {"commitment": "confirmed"}],
            cache_ttl=60,
        )
        return (r or {}).get("value") or []

    async def get_signatures(
        self,
        address: str,
        limit: int = 100,
        before: Optional[str] = None,
    ) -> list[dict]:
        cfg: dict[str, Any] = {
            "limit": min(max(int(limit), 1), 1000),
            "commitment": "confirmed",
        }
        if before:
            cfg["before"] = before

        r = await self.call(
            "getSignaturesForAddress",
            [address, cfg],
            cache_ttl=15,
        )
        return r or []

    async def get_transaction(self, signature: str) -> Optional[dict]:
        result = await self.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 1,
                    "commitment": "confirmed",
                },
            ],
            # A successfully fetched transaction is immutable for our
            # purposes. Failed requests are not cached by this method.
            cache_ttl=3600,
        )

        if result is not None and not isinstance(result, dict):
            raise RpcError(
                self.client.name,
                "getTransaction: result was not an object or null",
            )

        # Solana legitimately returns null when a transaction is unavailable
        # at the selected commitment or unsupported by the RPC backend.
        return result
