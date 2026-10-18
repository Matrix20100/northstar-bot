"""Rule-based entry / exit plan for callouts and early watches.

Every call gets the same written plan, and the monitors post a NEW message (so people get
pinged) when one of its rules triggers. Multiples are market-cap multiples since the call.

The defaults come from Northstar's own tracked decisions (2026-10-18): coins that moved did so
within ~15 minutes and most were near zero within the hour. So the plan takes profit early,
uses a hard stop, and exits anything that has not worked within the time limit.

These are mechanical rules on delayed observations (one check every refresh interval), not
predictions. A real fill can be worse than the posted level.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Plan:
    entry_mcap: float
    max_entry_mcap: float
    tp1: float
    tp1_sell_pct: int
    tp2: float
    tp2_sell_pct: int
    stop: float
    trail_pct: float
    time_stop_min: float
    time_stop_multiple: float


PROFILES = {"standard": "plan_", "scalp": "scalp_"}


def plan_values(s, profile: str = "standard") -> dict:
    """The plan levels for a profile, frozen into each call (later tuning never changes a live call)."""
    pre = PROFILES.get(profile, "plan_")
    keys = ("max_entry_pct", "tp1", "tp1_sell_pct", "tp2", "tp2_sell_pct", "stop", "trail_pct",
            "time_stop_minutes", "time_stop_multiple")
    return {k: getattr(s, pre + k) for k in keys}


def build_plan(entry_mcap: Optional[float], s=None, profile: str = "standard",
               values: Optional[dict] = None) -> Optional[Plan]:
    if not isinstance(entry_mcap, (int, float)) or entry_mcap <= 0:
        return None
    v = values or plan_values(s, profile)
    return Plan(entry_mcap, entry_mcap * (1 + v["max_entry_pct"] / 100), v["tp1"], int(v["tp1_sell_pct"]),
                v["tp2"], int(v["tp2_sell_pct"]), v["stop"], v["trail_pct"], v["time_stop_minutes"],
                v["time_stop_multiple"])


def valid_values(v: dict) -> bool:
    """A plan that makes sense: stop below entry, entry zone below TP1, TP1 not above TP2."""
    try:
        return (0 < v["stop"] < 1 < 1 + v["max_entry_pct"] / 100 < v["tp1"] <= v["tp2"]
                and 0 < v["tp1_sell_pct"] <= 100 and 0 <= v["tp2_sell_pct"] <= 100
                and v["trail_pct"] > 0 and v["time_stop_minutes"] > 0 and v["time_stop_multiple"] > 1)
    except (KeyError, TypeError):
        return False


def sells_everything(p: Plan) -> bool:
    return p.tp1_sell_pct + p.tp2_sell_pct >= 100


def plan_lines(p: Plan, fmt) -> list[str]:
    """Human text. ``fmt`` turns a market cap into display text ($ or SOL)."""
    rest = ("— that's everything, trade done" if sells_everything(p)
            else f"trail the rest (exit if it drops {p.trail_pct:g}% from its high)")
    return [
        f"🟢 **Entry:** up to {fmt(p.max_entry_mcap)} MC — don't chase above that",
        f"🎯 **TP1:** {p.tp1:g}x ({fmt(p.entry_mcap * p.tp1)}) → sell {p.tp1_sell_pct}%",
        f"🎯 **TP2:** {p.tp2:g}x ({fmt(p.entry_mcap * p.tp2)}) → sell "
        f"{min(p.tp2_sell_pct, 100 - p.tp1_sell_pct)}% {rest}",
        f"🛑 **Stop:** {fmt(p.entry_mcap * p.stop)} ({(p.stop - 1) * 100:+.0f}%)",
        f"⏱️ **Time stop:** below {p.time_stop_multiple:g}x after {p.time_stop_min:g} min → exit",
    ]


# event kind -> (title, colour key)
EVENTS = {
    "ENTRY_CLOSED": "⛔ ENTRY CLOSED",
    "TP1": "🎯 TAKE PROFIT 1",
    "TP2": "🎯 TAKE PROFIT 2",
    "TRAIL": "🚪 EXIT — TRAILING STOP",
    "STOP": "🛑 EXIT — STOP LOSS",
    "TIME": "⏱️ EXIT — TIME STOP",
    "DANGER": "🚨 EXIT NOW",
}
EXIT_KINDS = {"TRAIL", "STOP", "TIME", "DANGER"}


def evaluate(p: Plan, multiple: Optional[float], peak: float, age_seconds: float, sent: set,
             fmt) -> list[tuple[str, str]]:
    """New plan events for the current observation, in the order they should be posted.

    ``sent`` holds kinds already posted. Once any EXIT kind has been posted, nothing further is
    posted for this call (the plan is finished).
    """
    if multiple is None or multiple <= 0 or sent & EXIT_KINDS:
        return []
    if sells_everything(p) and "TP2" in sent:
        return []                                   # everything already sold: the plan is complete
    out: list[tuple[str, str]] = []
    mc = p.entry_mcap * multiple
    tp1_done = "TP1" in sent or multiple >= p.tp1
    if "ENTRY_CLOSED" not in sent and not tp1_done and mc > p.max_entry_mcap:
        out.append(("ENTRY_CLOSED", f"Now {fmt(mc)} — above the {fmt(p.max_entry_mcap)} max entry. "
                                    "Don't buy here; holders follow the plan below."))
    if "TP1" not in sent and multiple >= p.tp1:
        out.append(("TP1", f"Hit {multiple:.2f}x ({fmt(mc)}). Sell {p.tp1_sell_pct}% now and move your stop "
                           "to break-even on the rest."))
    if "TP2" not in sent and multiple >= p.tp2:
        if sells_everything(p):
            out.append(("TP2", f"Hit {multiple:.2f}x ({fmt(mc)}). Sell the rest — trade done."))
            return out
        out.append(("TP2", f"Hit {multiple:.2f}x ({fmt(mc)}). Sell another {p.tp2_sell_pct}%; trail the rest "
                           f"({p.trail_pct:g}% below the high)."))
    exit_event = None
    if tp1_done and peak >= p.tp1 and multiple <= max(1.0, peak * (1 - p.trail_pct / 100)):
        exit_event = ("TRAIL", f"Fell from a {peak:.2f}x high to {multiple:.2f}x ({fmt(mc)}). Exit the rest "
                               "and keep the profit.")
    elif multiple <= p.stop:
        exit_event = ("STOP", f"Down to {multiple:.2f}x ({fmt(mc)}), below the {(p.stop - 1) * 100:+.0f}% stop. Exit.")
    elif (age_seconds >= p.time_stop_min * 60 and multiple < p.time_stop_multiple and not tp1_done):
        # judged on where it IS, not on a past wick: a coin that touched 1.35x and now sits at 0.8x
        # must still be told to exit
        exit_event = ("TIME", f"{age_seconds / 60:.0f} min in and it isn't holding {p.time_stop_multiple:g}x "
                              f"(now {multiple:.2f}x, {fmt(mc)}; high {peak:.2f}x). Exit — "
                              "calls that stall like this usually fade.")
    if exit_event:
        out.append(exit_event)
    return out


def danger_event(reason: str) -> tuple[str, str]:
    return ("DANGER", f"{reason} Sell now — don't wait for the stop.")
