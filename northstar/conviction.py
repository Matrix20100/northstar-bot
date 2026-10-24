"""Conviction (v5.1.0): how strong a position's setup was when it was bought, and what that means for its exit.

Operator: "the bot was down on coins and sold, now it ran to 2x. A bot should have a risk factor by analysing
the coin and likelihood of succeeding in the trade, we need to take educated risks."

Every position (paper or real) is graded once, from the research report of the coin at the moment it was bought.
The grade is a conviction score 0-100 built ONLY from evidence the bot measured; a check that was not measured adds
nothing (unknown is never treated as good). Inputs:

  * the rug model's odds (trusted model)          low odds +, high odds -
  * the risk system's level                        LOW +, HIGH/CRITICAL -
  * the call's score (runner score or unified)     high +, low -
  * the launch: bundled / sniped / organic         bundled or sniped -, organic +
  * launch snipers still holding                   a lot -
  * tracked (smart) wallets holding                +
  * buyers rising or falling, 5-min buy share      +/-
  * failed risk gates                              - each
  * the runner brain's odds, ONLY when the brain is trusted (it has earned a say)

What the grade changes (exits.py applies it):

  1. The stop: from `exit_stop_min_pct` (15%) for the weakest setups to `exit_stop_max_pct` (35%) for the
     strongest. A strong coin gets room for the normal shakeout; a weak one is cut fast.
  2. The dip-hold: a setup with conviction >= `exit_grace_min_conviction` (60) that falls under its stop, while
     buyers are still in control (5-min buys >= sells, no fast dump, liquidity still there), waits up to
     `exit_grace_seconds` (90 s) for a bounce before selling. It never waits below `exit_hard_stop_pct` (45%),
     and never after the coin had already run (the trailing stop is in charge then).
  3. The size: weaker setups get a smaller position, and a wider stop gets a smaller position, so the money lost
     at the stop is never more than a plain `exit_stop_pct` (20%) stop on a full position. Never bigger.
  4. "Never got going" waits longer for strong setups.

Nothing here buys, signs or sends anything, and nothing claims a win rate: whether graded exits do better is
measured on the Paper tab (results by grade), from the bot's own trades.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Optional

GRADES = (("A", 70.0), ("B", 50.0), ("C", 0.0))


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _g(s, key, default):
    v = _num(getattr(s, key, default) if s is not None else default)
    return default if v is None else v


@dataclass
class Conviction:
    score: float                       # 0-100
    grade: str                         # A / B / C
    stop_pct: float                    # this position's stop (% under the entry)
    size_mult: float                   # share of the normal position size (<= 1)
    grace: bool                        # may hold through a short dip under the stop
    known: int                         # how many inputs were measured
    why: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def label(self) -> str:
        return (f"conviction {self.score:.0f} ({self.grade}) · stop -{self.stop_pct:.0f}%"
                + (" · holds short dips" if self.grace else "")
                + (f" · size {self.size_mult:.0%}" if self.size_mult < 0.995 else ""))


def grade_of(score: float) -> str:
    for g, floor in GRADES:
        if score >= floor:
            return g
    return "C"


def assess(report: Optional[dict], s=None) -> Conviction:
    """Grade a setup from its research report."""
    r = report if isinstance(report, dict) else {}
    pts = 50.0
    why: list[str] = []
    known = 0

    def add(n: float, text: str) -> None:
        nonlocal pts
        pts += n
        why.append(f"{'+' if n >= 0 else '−'}{abs(n):.0f} {text}")

    u = r.get("unified") if isinstance(r.get("unified"), dict) else {}
    rug = _num(u.get("rug_odds"))
    if rug is None:
        rug = _num((r.get("rug_model") or {}).get("p")) if isinstance(r.get("rug_model"), dict) else None
    if rug is not None:
        known += 1
        if rug < 0.15:
            add(15, f"rug odds {rug:.0%}")
        elif rug < 0.30:
            add(5, f"rug odds {rug:.0%}")
        elif rug >= 0.45:
            add(-20, f"rug odds {rug:.0%}")
        else:
            add(-8, f"rug odds {rug:.0%}")

    risk = r.get("risk")
    if risk in ("LOW_DETECTED", "MODERATE", "ELEVATED", "CRITICAL"):
        known += 1
        n = {"LOW_DETECTED": 10, "MODERATE": 0, "ELEVATED": -15, "CRITICAL": -30}[risk]
        if n:
            add(n, f"risk {'LOW' if risk == 'LOW_DETECTED' else 'HIGH' if risk == 'ELEVATED' else risk}")

    runner = r.get("runner") if isinstance(r.get("runner"), dict) else {}
    score = _num(runner.get("score"))
    if score is None:
        score = _num((r.get("score") or {}).get("total")) if isinstance(r.get("score"), dict) else None
    if score is not None:
        known += 1
        if score >= 80:
            add(10, f"score {score:.0f}")
        elif score >= 65:
            add(5, f"score {score:.0f}")
        elif score < 50:
            add(-10, f"score {score:.0f}")

    lb = r.get("launch_buys") if isinstance(r.get("launch_buys"), dict) else {}
    if lb.get("status") in ("OBSERVED", "INCOMPLETE"):
        known += 1
        bundle, early = _num(lb.get("bundle_pct")), _num(lb.get("early_buy_pct"))
        if bundle is not None and bundle >= 20:
            add(-12, f"{bundle:.0f}% bundled at launch")
        elif bundle is not None and bundle >= 10:
            add(-5, f"{bundle:.0f}% bundled at launch")
        if early is not None and early >= 35:
            add(-8, f"{early:.0f}% sniped at launch")
        if lb.get("verdict") == "ORGANIC":
            add(8, "organic launch")
        held = _num(lb.get("sniper_held_pct"))
        if held is not None and held >= 10:
            add(-8, f"launch snipers still hold {held:.0f}%")

    tw = r.get("tracked_wallets") if isinstance(r.get("tracked_wallets"), dict) else {}
    if tw.get("status") == "OBSERVED":
        known += 1
        n = int(_num(tw.get("holding_count")) or 0)
        if n >= 2:
            add(8, f"{n} tracked wallets hold it")
        elif n == 1:
            add(4, "a tracked wallet holds it")

    tr = r.get("trades") if isinstance(r.get("trades"), dict) else {}
    trend = str(tr.get("buyer_trend") or "").lower()
    if trend in ("rising", "declining"):
        known += 1
        add(5 if trend == "rising" else -5, f"buyers {trend}")
    t5 = ((r.get("market") or {}).get("txns") or {}).get("m5") if isinstance(r.get("market"), dict) else None
    if isinstance(t5, dict):
        b, sl = _num(t5.get("buys")) or 0, _num(t5.get("sells")) or 0
        if b + sl >= 10:
            known += 1
            share = b / (b + sl)
            if share >= 0.6:
                add(5, f"{share:.0%} of 5-min trades are buys")
            elif share <= 0.4:
                add(-5, f"only {share:.0%} of 5-min trades are buys")

    gates = [g for g in r.get("gates") or [] if isinstance(g, dict)]
    failed = [g for g in gates if g.get("status") == "FAIL"]
    if gates:
        known += 1
        if failed:
            add(-min(16, 4 * len(failed)), f"{len(failed)} risk check(s) failed")

    brain = r.get("brain") if isinstance(r.get("brain"), dict) else {}
    p, base = _num(brain.get("p")), _num(brain.get("base"))
    if brain.get("trusted") and p is not None and base:
        known += 1
        if p >= 2 * base:
            add(10, f"brain odds {p:.0%} (normal {base:.0%})")
        elif p < 0.7 * base:
            add(-10, f"brain odds {p:.0%} (normal {base:.0%})")

    if known < 3:
        # too little measured: no room for a dip and a normal-to-tight stop. Unknown is not good.
        pts = min(pts, 45.0)
        why.append(f"only {known} input(s) measured")
    pts = max(0.0, min(100.0, pts))

    lo, hi = _g(s, "exit_stop_min_pct", 15.0), _g(s, "exit_stop_max_pct", 35.0)
    lo, hi = min(lo, hi), max(lo, hi)
    c = max(0.0, min(1.0, (pts - 30.0) / 50.0))        # 30 or less -> tightest, 80 or more -> widest
    stop = round(lo + (hi - lo) * c, 1)
    hard = _g(s, "exit_hard_stop_pct", 45.0)
    stop = min(stop, max(1.0, hard - 1.0))
    base_stop = _g(s, "exit_stop_pct", 20.0) or stop
    mult = 1.0
    if getattr(s, "exit_size_by_conviction", True) if s is not None else True:
        mult = (0.5 + 0.5 * c) * min(1.0, base_stop / stop if stop else 1.0)
        mult = round(max(0.25, min(1.0, mult)), 3)
    grace = pts >= _g(s, "exit_grace_min_conviction", 60.0) and _g(s, "exit_grace_seconds", 90.0) > 0
    return Conviction(score=round(pts, 1), grade=grade_of(pts), stop_pct=stop, size_mult=mult, grace=grace,
                      known=known, why=why[:8])


def enabled(s) -> bool:
    return bool(getattr(s, "exit_conviction_enabled", True)) if s is not None else True


def report_for(db, ref: str, mint: str) -> Optional[dict]:
    """The research report a call was made from: an early/runner call keeps its own; a standard call has the
    snapshot it was posted from (else the coin's latest research)."""
    try:
        if ref.startswith("early:"):
            row = db.fetchone("SELECT report FROM early_calls WHERE mint = ?", (mint,))
            if row and row["report"]:
                return json.loads(row["report"])
        if ref.startswith("alert:"):
            a = db.fetchone("SELECT snapshot_id FROM alerts WHERE id = ?", (int(ref.split(":", 1)[1]),))
            if a and a["snapshot_id"]:
                row = db.fetchone("SELECT report FROM research_snapshots WHERE id = ?", (a["snapshot_id"],))
                if row and row["report"]:
                    return json.loads(row["report"])
        row = db.fetchone("SELECT report FROM research_snapshots WHERE mint = ? ORDER BY ts DESC LIMIT 1", (mint,))
        if row and row["report"]:
            return json.loads(row["report"])
    except Exception:  # noqa: BLE001 - no report: graded as unknown
        return None
    return None


def for_call(db, ref: str, mint: str, s=None) -> Conviction:
    return assess(report_for(db, ref, mint), s)


def entry_liquidity(report: Optional[dict]) -> Optional[float]:
    m = (report or {}).get("market") if isinstance(report, dict) else None
    return _num((m or {}).get("liquidity_usd")) if isinstance(m, dict) else None
