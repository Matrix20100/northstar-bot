"""The exit engine (v5.0.0): when to sell, decided live from the coin's chart.

Operator: "almost every coin dies, so the bot needs to show when it would've exited ... we should sell
when we are down, we should check every chart and analyse it live so it exits on the best condition or
if it believes it's still going to rise ... we need to stop taking big hits, we should minimise losses
while still making educated choices."

Every open position (paper or real) is checked every `exit_poll_seconds` against a fresh read of its
pool: market cap, 5-minute buys/sells, 5-minute and 1-hour volume, 5-minute price change. `decide()`
returns what to sell and why. Rules, in order ("protect capital"):

  1. STOP         down `exit_stop_pct` (20%) from the entry: sell everything. No hoping.
  2. TRAIL        once the position has been up `exit_trail_start_x` (1.3x), the stop moves up to break
                  even, then follows the high: it gives back at most `exit_trail_pct` (25%) of the high
                  below 2x, 20% from 2x, 15% from 5x. The stop only ever moves up.
  3. SELLERS      sellers took over: in the last 5 minutes at least `exit_sell_pressure` (1.5) sells per
                  buy and the price fell `exit_dump_m5_pct` (10%)+, while the position is under its high.
                  Sell everything before the dump finishes.
  4. FADING       the move is over: 5-minute volume under `exit_fade_ratio` (25%) of the hour's pace, the
                  price 15%+ under its high and the position at least 10 minutes old.
  5. NO_PROGRESS  never got going: after `exit_no_progress_minutes` (8) it never reached 1.15x and is
                  below 0.92x. Dead money is sold early, before it becomes a stop.
  6. TIME         held `exit_max_hold_minutes` (180): curve coins rarely survive the day.
  7. TP1 / TP2    take profit in steps: sell `exit_tp1_sell_pct` (35%) of the position at `exit_tp1_x`
                  (1.5x) and another 35% at `exit_tp2_x` (2.5x). The rest rides the trailing stop.

HOLD: while the coin is making a new high with at least as many buys as sells, rules 3-5 wait: it is
still going up. The stop and the trail always apply.

v5.1.0 CONVICTION (conviction.py): a position graded at the buy carries its own stop (`Position.stop_pct`, 15-35%
instead of the flat 20%). A strong setup (`Position.grace`) that falls under its stop while buyers are still in
control - 5-min buys >= sells, no fast 5-min dump, liquidity at least 60% of what it was at the buy - waits up to
`exit_grace_seconds` for a bounce ("dip-hold"). Never below `exit_hard_stop_pct`, and never once the coin had run
past the trailing-stop start (then the trailing stop is in charge). "Never got going" waits longer for strong setups.

Every rule is a setting (dashboard Settings, "Exits"). Nothing here buys, signs or sends anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

ALL = 1.0          # sell everything left

REASONS = {
    "STOP": "stop loss", "TRAIL": "trailing stop", "SELLERS": "sellers took over", "FADING": "volume faded",
    "NO_PROGRESS": "never got going", "TIME": "held too long", "TP1": "take profit 1", "TP2": "take profit 2",
    "DEAD": "coin died", "KILL": "kill switch", "END": "tracking ended", "HARD_STOP": "hard stop",
}
GRACE_MIN_LIQ_SHARE = 0.6        # a dip-hold needs at least this share of the liquidity seen at the buy


@dataclass
class Tick:
    """One fresh read of a coin's pool."""
    mcap: float
    at: float
    buys_m5: Optional[int] = None
    sells_m5: Optional[int] = None
    vol_m5: Optional[float] = None
    vol_h1: Optional[float] = None
    chg_m5: Optional[float] = None
    liquidity: Optional[float] = None

    @classmethod
    def from_pair(cls, pair, now: float) -> Optional["Tick"]:
        mc = pair.market_cap or pair.fdv
        if not mc or mc <= 0:
            return None
        t5 = (pair.txns or {}).get("m5") or {}
        return cls(mcap=float(mc), at=now, buys_m5=t5.get("buys"), sells_m5=t5.get("sells"),
                   vol_m5=(pair.vol or {}).get("m5"), vol_h1=(pair.vol or {}).get("h1"),
                   chg_m5=(pair.price_change or {}).get("m5"), liquidity=pair.liquidity_usd)


