"""Which coins Northstar keeps following after a decision (v2.5.5).

Operator rule (22 Oct): a coin that fails the checks is not followed any more, unless it has good
numbers, a good narrative and no high holder concentration. Before v2.5.5 every rejected coin
was re-researched every hour for as long as its market data changed (the 21 Oct backup: 3,494
rejected coins, 21,401 research runs), and every decision was price-tracked for 24 h.

  second_chance(report, s)    may a REJECTED coin be researched again later?
  track_outcome(mint, action, report, s)
                              should this decision be price-tracked for 24 h (learning)? Every
                              non-rejection, every second-chance rejection, and a fixed random
                              sample of the rest (operator choice: 10%), so the bot can still
                              measure whether it rejects good coins.

A proven trap, a prefilter rejection or a rejection without a full analysis never gets a second
chance: there is nothing to judge "good numbers" on.
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

CONCENTRATION_HAZARDS = {"haz:concentration", "haz:whale", "haz:cluster", "haz:dev_holdings"}
NO_SECOND_CHANCE_CLASSES = {"SAFETY_FAIL", "PREFILTER", "INVALID"}


def _num(x: Any) -> Optional[float]:
    if isinstance(x, dict):
        x = x.get("value")
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def second_chance(report: dict, s: Any) -> tuple[bool, str]:
    """(allowed, why) for re-researching a rejected coin. All three must hold:
    good numbers, good narrative, no high holder concentration."""
    final = report.get("final") if isinstance(report.get("final"), dict) else {}
    u = report.get("unified") if isinstance(report.get("unified"), dict) else None
    if final.get("reason_class") in NO_SECOND_CHANCE_CLASSES:
        return False, f"{final.get('reason_class')}: not followed"
    if not u or not u.get("decision"):
        return False, "no full analysis: not followed"
    vetoes = [v for v in u.get("vetoes") or [] if isinstance(v, dict)]
    if any(v.get("severity") == "CONFIRMED_CRITICAL" for v in vetoes):
        return False, "proven safety failure: not followed"

    # good numbers: a watchable score, enough liquidity, not dumping
    score = _num(u.get("score"))
    watch_min = _num(getattr(s, "u_watch_min", None)) or 48.0
    liq = _num((report.get("market") or {}).get("liquidity_usd"))
    floor = _num(getattr(s, "min_liquidity_usd", None)) or 0.0
    if score is None or score < watch_min:
        return False, f"score {score if score is not None else 0:.0f} below the {watch_min:.0f} watch line"
    if liq is None or liq < floor:
        return False, "liquidity below the minimum"
    if (report.get("market_structure") or {}).get("collapsed"):
        return False, "price collapsing"

    # good narrative: real evidence, and the narrative category scored well
    nar = report.get("narrative") or {}
    items = _num(nar.get("evidence_items")) or 0.0
    need = max(1, int(getattr(s, "min_narrative_evidence_items", 1) or 1))
    quality = _num(((u.get("categories") or {}).get("narrative") or {}).get("quality"))
    if items < need or quality is None or quality < 0.6:
        return False, "narrative too weak"

    # holders: the concentration check ran and passed, and no concentration hazard was found
    gate = next((g for g in report.get("gates") or [] if isinstance(g, dict)
                 and g.get("name") == "holder_concentration"), None)
    if not gate or gate.get("status") != "PASS":
        return False, "holder concentration not shown to be safe"
    if any(v.get("key") in CONCENTRATION_HAZARDS for v in vetoes):
        return False, "concentrated holders or wallet clusters"
    return True, "good numbers, good narrative, holders spread: re-checked later"


def sampled(mint: str, pct: float) -> bool:
    """A fixed pseudo-random sample by mint: the same coin is always in or always out."""
    if pct >= 100:
        return True
    if pct <= 0:
        return False
    h = int(hashlib.sha1(mint.encode()).hexdigest()[:8], 16)
    return (h % 10000) < pct * 100


def track_outcome(mint: str, action: str, report: dict, s: Any) -> bool:
    """Price-track this decision for 24 h? Rejections: second chance, or the learning sample."""
    if action != "REJECT":
        return True
    if second_chance(report, s)[0]:
        return True
    return sampled(mint, float(getattr(s, "reject_tracking_sample_pct", 10.0)))
