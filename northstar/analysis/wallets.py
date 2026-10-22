"""Trade sampling and wallet analysis from raw on-chain transactions.

All metrics describe bounded samples, not complete token or wallet history.

Relationship strength:
  CONFIRMED - a direct on-chain link, such as creator funding a buyer
  STRONG    - shared funding source or a same-slot buying heuristic
  WEAK      - same-slot buys only

Wallet relationships do not establish common ownership.

Important limitations:
- Token balance changes alone do not prove a transaction was a swap.
- SOL balance changes are estimates, not verified swap amounts.
- Parsed candidates are not automatically verified swaps.
- A partial sample must not be treated as complete history.
"""
from __future__ import annotations

import asyncio
import logging
import math
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from ..config import Settings
from ..models import (
    DataStatus,
    EvidenceStrength,
    Flag,
    GateResult,
    GateStatus,
    Severity,
    Trade,
)
from ..net import ProviderError
from ..providers.solana_rpc import SolanaRpc
from .pump_trades import REASONS as CURVE_DECODE_REASONS
from .pump_trades import VERIFIED as VERIFIED_PUMP_EVENT
from .pump_trades import decode_curve_transaction
from .pumpswap_trades import VERIFIED_PUMPSWAP, decode_pumpswap_transaction

# Verification labels that count as confirmed trades. Anything else is an unverified candidate.
VERIFIED_LABELS = frozenset({VERIFIED_PUMP_EVENT, VERIFIED_PUMPSWAP})

log = logging.getLogger("northstar.wallets")


# ---------------------------------------------------------------------------
# Transaction parsing


def _keys(tx: dict) -> list[dict]:
    """Normalize account keys, including addresses loaded through ALTs."""
    message = (
        (tx.get("transaction") or {}).get("message") or {}
    )
    raw_keys = message.get("accountKeys") or []
    keys: list[dict] = []

    for index, key in enumerate(raw_keys):
        if isinstance(key, dict):
            pubkey = key.get("pubkey")
            signer = bool(key.get("signer", index == 0))
        else:
            pubkey = key
            # String-only account keys do not carry explicit signer flags.
            # The fee payer is normally the first key.
            signer = index == 0

        if pubkey:
            keys.append({
                "pubkey": str(pubkey),
                "signer": signer,
            })

    # jsonParsed responses already include lookup-table addresses inside accountKeys (marked
    # with source="lookupTable"). Raw/json encodings do not, so append meta.loadedAddresses
    # (writable first, then readonly) ONLY for addresses that are not already present.
    present = {key["pubkey"] for key in keys}
    meta = tx.get("meta") or {}
    loaded = meta.get("loadedAddresses") or {}

    for group in ("writable", "readonly"):
        for address in loaded.get(group) or []:
            if address and str(address) not in present:
                present.add(str(address))
                keys.append({
                    "pubkey": str(address),
                    "signer": False,
                })

    return keys


def _token_amount(balance: dict) -> Optional[float]:
    """Read a token amount, preferring raw integer units and decimals."""
    info = balance.get("uiTokenAmount") or {}

    raw_amount = info.get("amount")
    decimals = info.get("decimals")

    if raw_amount is not None and decimals is not None:
        try:
            amount = int(raw_amount) / (10 ** int(decimals))
            return amount if math.isfinite(amount) else None
        except (
            TypeError,
            ValueError,
            OverflowError,
            ZeroDivisionError,
        ):
            pass

    for value in (
        info.get("uiAmountString"),
        info.get("uiAmount"),
    ):
        if value is None:
            continue

        try:
            amount = float(value)
            if math.isfinite(amount):
                return amount
        except (TypeError, ValueError, OverflowError):
            continue

    return None


def _instruction_mentions_swap(tx: dict) -> bool:
    """Check whether parsed RPC instructions explicitly describe a swap.

    This is not a universal DEX decoder. Many Solana programs return
    compiled, unparsed instructions, so False does not prove no swap occurred.
    """
    message = (
        (tx.get("transaction") or {}).get("message") or {}
    )
    instructions = list(message.get("instructions") or [])

    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        instructions.extend(inner.get("instructions") or [])

    swap_types = {
        "swap",
        "swapexactin",
        "swapexactout",
        "route",
        "routewithtokenledger",
        "sharedaccountsroute",
        "sharedaccountsroutewithtokenledger",
        "buy",
        "sell",
        "buyexactsolin",
        "buyexacttokenin",
        "sellexactsolin",
        "sellexacttokenin",
    }

    for instruction in instructions:
        if not isinstance(instruction, dict):
            continue

        parsed = instruction.get("parsed")
        if not isinstance(parsed, dict):
            continue

        instruction_type = str(
            parsed.get("type") or ""
        ).lower()

        if instruction_type in swap_types:
            return True

    return False