@dataclass
class Position:
    entry_mcap: float
    opened_at: float
    peak_mcap: float                 # highest market cap seen since the buy (before this tick)
    done: set = field(default_factory=set)
    stop_x: Optional[float] = None   # the trailing stop reached so far (only moves up)
    stop_pct: Optional[float] = None # v5.1.0: this position's own stop from its conviction (None = exit_stop_pct)
    grace: bool = False              # v5.1.0: may hold through a short dip under the stop
    grace_since: Optional[float] = None   # when the current dip-hold started
    conviction: Optional[float] = None    # 0-100
    entry_liq: Optional[float] = None     # pool liquidity at the buy
    note: Optional[str] = None            # what the engine is doing right now (e.g. holding through a dip)


@dataclass
class Action:
    kind: str
    fraction: float                  # share of the ORIGINAL position to sell (ALL = everything left)
    reason: str


def _g(s, key, default):
    try:
        v = getattr(s, key, default)
        return default if v is None else float(v)
    except (TypeError, ValueError):
        return default


def trail_line(peak_x: float, s=None) -> Optional[float]:
    """The trailing stop (as a multiple of the entry) for a position that peaked at `peak_x`."""
    start = _g(s, "exit_trail_start_x", 1.3)
    if peak_x < start:
        return None
    base = _g(s, "exit_trail_pct", 25.0) / 100
    give = base if peak_x < 2 else (min(base, 0.20) if peak_x < 5 else min(base, 0.15))
    floor = 1.0 + _g(s, "exit_breakeven_buffer_pct", 2.0) / 100     # break even after fees
    return max(floor, peak_x * (1 - give))


def _dip_hold(pos: Position, tick: Tick, peak_x: float, s=None) -> tuple[bool, str]:
    """May a strong setup wait under its stop? (ok, why)."""
    if not pos.grace:
        return False, ""
    if peak_x >= _g(s, "exit_trail_start_x", 1.3):
        return False, "it already ran, the trailing stop is in charge"
    b, sl = tick.buys_m5, tick.sells_m5
    if b is None or sl is None:
        return False, "5-min trades unknown"
    if sl > b:
        return False, f"sellers in control ({sl} sells vs {b} buys)"
    if tick.chg_m5 is not None and tick.chg_m5 <= -2 * _g(s, "exit_dump_m5_pct", 10.0):
        return False, f"fast dump ({tick.chg_m5:+.0f}% in 5 min)"
    if pos.entry_liq and tick.liquidity is not None and tick.liquidity < GRACE_MIN_LIQ_SHARE * pos.entry_liq:
        return False, f"liquidity down to {tick.liquidity / pos.entry_liq:.0%} of the buy"
    return True, f"{b} buys vs {sl} sells"


