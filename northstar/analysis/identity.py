"""Mint validation and pool selection. The mint address is the only identity we trust."""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Optional

import base58

from ..models import DataStatus, Datum, PairInfo
from ..providers.solana_rpc import QUOTE_MINTS, TOKEN_2022_PROGRAM, TOKEN_PROGRAM


def is_valid_mint_format(addr: str) -> bool:
    """Base58 string that decodes to exactly 32 bytes."""
    if not isinstance(addr, str) or not (32 <= len(addr) <= 44):
        return False
    try:
        return len(base58.b58decode(addr)) == 32
    except ValueError:
        return False


@dataclass
class MintAccount:
    exists: Datum[bool]
    program: Datum[str]
    is_token_2022: bool = False
    mint_authority: Datum[Optional[str]] = field(default_factory=lambda: Datum.unknown("rpc"))
    freeze_authority: Datum[Optional[str]] = field(default_factory=lambda: Datum.unknown("rpc"))
    supply_ui: Datum[float] = field(default_factory=lambda: Datum.unknown("rpc"))
    decimals: Optional[int] = None
    extensions: Datum[list] = field(default_factory=lambda: Datum.unknown("rpc"))

    @property
    def verified_mint(self) -> bool:
        return self.exists.status == DataStatus.VERIFIED and bool(self.exists.value)


def parse_mint_account(value: Optional[dict], source: str = "solana-rpc") -> MintAccount:
    """Interpret a getAccountInfo(jsonParsed) result for a mint address."""
    now = time.time()
    if value is None:
        return MintAccount(exists=Datum(False, DataStatus.VERIFIED, source, now, "account not found"),
                           program=Datum.unknown(source))
    owner = value.get("owner")
    data = value.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    if owner not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM) or not isinstance(parsed, dict) \
            or parsed.get("type") != "mint":
        return MintAccount(exists=Datum(False, DataStatus.VERIFIED, source, now,
                                        f"account is not an SPL mint (owner={owner})"),
                           program=Datum(owner, DataStatus.VERIFIED, source, now))
    info = parsed.get("info") or {}
    decimals = info.get("decimals")
    supply_ui = None
    try:
        supply_ui = int(info.get("supply")) / (10 ** int(decimals))
    except (TypeError, ValueError):
        pass
    exts = info.get("extensions") if owner == TOKEN_2022_PROGRAM else []
    return MintAccount(
        exists=Datum(True, DataStatus.VERIFIED, source, now),
        program=Datum(owner, DataStatus.VERIFIED, source, now),
        is_token_2022=owner == TOKEN_2022_PROGRAM,
        mint_authority=Datum(info.get("mintAuthority"), DataStatus.VERIFIED, source, now),
        freeze_authority=Datum(info.get("freezeAuthority"), DataStatus.VERIFIED, source, now),
        supply_ui=Datum(supply_ui, DataStatus.VERIFIED if supply_ui is not None else DataStatus.UNKNOWN,
                        source, now),
        decimals=decimals if isinstance(decimals, int) else None,
        extensions=Datum(exts or [], DataStatus.VERIFIED, source, now),
    )


@dataclass
class PoolSelection:
    pair: Optional[PairInfo]
    reason: str
    candidates: int
    price_dispersion_pct: Optional[float] = None
    inconsistent_pools: list[str] = field(default_factory=list)
    inverted_pools: int = 0


def select_pool(mint: str, pairs: list[PairInfo]) -> PoolSelection:
    """Pick the most reliable pool for `mint`. Never blindly the first one returned.

    Rules: base token must be the mint; prefer SOL/USDC/USDT quotes; require a price; drop pools
    whose price disagrees >20% with the liquidity-weighted median; then highest liquidity,
    tie-broken by 1h activity.
    """
    own = [p for p in pairs if p.base_mint == mint]
    inverted = sum(1 for p in pairs if p.quote_mint == mint and p.base_mint != mint)
    if not own:
        return PoolSelection(None, "no pool with this mint as base token", 0, inverted_pools=inverted)
    priced = [p for p in own if p.price_usd and p.price_usd > 0 and (p.liquidity_usd or 0) > 0]
    if not priced:
        return PoolSelection(None, "no pool with a usable USD price and liquidity", len(own),
                             inverted_pools=inverted)
    standard = [p for p in priced if p.quote_mint in QUOTE_MINTS] or priced
    weights = [(p.price_usd, p.liquidity_usd or 0) for p in standard]
    median = _weighted_median(weights)
    consistent, inconsistent = [], []
    for p in standard:
        dev = abs(p.price_usd / median - 1) * 100 if median else 0
        (consistent if dev <= 20 else inconsistent).append(p)
    if not consistent:
        consistent = standard
    best = max(consistent, key=lambda p: ((p.liquidity_usd or 0),
                                          ((p.txns["h1"]["buys"] or 0) + (p.txns["h1"]["sells"] or 0))))
    prices = [p.price_usd for p in standard]
    dispersion = (max(prices) / min(prices) - 1) * 100 if len(prices) > 1 and min(prices) > 0 else 0.0
    reason = (f"highest liquidity (${best.liquidity_usd:,.0f}) among {len(consistent)} price-consistent "
              f"{QUOTE_MINTS.get(best.quote_mint, best.quote_symbol or '?')}-quoted pool(s) on {best.dex_id}")
    return PoolSelection(best, reason, len(own), round(dispersion, 2),
                         [p.pair_address for p in inconsistent], inverted)


def _weighted_median(values: list[tuple[float, float]]) -> float:
    if not values:
        return 0.0
    total = sum(w for _, w in values)
    if total <= 0:
        return statistics.median(v for v, _ in values)
    acc = 0.0
    for v, w in sorted(values):
        acc += w
        if acc >= total / 2:
            return v
    return values[-1][0]
