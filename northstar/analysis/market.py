"""Evidence-led market structure analysis using provider windows and Northstar snapshots.

DexScreener's public API does not expose OHLCV candles. Consequently this module never
pretends sparse snapshots are candles: it measures the observed snapshot path, combines it
with provider price-change/volume/transaction windows, and lowers confidence when history is thin.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Optional

from ..config import Settings
from ..models import DataStatus, Flag, PairInfo, Severity


@dataclass
class MarketStructure:
    status: DataStatus
    extended: bool = False
    vertical_thin: bool = False
    fading: bool = False
    constructive: bool = False
    pullback_pct_from_high: Optional[float] = None
    consolidating: Optional[bool] = None
    volume_sustained: Optional[bool] = None
    mcap_unsupported: Optional[bool] = None
    snapshots_used: int = 0
    # Additional measured features. These are snapshot-path metrics, not candle indicators.
    observed_return_pct: Optional[float] = None
    observed_drawdown_pct: Optional[float] = None
    price_path_volatility_pct: Optional[float] = None
    liquidity_change_pct: Optional[float] = None
    mcap_change_pct: Optional[float] = None
    buy_sell_ratio_h1: Optional[float] = None
    transaction_imbalance_h1_pct: Optional[float] = None
    volume_acceleration: Optional[float] = None
    entry_quality: str = "UNKNOWN"
    entry_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "flags"} | {
            "status": self.status.value,
            "flags": [f.to_dict() for f in self.flags],
        }


def _valid_history(history: list[dict], pair: PairInfo) -> list[dict]:
    rows = []
    for row in history:
        if row.get("pair_address") != pair.pair_address:
            continue
        try:
            price = float(row.get("price_usd"))
            ts = float(row.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and math.isfinite(price) and ts > 0:
            rows.append(row | {"price_usd": price, "ts": ts})
    return sorted(rows, key=lambda r: r["ts"])


def _pct_change(first: Optional[float], last: Optional[float]) -> Optional[float]:
    if first is None or last is None or first <= 0:
        return None
    return round((last / first - 1) * 100, 2)


def analyze_market(pair: PairInfo, history: list[dict], s: Settings) -> MarketStructure:
    pc = pair.price_change or {}
    m5, h1, h6, h24 = (pc.get(k) for k in ("m5", "h1", "h6", "h24"))
    ms = MarketStructure(DataStatus.OBSERVED if any(v is not None for v in (m5, h1, h6, h24)) else DataStatus.UNAVAILABLE)
    liq = pair.liquidity_usd or 0

    if (h6 is not None and h6 > s.max_price_extension_h6_pct) or \
            (h24 is not None and h24 > s.max_price_extension_h6_pct * 2):
        ms.extended = True
        h6_text = f"{h6:+.0f}%" if h6 is not None else "n/a"
        h24_text = f"{h24:+.0f}%" if h24 is not None else "n/a"
        ms.flags.append(Flag("EXTENDED", Severity.MEDIUM,
                             f"price already extended (6h {h6_text}, 24h {h24_text}) — late-entry risk",
                             DataStatus.OBSERVED))
        ms.entry_reasons.append("Recent price expansion increases late-entry risk")

    if m5 is not None and m5 > 30 and liq < 3 * s.min_liquidity_usd:
        ms.vertical_thin = True
        ms.flags.append(Flag("VERTICAL_THIN_LIQUIDITY", Severity.MEDIUM,
                             f"vertical move ({m5:+.0f}% in 5m) on thin liquidity", DataStatus.OBSERVED))
        ms.entry_reasons.append("Fast 5m move on liquidity below 3x minimum")

    if h6 is not None and h1 is not None and h6 > 100 and h1 < -20:
        ms.fading = True
        ms.flags.append(Flag("FADING_AFTER_PUMP", Severity.HIGH,
                             f"sharp expansion ({h6:+.0f}% 6h) now fading ({h1:+.0f}% 1h)",
                             DataStatus.OBSERVED))
        ms.entry_reasons.append("Large move is reversing; continuation is not confirmed")

    if h1 is not None and h6 is not None and h1 > 0 and h6 > 0 and not ms.extended and not ms.vertical_thin:
        ms.constructive = True
        ms.entry_reasons.append("1h and 6h price changes are positive without the configured extension flag")

    vh1, vh6 = (pair.vol or {}).get("h1"), (pair.vol or {}).get("h6")
    if vh1 is not None and vh6 is not None and vh6 > 0:
        ms.volume_sustained = vh1 >= (vh6 / 6) * 0.8
        if not ms.volume_sustained:
            ms.entry_reasons.append("1h volume is below 80% of the 6h hourly average")
        else:
            ms.entry_reasons.append("1h volume is holding near or above the 6h hourly average")
        ms.volume_acceleration = round(vh1 / (vh6 / 6), 2)

    buys = (pair.txns or {}).get("h1", {}).get("buys")
    sells = (pair.txns or {}).get("h1", {}).get("sells")
    if buys is not None and sells is not None:
        total = buys + sells
        ms.buy_sell_ratio_h1 = round(buys / sells, 3) if sells else (None if buys else 0.0)
        ms.transaction_imbalance_h1_pct = round((buys - sells) / total * 100, 2) if total else 0.0
        if total >= 20 and buys < sells:
            ms.entry_reasons.append("Observed 1h transaction count is sell-dominant (counts are not unique wallets)")
        elif total >= 20 and buys > sells:
            ms.entry_reasons.append("Observed 1h transaction count is buy-dominant (counts are not unique wallets)")

    rows = _valid_history(history, pair)
    ms.snapshots_used = len(rows)
    if len(rows) >= 2 and pair.price_usd and pair.price_usd > 0:
        prices = [r["price_usd"] for r in rows] + [pair.price_usd]
        ms.observed_return_pct = _pct_change(prices[0], prices[-1])
        peak = prices[0]
        max_dd = 0.0
        for price in prices:
            peak = max(peak, price)
            if peak > 0:
                max_dd = max(max_dd, (peak - price) / peak * 100)
        ms.observed_drawdown_pct = round(max_dd, 2)
        high = max(prices)
        ms.pullback_pct_from_high = round((1 - pair.price_usd / high) * 100, 2) if high else None
        # Relative range is only meaningful for at least 4 observations.
        if len(prices) >= 5:
            tail = prices[-5:]
            rng = (max(tail) / min(tail) - 1) * 100 if min(tail) > 0 else None
            ms.consolidating = rng is not None and rng < 15
            returns = [(b / a - 1) * 100 for a, b in zip(tail, tail[1:]) if a > 0]
            ms.price_path_volatility_pct = round(statistics.pstdev(returns), 2) if len(returns) >= 2 else None
        first = rows[0]
        for key, dest in (("liquidity_usd", "liquidity_change_pct"), ("market_cap", "mcap_change_pct")):
            try:
                start = float(first.get(key)) if first.get(key) is not None else None
                end = liq if key == "liquidity_usd" else pair.market_cap
                setattr(ms, dest, _pct_change(start, end))
            except (TypeError, ValueError):
                pass
        if first.get("market_cap") and first.get("liquidity_usd") and pair.market_cap and liq:
            try:
                mcap_growth = pair.market_cap / float(first["market_cap"])
                liq_growth = liq / float(first["liquidity_usd"])
                ms.mcap_unsupported = mcap_growth > 3 and liq_growth < 1.3
                if ms.mcap_unsupported:
                    ms.flags.append(Flag("MCAP_UNSUPPORTED", Severity.MEDIUM,
                                         f"market cap x{mcap_growth:.1f} while liquidity x{liq_growth:.1f} since first "
                                         "observation", DataStatus.OBSERVED))
                    ms.entry_reasons.append("Market cap growth is not being matched by liquidity growth")
            except (TypeError, ValueError, ZeroDivisionError):
                ms.notes.append("market-cap/liquidity growth comparison unavailable due to invalid snapshot values")
    else:
        ms.notes.append(f"only {len(rows)} prior valid snapshot(s); snapshot-path metrics not assessed")

    if ms.fading or ms.vertical_thin:
        ms.entry_quality = "POOR"
    elif ms.extended:
        ms.entry_quality = "LATE_OR_WAIT"
    elif ms.status == DataStatus.UNAVAILABLE or ms.snapshots_used < 2:
        # Provider windows alone are not an observed entry structure for this pool.
        ms.entry_quality = "INSUFFICIENT_HISTORY"
    elif ms.constructive and (ms.volume_sustained is not False) and (ms.buy_sell_ratio_h1 is None or ms.buy_sell_ratio_h1 >= 1):
        ms.entry_quality = "POTENTIALLY_CONSTRUCTIVE"
    elif ms.consolidating and ms.pullback_pct_from_high is not None and 5 <= ms.pullback_pct_from_high <= 35:
        ms.entry_quality = "WATCH_PULLBACK"
    else:
        ms.entry_quality = "UNCONFIRMED"
    return ms