def _parse_trade_with_reason(
    tx: Optional[dict],
    mint: str,
    pool_owners: set[str],
    signature: str = "",
) -> tuple[Optional[Trade], str]:
    """Parse a candidate trade and return a diagnostic rejection reason.

    A candidate requires:
    - a successful transaction;
    - the selected pool address in the transaction when supplied;
    - a nonzero balance change for the requested mint;
    - an attributable wallet.

    Token balance changes alone cannot distinguish every swap from a
    transfer. The returned Trade must therefore be treated as a candidate
    until validated against the relevant program's instructions.
    """
    if not isinstance(tx, dict):
        return None, "invalid_transaction"

    meta = tx.get("meta") or {}

    if meta.get("err"):
        return None, "transaction_failed"

    keys = _keys(tx)
    account_addresses = {
        key["pubkey"]
        for key in keys
        if key.get("pubkey")
    }

    if pool_owners and not (pool_owners & account_addresses):
        return None, "pool_not_in_transaction"

    deltas: dict[str, float] = defaultdict(float)

    for side, multiplier in (
        ("preTokenBalances", -1.0),
        ("postTokenBalances", 1.0),
    ):
        for balance in meta.get(side) or []:
            if balance.get("mint") != mint:
                continue

            owner = balance.get("owner")
            if not owner:
                continue

            amount = _token_amount(balance)
            if amount is None:
                continue

            deltas[str(owner)] += multiplier * amount

    candidates = {
        owner: delta
        for owner, delta in deltas.items()
        if (
            abs(delta) > 1e-12
            and owner not in pool_owners
        )
    }

    if not candidates:
        return None, "no_requested_mint_owner_delta"

    signers = {
        key["pubkey"]
        for key in keys
        if key.get("signer") and key.get("pubkey")
    }

    signed_candidates = {
        owner: delta
        for owner, delta in candidates.items()
        if owner in signers
    }

    if signed_candidates:
        trader, delta = max(
            signed_candidates.items(),
            key=lambda item: abs(item[1]),
        )
    else:
        # Token owners are not always transaction signers. Avoid rejecting
        # every such transaction, but do not guess in ambiguous transactions.
        if len(signers) != 1 or len(candidates) != 1:
            return None, "ambiguous_trader_attribution"

        trader = next(iter(signers))
        delta = next(iter(candidates.values()))

    if not trader or abs(delta) <= 1e-12:
        return None, "zero_or_invalid_delta"

    explicit_swap = _instruction_mentions_swap(tx)

    # Without an explicit parsed swap instruction, the pool-address check
    # is only supporting evidence, not proof of a swap. Retain the candidate
    # for diagnostics, but label it as lower confidence in the reason.
    if explicit_swap:
        parse_reason = "parsed_candidate"
    elif pool_owners:
        parse_reason = "candidate_without_decoded_swap"
    else:
        return None, "no_swap_instruction_or_pool_evidence"

    # Estimate SOL movement. This is not verified swap volume: wallet balance
    # changes can include rent, transfers, tips, and unrelated instructions.
    sol_amount = None

    try:
        account_index = next(
            index
            for index, key in enumerate(keys)
            if key["pubkey"] == trader
        )

        pre_balances = meta.get("preBalances") or []
        post_balances = meta.get("postBalances") or []

        if (
            account_index >= len(pre_balances)
            or account_index >= len(post_balances)
        ):
            raise IndexError("wallet balance index missing")

        lamport_change = abs(
            int(post_balances[account_index])
            - int(pre_balances[account_index])
        )

        fee = int(meta.get("fee") or 0)

        # Account index zero is normally the transaction fee payer.
        if account_index == 0:
            lamport_change = max(0, lamport_change - fee)

        estimate = lamport_change / 1e9

        if math.isfinite(estimate) and estimate > 0:
            sol_amount = estimate

    except (
        StopIteration,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        OverflowError,
    ):
        sol_amount = None

    signatures = (
        (tx.get("transaction") or {}).get("signatures") or []
    )

    tx_signature = signature or (
        str(signatures[0])
        if signatures
        else ""
    )

    try:
        slot = int(tx.get("slot") or 0)
    except (TypeError, ValueError, OverflowError):
        slot = 0

    return Trade(
        tx_signature,
        slot,
        tx.get("blockTime"),
        trader,
        "buy" if delta > 0 else "sell",
        abs(delta),
        sol_amount,
    ), parse_reason


def parse_trade(
    tx: Optional[dict],
    mint: str,
    pool_owners: set[str],
    signature: str = "",
) -> Optional[Trade]:
    """Compatibility wrapper returning a candidate Trade or None."""
    trade, _reason = _parse_trade_with_reason(
        tx,
        mint,
        pool_owners,
        signature,
    )
    return trade


def find_funder(
    tx: Optional[dict],
    wallet: str,
) -> Optional[str]:
    """Find a direct SOL funding source in the supplied transaction."""
    if not tx:
        return None

    instructions = list(
        (
            (tx.get("transaction") or {}).get("message") or {}
        ).get("instructions") or []
    )

    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        instructions.extend(inner.get("instructions") or [])

    for instruction in instructions:
        parsed = (
            instruction.get("parsed")
            if isinstance(instruction, dict)
            else None
        )

        if not isinstance(parsed, dict):
            continue

        if parsed.get("type") not in (
            "transfer",
            "transferWithSeed",
            "createAccount",
        ):
            continue

        info = parsed.get("info") or {}

        destination = (
            info.get("destination")
            or info.get("newAccount")
        )
        source = info.get("source")

        if (
            destination == wallet
            and source
            and source != wallet
        ):
            return source

    return None


# ---------------------------------------------------------------------------
# Results


@dataclass
class TradeStats:
    status: DataStatus
    sample_size: int = 0
    span_minutes: Optional[float] = None
    buys: int = 0
    sells: int = 0
    unique_buyers: int = 0
    unique_sellers: int = 0
    buys_per_buyer: Optional[float] = None
    top5_buyer_share: Optional[float] = None
    buyer_trend: Optional[str] = None
    first_half_buyers: Optional[int] = None
    second_half_buyers: Optional[int] = None
    round_trip_wallets: int = 0
    creator_sold_pct_supply: Optional[float] = None
    creator_bought: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        return self.__dict__ | {
            "status": self.status.value,
        }


@dataclass
class ClusterResult:
    status: DataStatus
    clusters: list[dict] = field(default_factory=list)
    max_cluster_supply_pct: Optional[float] = None
    launch_bundle_pct: Optional[float] = None
    launch_same_slot_wallets: int = 0
    fresh_wallet_share: Optional[float] = None
    wallets_aged: int = 0
    fresh_supply_pct: Optional[float] = None       # % of supply held by wallets younger than 24h
    hub_funders: list[str] = field(default_factory=list)   # busy wallets (exchanges) shared by holders
    two_hop_links: int = 0
    bubbles: list[dict] = field(default_factory=list)      # bubblemap nodes
    links: list[dict] = field(default_factory=list)        # bubblemap funding links
    note: str = ""

    def to_dict(self) -> dict:
        return self.__dict__ | {
            "status": self.status.value,
        }