def decide(pos: Position, tick: Tick, s=None) -> list[Action]:
    """What to sell now. Updates pos.peak_mcap and pos.stop_x."""
    entry = pos.entry_mcap
    if not entry or entry <= 0 or not tick or tick.mcap <= 0:
        return []
    x = tick.mcap / entry
    new_high = tick.mcap > pos.peak_mcap
    pos.peak_mcap = max(pos.peak_mcap or entry, tick.mcap)
    peak_x = pos.peak_mcap / entry
    minutes = (tick.at - pos.opened_at) / 60.0

    pos.note = None
    # 1. stop (its own, from the conviction grade) and the hard stop under it
    stop_pct = pos.stop_pct if pos.stop_pct else _g(s, "exit_stop_pct", 20.0)
    stop = 1 - stop_pct / 100
    hard = 1 - max(stop_pct, _g(s, "exit_hard_stop_pct", 45.0)) / 100
    if x <= hard:
        return [Action("HARD_STOP" if pos.stop_pct else "STOP", ALL,
                       f"down {100 * (1 - x):.0f}% from the entry (hard stop at -{100 * (1 - hard):.0f}%)")]
    if x <= stop:
        ok, why = _dip_hold(pos, tick, peak_x, s)
        if ok:
            pos.grace_since = pos.grace_since or tick.at
            waited = tick.at - pos.grace_since
            limit = _g(s, "exit_grace_seconds", 90.0)
            if waited < limit:
                pos.note = (f"holding through a dip: {100 * (1 - x):.0f}% down, {why}; "
                            f"{limit - waited:.0f}s left before the stop")
                return []
            return [Action("STOP", ALL, f"down {100 * (1 - x):.0f}% and no bounce in {waited:.0f}s "
                                        f"(stop at -{stop_pct:.0f}%)")]
        extra = f"; no dip-hold: {why}" if pos.grace and why else ""
        return [Action("STOP", ALL, f"down {100 * (1 - x):.0f}% from the entry (stop at -{stop_pct:.0f}%){extra}")]
    pos.grace_since = None
    # 2. trailing stop (ratchets up, never down)
    line = trail_line(peak_x, s)
    if line is not None:
        pos.stop_x = max(pos.stop_x or 0.0, line)
    if pos.stop_x is not None and x <= pos.stop_x:
        return [Action("TRAIL", ALL, f"fell to {x:.2f}x from a {peak_x:.2f}x high (stop {pos.stop_x:.2f}x)")]

    buys, sells = tick.buys_m5, tick.sells_m5
    rising = new_high and (buys is None or sells is None or buys >= sells)
    if not rising:
        # 3. sellers took over
        ratio = _g(s, "exit_sell_pressure", 1.5)
        dump = _g(s, "exit_dump_m5_pct", 10.0)
        if (buys is not None and sells is not None and sells >= ratio * max(1, buys)
                and tick.chg_m5 is not None and tick.chg_m5 <= -dump and x < peak_x * 0.97):
            return [Action("SELLERS", ALL, f"{sells} sells vs {buys} buys in 5 min, price {tick.chg_m5:+.0f}%")]
        # 4. volume faded
        fade = _g(s, "exit_fade_ratio", 0.25)
        if (minutes >= 10 and tick.vol_m5 is not None and tick.vol_h1 and tick.vol_h1 > 0
                and tick.vol_m5 < fade * tick.vol_h1 / 12 and x <= peak_x * 0.85):
            return [Action("FADING", ALL, f"5-min volume ${tick.vol_m5:,.0f} is under {fade:.0%} of the hour's pace, "
                                          f"{100 * (1 - x / peak_x):.0f}% under the high")]
        # 5. never got going
        np_minutes = _g(s, "exit_no_progress_minutes", 8.0) * (1 + max(0.0, min(1.0, ((pos.conviction or 50) - 50) / 30)))
        if minutes >= np_minutes and peak_x < 1.15 and x < 0.92:
            return [Action("NO_PROGRESS", ALL, f"{minutes:.0f} min in, never above {peak_x:.2f}x, now {x:.2f}x")]
    # 6. time
    hold = _g(s, "exit_max_hold_minutes", 180.0)
    if hold and minutes >= hold:
        return [Action("TIME", ALL, f"held {minutes:.0f} min")]
    # 7. take profit in steps
    out = []
    for kind, xkey, pkey, xd, pd in (("TP1", "exit_tp1_x", "exit_tp1_sell_pct", 1.5, 35.0),
                                      ("TP2", "exit_tp2_x", "exit_tp2_sell_pct", 2.5, 35.0)):
        tx = _g(s, xkey, xd)
        if kind not in pos.done and tx and x >= tx:
            out.append(Action(kind, _g(s, pkey, pd) / 100, f"reached {x:.2f}x (take profit at {tx:g}x)"))
    return out


def apply_conviction(pos: Position, conv) -> Position:
    """Put a conviction grade (conviction.Conviction or its dict) on a position."""
    if conv is None:
        return pos
    d = conv if isinstance(conv, dict) else conv.to_dict()
    pos.stop_pct = d.get("stop_pct") or pos.stop_pct
    pos.grace = bool(d.get("grace"))
    pos.conviction = d.get("score")
    return pos


def replay(entry_mcap: float, opened_at: float, ticks: list[Tick], s=None, conv=None) -> dict:
    """Run the engine over a recorded price path: where would it have sold? Used for calls the bot
    only tracked (the Calls tab's "bot exit"). Returns {"x" (average sell multiple), "exit", "reason", "at"}."""
    pos = apply_conviction(Position(entry_mcap=entry_mcap, opened_at=opened_at, peak_mcap=entry_mcap), conv)
    left, got, last = 1.0, 0.0, None
    for t in ticks:
        for a in decide(pos, t, s):
            part = left if a.fraction >= ALL else min(left, a.fraction)
            got += part * t.mcap / entry_mcap
            left -= part
            pos.done.add(a.kind)
            last = (a.kind, a.reason, t.at)
            if left <= 1e-9:
                return {"x": round(got, 4), "exit": a.kind, "reason": a.reason, "at": t.at, "open": False}
    if ticks:
        got_open = got + left * ticks[-1].mcap / entry_mcap
        return {"x": round(got_open, 4), "exit": last[0] if last else None, "reason": last[1] if last else None,
                "at": last[2] if last else None, "open": True}
    return {"x": None, "exit": None, "reason": None, "at": None, "open": True}
