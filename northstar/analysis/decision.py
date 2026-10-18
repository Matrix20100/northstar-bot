"""Gate aggregation and publication policy.

Order of precedence (strict):
  1. Any FAILED gate                       -> REJECT   (a high score never overrides this)
  2. Any critical gate UNKNOWN             -> INSUFFICIENT_DATA
  3. Risk CRITICAL/ELEVATED                -> REJECT
  4. Score + all publication conditions    -> AUTO_POST
  5. Score >= watchlist threshold          -> WATCHLIST
  6. Otherwise                             -> REJECT (weak opportunity)
Thresholds never move on their own, regardless of how long it has been since the last alert.
"""
from __future__ import annotations

from typing import Optional

from dataclasses import dataclass, field

from ..config import Settings
from ..models import Action, Confidence, GateResult, GateStatus, RiskLevel


@dataclass
class Decision:
    action: Action
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"action": self.action.value, "reasons": self.reasons, "blockers": self.blockers}


def decide(score: float, risk: RiskLevel, confidence: Confidence, gates: list[GateResult],
           narrative_points: float, narrative_evidence: int, extended: bool, s: Settings,
           cooldown_active: bool = False, daily_cap_reached: bool = False,
           market_cap: Optional[float] = None, volume_acceleration: Optional[float] = None,
           buy_sell_ratio: Optional[float] = None) -> Decision:
    failed = [g for g in gates if g.status == GateStatus.FAIL]
    if failed:
        return Decision(Action.REJECT, [f"gate failed: {g.name} — {g.reason}" for g in failed])
    unknown = [g for g in gates if g.status == GateStatus.UNKNOWN and g.critical]
    if unknown:
        return Decision(Action.INSUFFICIENT_DATA,
                        [f"critical check unresolved: {g.name} — {g.reason}" for g in unknown])
    if risk in (RiskLevel.CRITICAL, RiskLevel.ELEVATED):
        return Decision(Action.REJECT, [f"risk assessment {risk.value}"])

    blockers = []
    if score < s.min_opportunity_score:
        blockers.append(f"score {score:.1f} < {s.min_opportunity_score:.0f}")
    allowed = {"LOW_DETECTED": {RiskLevel.LOW_DETECTED},
               "MODERATE": {RiskLevel.LOW_DETECTED, RiskLevel.MODERATE}}[s.max_allowed_risk]
    if risk not in allowed:
        blockers.append(f"risk {risk.value} above allowed {s.max_allowed_risk}")
    if confidence.rank < Confidence(s.min_confidence).rank:
        blockers.append(f"confidence {confidence.value} below {s.min_confidence}")
    if narrative_points < s.min_narrative_score * s.w_narrative / 25:
        blockers.append(f"narrative score {narrative_points:.1f} below minimum")
    if narrative_evidence < s.min_narrative_evidence_items:
        blockers.append(f"only {narrative_evidence} independent narrative evidence item(s)")
    if extended and getattr(s, "block_extended_moves", True):
        blockers.append("price already extended — wait for consolidation")
    min_mc = getattr(s, "min_callout_market_cap", 0) or 0
    if min_mc > 0 and (market_cap is None or market_cap < min_mc):
        blockers.append(f"market cap {'UNKNOWN' if market_cap is None else f'${market_cap:,.0f}'} "
                        f"below ${min_mc:,.0f} callout minimum")
    # Momentum: fading coins (volume dropping, sellers matching buyers) were never winners in the data.
    min_acc = getattr(s, "min_volume_acceleration", 0) or 0
    if min_acc > 0 and (volume_acceleration is None or volume_acceleration < min_acc):
        blockers.append("volume fading: 1h volume is "
                        + ("UNKNOWN" if volume_acceleration is None else f"{volume_acceleration:.2f}x")
                        + f" its 6h hourly average (need {min_acc:g}x)")
    min_bs = getattr(s, "min_buy_sell_ratio_h1", 0) or 0
    if min_bs > 0 and (buy_sell_ratio is None or buy_sell_ratio < min_bs):
        blockers.append("weak buying: 1h buy/sell ratio "
                        + ("UNKNOWN" if buy_sell_ratio is None else f"{buy_sell_ratio:.2f}")
                        + f" (need {min_bs:g})")
    if cooldown_active:
        blockers.append("alert cooldown active for this mint")
    if daily_cap_reached:
        blockers.append("daily alert cap reached")

    if not blockers:
        return Decision(Action.AUTO_POST, ["all gates passed and publication policy satisfied"])
    if score >= s.watchlist_min_score and not cooldown_active:
        return Decision(Action.WATCHLIST, ["promising but not yet qualified"], blockers)
    if cooldown_active:
        return Decision(Action.WATCHLIST, ["already alerted within cooldown"], blockers)
    return Decision(Action.REJECT, ["opportunity too weak"], blockers)
