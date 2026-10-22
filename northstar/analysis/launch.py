"""How did the coin START trading? An organic launch climbs step by step on many small trades; a
rigged one jumps in a candle or two (bundled or sniped supply), moves in a few giant steps with dead
time in between, or has already been dumped (v2.5.7, operator request 22 Oct).

Input: 1-minute candles (ts, open, high, low, close, volume_usd) from the coin's EARLIEST pool, from
its creation onwards (GeckoTerminal), plus the current price. Output: a LaunchProfile and the
`launch_shape` gate:

  FAIL     instant jump:   the first 2 minutes of trading reached >= `launch_max_first_jump_x` the
                           first trade price (bundled / sniped launch)
           staircase:      >= 3x total rise, >= 85% of it from the 3 biggest one-minute candles, and
                           trades in < 40% of the first 30 minutes (a few big coordinated buys)
           already dumped: the price is <= `launch_max_drawdown_x` of its high since launch
  PASS     enough candles and none of the above
  UNKNOWN  no candles (provider down, pool not indexed yet): recorded, never treated as organic

The thresholds are deliberately wide: they target the obvious cases. The metrics are stored on every
report so they can be measured against outcomes and tightened with evidence.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Optional

Candle = tuple  # (ts, open, high, low, close, volume_usd)


@dataclass
class LaunchProfile:
    status: str = "UNAVAILABLE"            # OBSERVED / UNAVAILABLE
    pool: Optional[str] = None
    dex: Optional[str] = None
    created_at: Optional[float] = None
    candles: int = 0
    minutes_observed: float = 0.0
    first_price: Optional[float] = None
    first_jump_x: Optional[float] = None    # highest price in the first 2 trading minutes / first open
    rise_x: Optional[float] = None          # highest price in the launch window / first open
    top3_share: Optional[float] = None      # share of the log-rise made by the 3 biggest 1-min candles
    active_share: Optional[float] = None    # share of the first 30 minutes that had trades
    ath_x: Optional[float] = None           # highest price seen since launch / first open
    now_vs_ath: Optional[float] = None      # current price / highest price since launch
    verdict: str = "UNKNOWN"                # ORGANIC / INSTANT_JUMP / STAIRCASE / DUMPED / UNKNOWN
    reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def analyze(candles: list, created_at: Optional[float], now_price: Optional[float], *,
            max_first_jump_x: float = 6.0, max_drawdown_x: float = 0.35, window_min: float = 60.0,
            pool: Optional[str] = None, dex: Optional[str] = None) -> LaunchProfile:
    lp = LaunchProfile(pool=pool, dex=dex, created_at=created_at)
    rows = sorted(c for c in candles or [] if isinstance(c, (list, tuple)) and len(c) >= 6
                  and all(isinstance(x, (int, float)) for x in c[:6]) and c[1] > 0 and c[2] > 0)
    if len(rows) < 3:
        lp.reasons.append("not enough launch candles to judge the start")
        return lp
    t0 = rows[0][0]
    first = rows[0][1]
    lp.status, lp.candles, lp.first_price = "OBSERVED", len(rows), first
    lp.minutes_observed = round((rows[-1][0] - t0) / 60 + 1, 1)
    early = [c for c in rows if c[0] < t0 + 120]
    lp.first_jump_x = round(max(c[2] for c in early) / first, 3)
    win = [c for c in rows if c[0] < t0 + window_min * 60]
    peak_win = max(c[2] for c in win)
    lp.rise_x = round(peak_win / first, 3)
    lp.ath_x = round(max(c[2] for c in rows) / first, 3)
    # where did the rise come from? one-minute gains measured from the previous close
    gains, prev = [], first
    for c in win:
        if c[2] > prev > 0:
            gains.append(math.log(c[2] / prev))
        prev = c[4] if c[4] > 0 else prev
    total = math.log(peak_win / first) if peak_win > first else 0.0
    if total > 0 and gains:
        lp.top3_share = round(min(1.0, sum(sorted(gains, reverse=True)[:3]) / total), 3)
    span = min(30.0, max(1.0, (min(rows[-1][0], t0 + 1800) - t0) / 60 + 1))
    lp.active_share = round(min(1.0, len([c for c in rows if c[0] < t0 + 1800]) / span), 3)
    ath = max(c[2] for c in rows)
    if now_price and now_price > 0:
        lp.now_vs_ath = round(now_price / ath, 3)

    if lp.first_jump_x >= max_first_jump_x:
        lp.verdict = "INSTANT_JUMP"
        lp.reasons.append(f"launch jumped {lp.first_jump_x:.1f}x in its first 2 minutes of trading "
                          f"(bundled or sniped supply), not an organic start")
    elif (lp.rise_x >= 3 and lp.top3_share is not None and lp.top3_share >= 0.85
          and lp.active_share is not None and lp.active_share < 0.4 and span >= 10):
        lp.verdict = "STAIRCASE"
        lp.reasons.append(f"{lp.top3_share * 100:.0f}% of the {lp.rise_x:.1f}x launch rise came from 3 one-minute "
                          f"candles, with trades in only {lp.active_share * 100:.0f}% of the first "
                          f"{span:.0f} minutes (a few big buys, not organic demand)")
    elif lp.now_vs_ath is not None and lp.ath_x >= 2 and lp.now_vs_ath <= max_drawdown_x:
        lp.verdict = "DUMPED"
        lp.reasons.append(f"already dumped: price is {lp.now_vs_ath * 100:.0f}% of its high since launch")
    else:
        lp.verdict = "ORGANIC"
        lp.reasons.append(f"organic start: {lp.first_jump_x:.1f}x in the first 2 minutes"
                          + (f", trades in {lp.active_share * 100:.0f}% of the first minutes"
                             if lp.active_share is not None else ""))
    return lp


def gate(lp: LaunchProfile) -> tuple[str, str]:
    """(status, reason) for the `launch_shape` gate."""
    if lp.status != "OBSERVED" or lp.verdict == "UNKNOWN":
        return "UNKNOWN", lp.reasons[0] if lp.reasons else "launch candles unavailable"
    if lp.verdict in ("INSTANT_JUMP", "STAIRCASE", "DUMPED"):
        return "FAIL", lp.reasons[0]
    return "PASS", lp.reasons[0]


def launch_pool(pairs: list, selected) -> tuple[Optional[str], Optional[str], Optional[float]]:
    """The pool where the coin STARTED trading: its earliest-created pool (the pump.fun curve when
    DexScreener lists it), else the selected pool. (address, dex, created_at)"""
    best = None
    for p in list(pairs or []) + ([selected] if selected is not None else []):
        if p is None or not getattr(p, "pair_address", None) or not getattr(p, "pair_created_at", None):
            continue
        if best is None or p.pair_created_at < best.pair_created_at:
            best = p
    if best is None:
        return (getattr(selected, "pair_address", None), getattr(selected, "dex_id", None), None)
    return best.pair_address, best.dex_id, best.pair_created_at