@dataclass
class WalletAnalysis:
    trades: TradeStats
    clusters: ClusterResult
    flags: list[Flag] = field(default_factory=list)
    gates: list[GateResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Sampling


async def sample_trades(
    rpc: SolanaRpc,
    pool: str,
    mint: str,
    pool_owners: set[str],
    n: int,
    launch: bool = False,
    max_pages: int = 3,
) -> tuple[list[Trade], int]:
    """Fetch a bounded sample of candidate transactions.

    Returns:
        (parsed_candidates, signatures_examined)

    signatures_examined is the number selected for examination, not the
    number of verified swaps. Launch sampling refuses to claim full launch
    coverage if the configured pagination limit is exhausted.
    """
    if n <= 0:
        return [], 0

    max_pages = max(1, int(max_pages))

    try:
        signatures = await rpc.get_signatures(
            pool,
            limit=1000 if launch else min(n, 1000),
        )
    except ProviderError as exc:
        log.warning(
            "Trade sample signature request failed: "
            "pool=%s mint=%s launch=%s error=%s",
            pool,
            mint,
            launch,
            str(exc)[:200],
        )
        return [], 0
    except Exception:
        log.exception(
            "Unexpected signature request failure: "
            "pool=%s mint=%s launch=%s",
            pool,
            mint,
            launch,
        )
        return [], 0

    signatures = signatures or []

    if launch:
        pages = 1

        while (
            signatures
            and len(signatures) % 1000 == 0
            and pages < max_pages
        ):
            before = signatures[-1].get("signature")

            if not before:
                break

            try:
                more = await rpc.get_signatures(
                    pool,
                    limit=1000,
                    before=before,
                )
            except ProviderError as exc:
                log.warning(
                    "Launch sample pagination failed: "
                    "pool=%s mint=%s page=%s error=%s",
                    pool,
                    mint,
                    pages + 1,
                    str(exc)[:200],
                )
                return [], len(signatures)
            except Exception:
                log.exception(
                    "Unexpected launch pagination failure: "
                    "pool=%s mint=%s page=%s",
                    pool,
                    mint,
                    pages + 1,
                )
                return [], len(signatures)

            if not more:
                break

            signatures.extend(more)
            pages += 1

        # A full final page means the oldest signature may not have been
        # reached. Do not present it as a complete launch-time sample.
        if (
            pages >= max_pages
            and len(signatures) >= 1000 * max_pages
        ):
            log.info(
                "Launch sample incomplete: "
                "pool=%s mint=%s signatures=%s pages=%s",
                pool,
                mint,
                len(signatures),
                pages,
            )
            return [], len(signatures)

        signatures = list(reversed(signatures))[:n]

    else:
        signatures = signatures[:n]

    signatures_examined = len(signatures)

    usable_signatures = [
        item
        for item in signatures
        if item.get("signature") and not item.get("err")
    ]

    if not usable_signatures:
        log.info(
            "Trade sample has no usable signatures: "
            "pool=%s mint=%s launch=%s examined=%s",
            pool,
            mint,
            launch,
            signatures_examined,
        )
        return [], signatures_examined

    semaphore = asyncio.Semaphore(8)
    counter_lock = asyncio.Lock()

    counters = {
        "fetched": 0,
        "fetch_failed": 0,
        "invalid_response": 0,
        "transaction_failed": 0,
        "pool_not_in_transaction": 0,
        "no_requested_mint_owner_delta": 0,
        "ambiguous_trader_attribution": 0,
        "zero_or_invalid_delta": 0,
        "no_swap_instruction_or_pool_evidence": 0,
        "candidate_without_decoded_swap": 0,
        "other_unparsed": 0,
        "verified_swaps": 0,
        "pumpswap_unverified": 0,
    }

    async def increment(key: str) -> None:
        async with counter_lock:
            counters[key] += 1

    async def fetch_one(item: dict) -> list[Trade]:
        signature = item["signature"]

        async with semaphore:
            try:
                tx = await rpc.get_transaction(signature)
            except ProviderError as exc:
                await increment("fetch_failed")
                log.debug(
                    "Transaction fetch failed: signature=%s error=%s",
                    signature,
                    str(exc)[:160],
                )
                return []
            except Exception:
                await increment("fetch_failed")
                log.debug(
                    "Unexpected transaction fetch failure: signature=%s",
                    signature,
                    exc_info=True,
                )
                return []

            if not isinstance(tx, dict):
                await increment("invalid_response")
                return []

            await increment("fetched")

            # PumpSwap swaps carry a program-emitted event that can be verified exactly.
            try:
                decoded = decode_pumpswap_transaction(tx, mint, pool, signature)
            except Exception:
                decoded = None
                log.debug("PumpSwap decode exception: signature=%s", signature, exc_info=True)
            if decoded is not None and decoded.trades:
                async with counter_lock:
                    counters["verified_swaps"] += len(decoded.trades)
                return decoded.trades
            if decoded is not None and decoded.reason not in ("no_pumpswap_instruction",):
                await increment("pumpswap_unverified")
                log.debug("PumpSwap swap not verified: signature=%s reason=%s notes=%s",
                          signature, decoded.reason, decoded.notes)

            try:
                trade, reason = _parse_trade_with_reason(
                    tx,
                    mint,
                    pool_owners,
                    signature,
                )
            except Exception:
                await increment("other_unparsed")
                log.debug(
                    "Transaction parsing exception: signature=%s mint=%s",
                    signature,
                    mint,
                    exc_info=True,
                )
                return []

            if trade is None:
                await increment(
                    reason if reason in counters else "other_unparsed"
                )
                return []

            if reason == "candidate_without_decoded_swap":
                await increment("candidate_without_decoded_swap")

            return [trade]

    results = await asyncio.gather(
        *(fetch_one(item) for item in usable_signatures)
    )

    trades = [
        trade
        for batch in results
        for trade in batch
    ]

    log.info(
        "Trade sample result: "
        "pool=%s mint=%s launch=%s "
        "signatures_examined=%s signatures_usable=%s "
        "fetched=%s fetch_failed=%s invalid_response=%s "
        "parsed_candidates=%s failed_tx=%s pool_missing=%s "
        "no_mint_delta=%s ambiguous_trader=%s "
        "candidate_without_decoded_swap=%s other_unparsed=%s "
        "verified_swaps=%s pumpswap_unverified=%s",
        pool,
        mint,
        launch,
        signatures_examined,
        len(usable_signatures),
        counters["fetched"],
        counters["fetch_failed"],
        counters["invalid_response"],
        len(trades),
        counters["transaction_failed"],
        counters["pool_not_in_transaction"],
        counters["no_requested_mint_owner_delta"],
        counters["ambiguous_trader_attribution"],
        counters["candidate_without_decoded_swap"],
        (
            counters["zero_or_invalid_delta"]
            + counters["no_swap_instruction_or_pool_evidence"]
            + counters["other_unparsed"]
        ),
        counters["verified_swaps"],
        counters["pumpswap_unverified"],
    )

    return trades, signatures_examined


# ---------------------------------------------------------------------------
# Pump.fun bonding-curve sampling (verified trades only)


@dataclass
class CurveSample:
    """Result of a bounded recent-transaction sample on a Pump.fun bonding curve.

    ``trades`` contains ONLY trades decoded from a Pump.fun TradeEvent and confirmed against the
    transaction's token balances. Fetched-but-undecoded transactions are counted, never
    converted into trades.
    """

    trades: list[Trade] = field(default_factory=list)
    signatures_examined: int = 0
    signatures_in_window: int = 0
    # True when the signature page provably reached back past the start of the requested
    # window, so the trade count is complete for that window rather than a lower bound.
    window_covered: bool = False
    counters: dict = field(default_factory=dict)
    error: Optional[str] = None

    def undecoded(self) -> int:
        return sum(
            value
            for key, value in self.counters.items()
            if key in CURVE_DECODE_REASONS and key != "decoded"
        )

    def describe(self) -> str:
        """Short human reason used in INSUFFICIENT_DATA messages."""
        if self.error:
            return f"RPC sampling failed: {self.error}"
        if not self.signatures_examined:
            return "no recent signatures on the bonding curve"
        parts = [f"{len(self.trades)} verified trades from {self.signatures_in_window} "
                 f"in-window transactions"]
        unsupported = (self.counters.get("trade_instruction_without_event", 0)
                       + self.counters.get("event_decode_error", 0)
                       + self.counters.get("event_balance_mismatch", 0)
                       + self.counters.get("event_balance_missing", 0))
        if unsupported:
            parts.append(f"{unsupported} trade-like transactions could not be verified")
        parts.append("window fully covered" if self.window_covered
                     else "window not fully covered (count is a lower bound)")
        return "; ".join(parts)


async def sample_curve_trades(
    rpc: SolanaRpc,
    curve: str,
    mint: str,
    n: int,
    min_block_time: Optional[float] = None,
    concurrency: int = 8,
) -> CurveSample:
    """Fetch recent signatures on a bonding curve and decode VERIFIED trades.

    * Signatures older than ``min_block_time`` are discarded BEFORE any transaction is fetched,
      so a quiet token costs one signature request instead of ``n`` transaction requests.
    * Signatures are de-duplicated; failed transactions are skipped.
    * Every transaction lands in exactly one diagnostic counter.
    """
    out = CurveSample()
    if n <= 0:
        return out
    limit = min(int(n), 1000)
    try:
        signatures = await rpc.get_signatures(curve, limit=limit)
    except ProviderError as exc:
        out.error = str(exc)[:200]
        log.warning("Curve sample signature request failed: mint=%s error=%s", mint, out.error)
        return out
    except Exception as exc:  # never let a sampling bug take down research
        out.error = f"{type(exc).__name__}: {str(exc)[:160]}"
        log.exception("Unexpected curve signature failure: mint=%s", mint)
        return out

    signatures = signatures or []
    out.signatures_examined = len(signatures)

    # Newest first. The window is fully covered if the page was not full, or if its oldest
    # entry is already older than the start of the window.
    oldest_time = next(
        (s.get("blockTime") for s in reversed(signatures) if s.get("blockTime")), None
    )
    out.window_covered = (
        len(signatures) < limit
        or (min_block_time is not None and oldest_time is not None and oldest_time < min_block_time)
    )

    counters: dict = {key: 0 for key in CURVE_DECODE_REASONS}
    counters.update({
        "fetched": 0, "fetch_failed": 0, "invalid_response": 0,
        "signature_failed": 0, "signature_duplicate": 0, "outside_window": 0,
        "block_time_from_event": 0, "other_unparsed": 0,
    })

    seen: set[str] = set()
    todo: list[str] = []
    for item in signatures:
        sig = item.get("signature")
        if not sig:
            continue
        if sig in seen:
            counters["signature_duplicate"] += 1
            continue
        seen.add(sig)
        if item.get("err"):
            counters["signature_failed"] += 1
            continue
        bt = item.get("blockTime")
        if min_block_time is not None and bt is not None and bt < min_block_time:
            counters["outside_window"] += 1
            continue
        todo.append(sig)
    out.signatures_in_window = len(todo)

    semaphore = asyncio.Semaphore(max(1, int(concurrency)))

    async def one(sig: str) -> list[Trade]:
        async with semaphore:
            try:
                tx = await rpc.get_transaction(sig)
            except ProviderError:
                counters["fetch_failed"] += 1
                return []
            except Exception:
                counters["fetch_failed"] += 1
                log.debug("Unexpected curve transaction fetch failure: sig=%s", sig, exc_info=True)
                return []
        if not isinstance(tx, dict):
            counters["invalid_response"] += 1
            return []
        counters["fetched"] += 1
        try:
            result = decode_curve_transaction(tx, mint, sig)
        except Exception:
            counters["other_unparsed"] += 1
            log.debug("Curve transaction decode exception: sig=%s", sig, exc_info=True)
            return []
        counters[result.reason] = counters.get(result.reason, 0) + 1
        if "block_time_from_event" in result.notes:
            counters["block_time_from_event"] += 1
        return result.trades

    # asyncio is single-threaded: counter updates between awaits are safe without a lock.
    batches = await asyncio.gather(*(one(sig) for sig in todo))
    trades: list[Trade] = []
    for batch in batches:
        trades.extend(batch)
    trades.sort(key=lambda t: (t.block_time or 0, t.slot))
    out.trades = trades
    out.counters = counters

    log.info(
        "Curve trade sample: mint=%s signatures_examined=%d in_window=%d fetched=%d "
        "fetch_failed=%d verified_trades=%d no_pump_ix=%d non_trade_pump_ix=%d "
        "trade_ix_without_event=%d event_decode_error=%d event_other_mint=%d "
        "balance_missing=%d balance_mismatch=%d failed_tx=%d other_unparsed=%d window_covered=%s",
        mint, out.signatures_examined, out.signatures_in_window, counters["fetched"],
        counters["fetch_failed"], len(trades), counters["no_pump_instruction"],
        counters["non_trade_pump_instruction"] + counters["unknown_pump_instruction"],
        counters["trade_instruction_without_event"], counters["event_decode_error"],
        counters["event_other_mint"], counters["event_balance_missing"],
        counters["event_balance_mismatch"], counters["transaction_failed"],
        counters["other_unparsed"] + counters["invalid_transaction"], out.window_covered,
    )
    return out


# ---------------------------------------------------------------------------
# Trade statistics


def compute_trade_stats(
    trades: list[Trade],
    creator: Optional[str],
    supply_ui: Optional[float],
    sampled: int,
    status: DataStatus = DataStatus.INFERRED,
    note: Optional[str] = None,
) -> TradeStats:
    """Calculate metrics for the supplied trades.

    ``status`` defaults to INFERRED (balance-delta candidates). Callers may pass VERIFIED only
    when every trade in ``trades`` was confirmed from a program-emitted swap event.
    """
    if not trades:
        return TradeStats(
            DataStatus.UNAVAILABLE,
            note=f"no parseable trade candidates in {sampled} signatures",
        )
    if status == DataStatus.VERIFIED and any(
        trade.verification not in VERIFIED_LABELS for trade in trades
    ):
        raise ValueError("VERIFIED trade stats require verified trades only")

    ts = TradeStats(
        status,
        sample_size=len(trades),
        note=note or (
            "metrics describe parsed transaction candidates only; "
            "not complete trading history"
        ),
    )

    times = [
        trade.block_time
        for trade in trades
        if trade.block_time
    ]

    if len(times) >= 2:
        ts.span_minutes = round(
            (max(times) - min(times)) / 60,
            1,
        )

    buys = [
        trade for trade in trades
        if trade.side == "buy"
    ]

    sells = [
        trade for trade in trades
        if trade.side == "sell"
    ]

    buyers = {
        trade.wallet for trade in buys
    }

    sellers = {
        trade.wallet for trade in sells
    }

    ts.buys = len(buys)
    ts.sells = len(sells)
    ts.unique_buyers = len(buyers)
    ts.unique_sellers = len(sellers)

    ts.buys_per_buyer = (
        round(len(buys) / len(buyers), 2)
        if buyers
        else None
    )

    # This is concentration of observed token buying, not current holdings.
    volume_by_wallet = defaultdict(float)

    for trade in buys:
        volume_by_wallet[trade.wallet] += trade.token_amount

    total_bought = sum(volume_by_wallet.values())

    if total_bought > 0:
        top_five = sorted(
            volume_by_wallet.values(),
            reverse=True,
        )[:5]

        ts.top5_buyer_share = round(
            sum(top_five) / total_bought * 100,
            1,
        )

    if len(times) >= 10:
        midpoint = (min(times) + max(times)) / 2

        ordered_buys = sorted(
            buys,
            key=lambda trade: trade.block_time or 0,
        )

        first_half = {
            trade.wallet
            for trade in ordered_buys
            if (trade.block_time or 0) < midpoint
        }

        second_half = {
            trade.wallet
            for trade in ordered_buys
            if (trade.block_time or 0) >= midpoint
        }

        ts.first_half_buyers = len(first_half)
        ts.second_half_buyers = len(second_half)

        if len(first_half) >= 3:
            ratio = len(second_half) / len(first_half)

            ts.buyer_trend = (
                "rising"
                if ratio >= 1.2
                else "declining"
                if ratio <= 0.7
                else "flat"
            )

    sides_by_wallet = defaultdict(set)

    for trade in trades:
        sides_by_wallet[trade.wallet].add(trade.side)

    ts.round_trip_wallets = sum(
        1
        for sides in sides_by_wallet.values()
        if {"buy", "sell"}.issubset(sides)
    )

    if creator:
        creator_sold = sum(
            trade.token_amount
            for trade in sells
            if trade.wallet == creator
        )

        ts.creator_bought = any(
            trade.wallet == creator
            for trade in buys
        )

        ts.creator_sold_pct_supply = (
            round(creator_sold / supply_ui * 100, 3)
            if supply_ui and supply_ui > 0
            else None
        )

    return ts


# ---------------------------------------------------------------------------
# Wallet tracing


async def trace_wallets(
    rpc: SolanaRpc,
    wallets: list[str],
    max_n: int,
) -> dict[str, dict]:
    """Fetch bounded wallet history and identify a direct funding source."""
    out: dict[str, dict] = {}
    semaphore = asyncio.Semaphore(4)

    async def one(wallet: str) -> None:
        async with semaphore:
            try:
                signatures = await rpc.get_signatures(
                    wallet,
                    limit=1000,
                )
            except ProviderError:
                return
            except Exception:
                log.debug(
                    "Wallet signature lookup failed: wallet=%s",
                    wallet,
                    exc_info=True,
                )
                return

            if not signatures:
                return

            complete = len(signatures) < 1000
            oldest = signatures[-1]

            info = {
                "oldest_seen": oldest.get("blockTime"),
                "last_seen": signatures[0].get("blockTime"),
                "history_complete": complete,
                "tx_count_lower_bound": len(signatures),
                # 1000+ transactions: an exchange, router, bot or very active trader, never a
                # throwaway wallet. Wallets that share such a funder are NOT linked by that alone.
                "busy": not complete,
                "funder": None,
            }

            # A full 1000-signature page does not establish complete history.
            if complete and oldest.get("signature"):
                try:
                    tx = await rpc.get_transaction(
                        oldest["signature"]
                    )
                    info["funder"] = find_funder(tx, wallet)
                except ProviderError:
                    pass
                except Exception:
                    log.debug(
                        "Wallet funder lookup failed: wallet=%s",
                        wallet,
                        exc_info=True,
                    )

            out[wallet] = info

    await asyncio.gather(
        *(one(wallet) for wallet in wallets[:max_n])
    )

    return out


async def trace_funders(
    rpc: SolanaRpc,
    traced: dict[str, dict],
    creator: Optional[str] = None,
    max_n: int = 10,
) -> dict[str, dict]:
    """Second hop: who are the funders, and who funded THEM?

    Pros look past the first hop because devs fund buyer wallets through a middle wallet
    (dev -> A -> holder 1, dev -> B -> holder 2). Funders shared by 2+ wallets come first.
    A funder with 1000+ transactions is marked busy (exchange / router / bot hub)."""
    counts: dict[str, int] = defaultdict(int)
    for info in traced.values():
        if info.get("funder"):
            counts[info["funder"]] += 1
    order = sorted((f for f in counts if f not in traced), key=lambda f: -counts[f])[:max_n]
    first = await trace_wallets(rpc, order, max_n) if order else {}
    # third layer only to know whether the grand-funders are hubs (and to link them to the creator)
    grand: dict[str, int] = defaultdict(int)
    for info in first.values():
        if info.get("funder") and info["funder"] not in first and info["funder"] not in traced:
            grand[info["funder"]] += 1
    shared = [g for g, n in sorted(grand.items(), key=lambda kv: -kv[1]) if n >= 2 or g == creator][:5]
    second = await trace_wallets(rpc, shared, len(shared)) if shared else {}
    return {**first, **second}


def bubble_map(
    traced: dict[str, dict],
    funders: dict[str, dict],
    holdings_by_owner: dict[str, float],
    supply_ui: Optional[float],
    creator: Optional[str],
    launch_wallets: set[str],
    parent_of: dict[str, str],
    s: Settings,
    now: float,
) -> tuple[list[dict], list[dict]]:
    """Nodes (holders, linking funders, creator) and funding links, for the visual bubblemap."""
    nodes: dict[str, dict] = {}
    links: list[dict] = []

    def age_h(info: Optional[dict]) -> Optional[float]:
        if info and info.get("history_complete") and info.get("oldest_seen"):
            return round((now - info["oldest_seen"]) / 3600, 1)
        return None

    for wallet, amount in sorted(holdings_by_owner.items(), key=lambda kv: -kv[1])[:20]:
        info = traced.get(wallet)
        a = age_h(info)
        nodes[wallet] = {
            "id": wallet, "kind": "creator" if wallet == creator else "holder",
            "pct": round(amount / supply_ui * 100, 3) if supply_ui else None,
            "age_h": a, "fresh": a is not None and a < s.fresh_wallet_age_hours,
            "busy": bool(info and info.get("busy")), "traced": info is not None,
            "tx": (info or {}).get("tx_count_lower_bound"), "launch_buyer": wallet in launch_wallets,
            "funder": (info or {}).get("funder"), "cluster": parent_of.get(wallet),
        }
    if creator and creator not in nodes:
        nodes[creator] = {"id": creator, "kind": "creator", "pct": None, "age_h": None, "fresh": False,
                          "busy": False, "traced": False, "tx": None, "launch_buyer": False, "funder": None,
                          "cluster": parent_of.get(creator)}
    # funders that link shown wallets (directly, or through a shared grand-funder / the creator)
    # become their own small bubbles, so "dev -> A -> holder" chains are visible
    def funder_node(f: str) -> dict:
        info = funders.get(f) or traced.get(f) or {}
        return {"id": f, "kind": "hub" if info.get("busy") else "funder", "pct": None, "age_h": age_h(info),
                "fresh": False, "busy": bool(info.get("busy")), "traced": bool(info),
                "tx": info.get("tx_count_lower_bound"), "launch_buyer": False, "funder": info.get("funder"),
                "cluster": parent_of.get(f)}

    fund_count: dict[str, int] = defaultdict(int)
    for n in list(nodes.values()):
        if n.get("funder"):
            fund_count[n["funder"]] += 1
    grand_count: dict[str, int] = defaultdict(int)
    for f in fund_count:
        gf = (funders.get(f) or {}).get("funder")
        if gf:
            grand_count[gf] += 1
    for f, k in fund_count.items():
        if f in nodes:
            continue
        gf = (funders.get(f) or {}).get("funder")
        if k >= 2 or (gf and (gf == creator or grand_count.get(gf, 0) >= 2)):
            nodes[f] = funder_node(f)
    for gf, k in grand_count.items():
        if gf not in nodes and k >= 2:
            nodes[gf] = funder_node(gf)
    for n in list(nodes.values()):
        f = n.get("funder")
        if f and f in nodes:
            links.append({"a": n["id"], "b": f, "kind": "funded",
                          "hub": bool(nodes[f].get("busy"))})
    return list(nodes.values()), links


# ---------------------------------------------------------------------------
# Wallet clusters


def build_clusters(
    launch_trades: list[Trade],
    recent_trades: list[Trade],
    traced: dict[str, dict],
    holdings_by_owner: dict[str, float],
    supply_ui: Optional[float],
    creator: Optional[str],
    s: Settings,
    now: Optional[float] = None,
    funders: Optional[dict[str, dict]] = None,
) -> ClusterResult:
    """Infer wallet relationships from funding and sampled trading patterns.

    Funding evidence:
      * same funder that is the creator                  -> CONFIRMED
      * same ordinary funder (a wallet with <1000 txs)    -> STRONG
      * same BUSY funder (exchange / router / bot hub)     -> WEAK (thousands of unrelated
        people withdraw from the same exchange wallet; on its own that proves nothing)
      * one traced wallet funded another                  -> STRONG
      * two funders that were funded by the same ordinary wallet (2 hops: dev -> A -> holder,
        dev -> B -> holder), or a funder funded by the creator -> STRONG / CONFIRMED
    """
    now = now or time.time()
    funders = funders or {}
    result = ClusterResult(DataStatus.INFERRED)

    parent: dict[str, str] = {}
    strength: dict[frozenset, EvidenceStrength] = {}

    def find(wallet: str) -> str:
        parent.setdefault(wallet, wallet)

        while parent[wallet] != wallet:
            parent[wallet] = parent[parent[wallet]]
            wallet = parent[wallet]

        return wallet

    def union(
        first: str,
        second: str,
        evidence: EvidenceStrength,
    ) -> None:
        if first == second:
            return

        root_first = find(first)
        root_second = find(second)

        if root_first != root_second:
            parent[root_first] = root_second

        key = frozenset((first, second))

        if (
            key not in strength
            or _rank(evidence) > _rank(strength[key])
        ):
            strength[key] = evidence

    # Shared funding source.
    by_funder = defaultdict(list)

    for wallet, info in traced.items():
        funder = info.get("funder")

        if funder:
            by_funder[funder].append(wallet)

    hubs = set()
    for funder, wallets in by_funder.items():
        busy = bool((funders.get(funder) or traced.get(funder) or {}).get("busy"))
        if creator and funder == creator:
            evidence = EvidenceStrength.CONFIRMED
        elif busy:
            evidence = EvidenceStrength.WEAK
            if len(wallets) > 1:
                hubs.add(funder)
        else:
            evidence = EvidenceStrength.STRONG

        if evidence == EvidenceStrength.CONFIRMED:
            for wallet in wallets:
                union(wallet, creator, evidence)

        for wallet in wallets[1:]:
            union(wallets[0], wallet, evidence)

        # one traced wallet funded another (dev wallet -> buyer wallet)
        if funder in traced and not busy:
            for wallet in wallets:
                union(funder, wallet, EvidenceStrength.STRONG)

    # second hop: funders funded by the same ordinary wallet, or by the creator
    two_hop: set[str] = set()                 # wallets linked only through a second-hop funder
    by_grand = defaultdict(list)
    for funder, wallets in by_funder.items():
        info = funders.get(funder) or {}
        grand = info.get("funder")
        if not grand or info.get("busy"):
            continue
        if (funders.get(grand) or {}).get("busy"):
            continue
        by_grand[grand].append(wallets)
        if creator and grand == creator:
            for wallet in wallets:
                union(wallet, creator, EvidenceStrength.CONFIRMED)
                two_hop.add(wallet)
    for grand, groups in by_grand.items():
        if len(groups) < 2:
            continue
        for wallets in groups[1:]:
            union(groups[0][0], wallets[0], EvidenceStrength.STRONG)
        for wallets in groups:
            two_hop.update(wallets)
    result.hub_funders = sorted(hubs)
    result.two_hop_links = len(two_hop)

    # Same-slot buying is only a coordination heuristic.
    by_slot = defaultdict(list)

    for trade in launch_trades + recent_trades:
        if trade.side == "buy":
            by_slot[trade.slot].append(trade)

    for slot_trades in by_slot.values():
        wallets = list({
            trade.wallet for trade in slot_trades
        })

        if len(wallets) < 3:
            continue

        sizes = [
            trade.token_amount
            for trade in slot_trades
            if trade.token_amount > 0
        ]

        if not sizes:
            continue

        mean_size = statistics.mean(sizes)

        coefficient = (
            statistics.pstdev(sizes) / mean_size
            if mean_size
            else 1
        )

        evidence = (
            EvidenceStrength.STRONG
            if coefficient < 0.15
            else EvidenceStrength.WEAK
        )

        for wallet in wallets[1:]:
            union(wallets[0], wallet, evidence)

    groups = defaultdict(set)

    for wallet in list(parent):
        groups[find(wallet)].add(wallet)

    for members in groups.values():
        if len(members) < 2:
            continue

        group_strengths = [
            evidence
            for key, evidence in strength.items()
            if key.issubset(members)
        ]

        best_strength = (
            max(group_strengths, key=_rank)
            if group_strengths
            else EvidenceStrength.WEAK
        )

        held = sum(
            holdings_by_owner.get(wallet, 0)
            for wallet in members
        )

        held_pct = (
            round(held / supply_ui * 100, 2)
            if supply_ui and supply_ui > 0
            else None
        )

        result.clusters.append({
            "size": len(members),
            "strength": best_strength.value,
            "held_pct_of_supply_top20_only": held_pct,
            "members": sorted(members)[:8],
        })

    result.clusters.sort(
        key=lambda cluster: -(
            cluster["held_pct_of_supply_top20_only"] or 0
        )
    )

    gateable = [
        cluster["held_pct_of_supply_top20_only"] or 0
        for cluster in result.clusters
        if cluster["strength"] in ("CONFIRMED", "STRONG")
    ]

    result.max_cluster_supply_pct = (
        max(gateable)
        if gateable
        else 0.0
        if traced or launch_trades
        else None
    )

    # This calculation is meaningful only if launch_trades actually represent
    # a sufficiently complete launch-time sample.
    if launch_trades:
        first_slot = min(
            trade.slot for trade in launch_trades
        )

        early_trades = [
            trade
            for trade in launch_trades
            if (
                trade.side == "buy"
                and trade.slot <= first_slot + 1
            )
        ]

        result.launch_same_slot_wallets = len({
            trade.wallet
            for trade in early_trades
            if trade.wallet != creator
        })

        if supply_ui and supply_ui > 0:
            result.launch_bundle_pct = round(
                sum(
                    trade.token_amount
                    for trade in early_trades
                ) / supply_ui * 100,
                2,
            )

    ages = [
        (now - info["oldest_seen"]) / 3600
        for info in traced.values()
        if (
            info.get("history_complete")
            and info.get("oldest_seen")
        )
    ]

    incomplete_histories = [
        info
        for info in traced.values()
        if not info.get("history_complete")
    ]

    result.wallets_aged = len(ages) + len(incomplete_histories)

    if result.wallets_aged:
        fresh_count = sum(
            1
            for age in ages
            if age < s.fresh_wallet_age_hours
        )

        # An incomplete history is not automatically a fresh wallet.
        # It does, however, remain part of the denominator.
        result.fresh_wallet_share = round(
            fresh_count / result.wallets_aged * 100,
            1,
        )

    # share of supply held by brand-new wallets (top holders only)
    if supply_ui and supply_ui > 0 and traced:
        fresh_amount = sum(
            amount for wallet, amount in holdings_by_owner.items()
            if (info := traced.get(wallet)) and info.get("history_complete") and info.get("oldest_seen")
            and (now - info["oldest_seen"]) / 3600 < s.fresh_wallet_age_hours
        )
        result.fresh_supply_pct = round(fresh_amount / supply_ui * 100, 2)

    launch_wallets = {trade.wallet for trade in launch_trades if trade.side == "buy"}
    # stable cluster ids: the index of the (sorted) cluster each wallet belongs to
    cid = {}
    groups_sorted = defaultdict(set)
    for wallet in list(parent):
        groups_sorted[find(wallet)].add(wallet)
    order = {}
    for i, c in enumerate(result.clusters):
        for member in c["members"]:
            order[find(member)] = i
    for root, members in groups_sorted.items():
        if len(members) >= 2 and root in order:
            for member in members:
                cid[member] = order[root]
    result.bubbles, result.links = bubble_map(traced, funders, holdings_by_owner, supply_ui, creator,
                                              launch_wallets, cid, s, now)
    for node in result.bubbles:
        c = node.get("cluster")
        node["cluster_strength"] = result.clusters[c]["strength"] if c is not None else None

    if not traced and not launch_trades:
        result.status = DataStatus.UNAVAILABLE
        result.note = (
            "no funding traces or launch trades available"
        )

    return result


def _rank(evidence: EvidenceStrength) -> int:
    return {
        "UNKNOWN": 0,
        "WEAK": 1,
        "STRONG": 2,
        "CONFIRMED": 3,
    }[evidence.value]


# ---------------------------------------------------------------------------
# Findings and risk gates


def wallet_network_gate(cr: "ClusterResult", s) -> GateResult:
    """v2.6.1 `wallet_network`: who actually holds the coin. FAIL when brand-new wallets hold a big
    share, when almost every traced wallet is brand new, or when one group of wallets linked by
    shared funding holds a big share. UNKNOWN when the wallets could not be traced."""
    max_fresh_supply = float(getattr(s, "max_fresh_supply_pct", 15.0) or 0)
    max_fresh_share = float(getattr(s, "max_fresh_wallet_share", 80.0) or 0)
    max_linked = float(getattr(s, "max_linked_supply_pct", 8.0) or 0)
    if cr.status == DataStatus.UNAVAILABLE or not cr.wallets_aged:
        return GateResult("wallet_network", GateStatus.UNKNOWN, "holder wallets could not be traced", critical=False)
    fails = []
    if max_fresh_supply and cr.fresh_supply_pct is not None and cr.fresh_supply_pct >= max_fresh_supply:
        fails.append(f"brand-new wallets (<{s.fresh_wallet_age_hours:.0f}h) hold {cr.fresh_supply_pct:.1f}% of supply")
    if (max_fresh_share and cr.fresh_wallet_share is not None and cr.wallets_aged >= 5
            and cr.fresh_wallet_share >= max_fresh_share):
        fails.append(f"{cr.fresh_wallet_share:.0f}% of {cr.wallets_aged} traced holder/buyer wallets are brand new")
    if max_linked and cr.max_cluster_supply_pct is not None and cr.max_cluster_supply_pct >= max_linked:
        biggest = max((c for c in cr.clusters or []), key=lambda c: c.get("held_pct_of_supply_top20_only") or 0,
                      default={})
        fails.append(f"{biggest.get('size', '?')} wallets linked by shared funding hold "
                     f"{cr.max_cluster_supply_pct:.1f}% of supply")
    parts = [f"{cr.wallets_aged} wallets traced"]
    if cr.fresh_wallet_share is not None:
        parts.append(f"{cr.fresh_wallet_share:.0f}% brand new")
    if cr.fresh_supply_pct is not None:
        parts.append(f"new wallets hold {cr.fresh_supply_pct:.1f}%")
    if cr.max_cluster_supply_pct is not None:
        parts.append(f"largest linked group {cr.max_cluster_supply_pct:.1f}%")
    if cr.two_hop_links:
        parts.append(f"{cr.two_hop_links} hidden 2-hop funding links")
    if fails:
        return GateResult("wallet_network", GateStatus.FAIL, "; ".join(fails), critical=False)
    return GateResult("wallet_network", GateStatus.PASS, ", ".join(parts), critical=False)


def wallet_findings(
    ts: TradeStats,
    cr: ClusterResult,
    dex_txns_h1: Optional[int],
    s: Settings,
) -> tuple[list[Flag], list[GateResult]]:
    """Generate sample-based wallet findings and risk gates."""
    flags: list[Flag] = []
    gates: list[GateResult] = []

    # Creator dumping.
    # Observed high sales can fail the gate. Low observed sales cannot prove
    # the creator did not sell outside this sample, so the gate remains UNKNOWN.
    if (
        ts.creator_sold_pct_supply is not None
        and ts.creator_sold_pct_supply
        > s.max_creator_sell_pct_of_supply
    ):
        # Sales decoded from program-emitted swap events are direct on-chain evidence.
        verified_sample = ts.status == DataStatus.VERIFIED
        flags.append(
            Flag(
                "CREATOR_DUMPING",
                Severity.CRITICAL,
                f"creator sold {ts.creator_sold_pct_supply:.2f}% "
                "of supply within the sampled trades"
                + (" (verified swap events)" if verified_sample else ""),
                DataStatus.VERIFIED if verified_sample else DataStatus.INFERRED,
                EvidenceStrength.CONFIRMED if verified_sample else EvidenceStrength.STRONG,
            )
        )

        gates.append(
            GateResult(
                "creator_dumping",
                GateStatus.FAIL,
                f"creator sold {ts.creator_sold_pct_supply:.2f}% "
                "of supply (sample)",
            )
        )
    else:
        gates.append(
            GateResult(
                "creator_dumping",
                GateStatus.UNKNOWN,
                "sampled creator sales cannot prove that no creator "
                "selling occurred",
                critical=False,
            )
        )

    # Concentration of observed token-buy activity, not current holdings.
    if (
        ts.top5_buyer_share is not None
        and ts.top5_buyer_share > 60
        and ts.unique_buyers >= 5
    ):
        flags.append(
            Flag(
                "BUYING_CONCENTRATED",
                Severity.MEDIUM,
                f"5 wallets account for "
                f"{ts.top5_buyer_share:.0f}% of sampled buy volume",
                DataStatus.INFERRED,
                EvidenceStrength.WEAK,
            )
        )

    if ts.buys_per_buyer and ts.buys_per_buyer > 3:
        flags.append(
            Flag(
                "POSSIBLE_MANIPULATION_REPEAT_BUYS",
                Severity.MEDIUM,
                f"possible manipulation: {ts.buys_per_buyer:.1f} "
                "buys per unique buyer in sample",
                DataStatus.INFERRED,
                EvidenceStrength.WEAK,
            )
        )

    if (
        ts.sample_size
        and ts.round_trip_wallets / max(ts.unique_buyers, 1) > 0.4
        and ts.unique_buyers >= 5
    ):
        flags.append(
            Flag(
                "POSSIBLE_WASH_TRADING",
                Severity.HIGH,
                f"possible manipulation: {ts.round_trip_wallets} "
                "wallets both bought and sold in the sample",
                DataStatus.INFERRED,
                EvidenceStrength.WEAK,
            )
        )

    if ts.buyer_trend == "declining":
        flags.append(
            Flag(
                "DECLINING_BUYERS",
                Severity.MEDIUM,
                f"unique buyers fell from {ts.first_half_buyers} "
                f"to {ts.second_half_buyers} across the sample",
                DataStatus.INFERRED,
            )
        )

    # Compare reported transaction activity with observed distinct buyers.
    if (
        dex_txns_h1
        and ts.span_minutes
        and ts.unique_buyers
        and ts.span_minutes > 0
    ):
        implied_per_hour = (
            ts.unique_buyers / ts.span_minutes * 60
        )

        if dex_txns_h1 > 40 * max(implied_per_hour, 1):
            flags.append(
                Flag(
                    "TXN_COUNT_VS_UNIQUE",
                    Severity.MEDIUM,
                    "possible manipulation: reported transaction count "
                    "is very large relative to distinct wallets observed",
                    DataStatus.INFERRED,
                    EvidenceStrength.WEAK,
                )
            )

    # Connected-wallet concentration.
    if (
        cr.max_cluster_supply_pct is not None
        and cr.max_cluster_supply_pct > s.max_cluster_supply_pct
    ):
        flags.append(
            Flag(
                "CONNECTED_WALLET_SUPPLY",
                Severity.CRITICAL,
                "linked wallets (shared funding / coordinated buys) "
                f"hold {cr.max_cluster_supply_pct:.1f}% of supply "
                "among top-20 holders",
                DataStatus.INFERRED,
                EvidenceStrength.STRONG,
            )
        )

        gates.append(
            GateResult(
                "bundled_supply",
                GateStatus.FAIL,
                f"linked-wallet cluster holds "
                f"{cr.max_cluster_supply_pct:.1f}%",
            )
        )

    # Launch bundle.
    if (
        cr.launch_bundle_pct is not None
        and cr.launch_bundle_pct > s.max_cluster_supply_pct
    ):
        flags.append(
            Flag(
                "LAUNCH_BUNDLE",
                Severity.HIGH,
                f"{cr.launch_same_slot_wallets} wallets bought "
                f"{cr.launch_bundle_pct:.1f}% of supply "
                "in the first two slots",
                DataStatus.INFERRED,
                EvidenceStrength.STRONG,
            )
        )

        gates.append(
            GateResult(
                "launch_bundle",
                GateStatus.FAIL,
                f"launch bundle {cr.launch_bundle_pct:.1f}% of supply",
            )
        )

    if not any(
        gate.name in ("bundled_supply", "launch_bundle")
        for gate in gates
    ):
        # No detected cluster is not automatically a clean PASS when the
        # relevant evidence is absent.
        known = (
            cr.status != DataStatus.UNAVAILABLE
            and (
                bool(cr.clusters)
                or cr.max_cluster_supply_pct is not None
            )
        )

        gates.append(
            GateResult(
                "bundled_supply",
                GateStatus.PASS if known else GateStatus.UNKNOWN,
                (
                    "no linked-wallet cluster above limit in available sample"
                    if known
                    else "bundle analysis unavailable or incomplete"
                ),
                critical=False,
            )
        )

    gates.append(wallet_network_gate(cr, s))

    # Fresh-wallet concentration.
    if (
        cr.fresh_wallet_share is not None
        and cr.fresh_wallet_share > 60
        and cr.wallets_aged >= 5
    ):
        flags.append(
            Flag(
                "FRESH_WALLETS",
                Severity.MEDIUM,
                f"{cr.fresh_wallet_share:.0f}% of traced wallets are "
                f"under {s.fresh_wallet_age_hours:.0f}h old or have "
                "incomplete age history",
                DataStatus.INFERRED,
                EvidenceStrength.WEAK,
            )
        )

    return flags, gates
