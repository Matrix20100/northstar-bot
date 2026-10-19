"""The ONE final decision for a research outcome — what is stored, logged, displayed and acted on.

Why this module exists (v2.5)
-----------------------------
In v2.4 the unified engine made the decision, but three other places still carried their own
numbers:

* `ResearchOutcome.score`, the `research_snapshots.score` column, `candidates.last_score`, the
  `/research` reply ("Score: 41.2"), the dashboard decision list and the `alerts.score` column all
  held the OLD v2.3 opportunity score (`report["score"]["total"]`), while the decision panel and
  the reasons said "score 58/100" from the unified engine. Same coin, same message, two scores.
* `ResearchOutcome.risk` held the old `assess_risk()` level (e.g. CRITICAL) while the panel said
  the unified risk (e.g. HIGH).
* the engine re-ran the unified engine after attaching live context, the learning brain and the
  AI read, but the stored snapshot had already been written, and `_refresh_unified` replaced the
  report's decision without updating `out.action` — the bot could act on one decision while the
  report showed another.

`final_view()` derives every one of those values from the unified result, and `apply_final()`
writes them onto the outcome. The legacy v2.3 score is kept in the report as
`report["score"]["total"]` (the brain still uses it as an input feature) and is labelled as such
wherever it is shown; it never decides anything.
"""
from __future__ import annotations

from typing import Optional

from .models import Action, Confidence, RiskLevel

# unified risk class -> the RiskLevel enum used by the DB and the older displays
RISK_MAP = {"EXTREME": RiskLevel.CRITICAL, "HIGH": RiskLevel.ELEVATED,
            "MODERATE": RiskLevel.MODERATE, "LOW": RiskLevel.LOW_DETECTED}
SCORE_SOURCE_UNIFIED = "unified decision engine"


def has_unified(report: dict) -> bool:
    u = report.get("unified")
    return isinstance(u, dict) and bool(u.get("version")) and bool(u.get("decision"))


def final_view(u: dict, policy: Optional[list[str]] = None,
               notes: Optional[list[str]] = None) -> dict:
    """Everything downstream needs, derived from one unified result.

    `policy` are publication-policy blockers (cooldown, daily cap, callout market-cap minimum):
    they can hold a call back to WATCH but never create one. `notes` are short lines from the
    layers that moved the decision (learning brain, AI read), shown first.
    """
    from .analysis.unified import INSUFFICIENT, LEGACY_ACTION

    policy = list(policy or [])
    decision = str(u.get("decision") or INSUFFICIENT)
    action = Action(LEGACY_ACTION.get(decision, "REJECT"))
    held = action == Action.AUTO_POST and bool(policy)
    if held:
        action = Action.WATCHLIST
    risk = RiskLevel.INCOMPLETE if decision == INSUFFICIENT else RISK_MAP.get(str(u.get("risk")),
                                                                              RiskLevel.INCOMPLETE)
    try:
        confidence = Confidence(str(u.get("confidence") or "LOW"))
    except ValueError:
        confidence = Confidence.LOW
    score = u.get("score")
    score = round(float(score), 1) if isinstance(score, (int, float)) else None
    head = (f"{decision} — score {score if score is not None else 0:.0f}/100, risk {u.get('risk')}, "
            f"confidence {u.get('confidence')}, evidence "
            f"{float(u.get('completeness') or 0) * 100:.0f}%")
    reasons = list(notes or []) + [head] + list(u.get("why") or [])[:3]
    if held:
        reasons.insert(len(notes or []) + 1, "held back by publication policy: " + "; ".join(policy))
    blockers = policy + [x for x in (list(u.get("hard_vetoes") or []) + list(u.get("blocking") or [])
                                     + list(u.get("unresolved") or []) + list(u.get("warnings") or [])
                                     + list(u.get("timing") or []) + list(u.get("evidence_gaps") or []))
                         if x not in policy]
    # de-duplicate, keep order
    seen, uniq = set(), []
    for b in blockers:
        if b not in seen:
            seen.add(b)
            uniq.append(b)
    reason_class = "POLICY_HOLD" if held else str(u.get("reason_class") or "")
    return {
        "action": action, "decision": decision, "score": score, "risk": risk,
        "risk_class": u.get("risk"), "confidence": confidence,
        "completeness": u.get("completeness"), "reasons": reasons, "blockers": uniq[:8],
        "reason_class": reason_class, "held_by_policy": held, "policy": policy,
        "safety": (u.get("safety") or {}).get("status"),
        "early_entry": (u.get("early_entry") or {}).get("grade"),
    }


def apply_final(out, notes: Optional[list[str]] = None) -> bool:
    """Write the unified decision onto a ResearchOutcome and its report. False when the report
    has no unified result (unified engine off, pre-migration path, early rejects) — those keep
    the values their own path set."""
    report = out.report
    if not has_unified(report):
        report["final"] = {
            "action": out.action.value, "score": out.score,
            "score_source": ("pre-migration activity heuristic"
                             if report.get("lifecycle") == "PRE_MIGRATION_BONDING_CURVE"
                             else "legacy v2.3 path (no unified result)"),
            "risk": out.risk.value, "confidence": out.confidence.value,
            "reason_class": report.get("final", {}).get("reason_class") or _early_class(out),
        }
        return False
    v = final_view(report["unified"], report.get("policy_blockers"), notes)
    out.action, out.score, out.risk, out.confidence = v["action"], v["score"], v["risk"], v["confidence"]
    out.reasons = v["reasons"] + [b for b in v["blockers"] if b not in v["reasons"]][:6]
    report["decision"] = {"action": out.action.value, "reasons": v["reasons"], "blockers": v["blockers"]}
    report["final_action"] = out.action.value
    report["reasons"] = out.reasons
    report["risk"] = out.risk.value
    report["confidence"] = out.confidence.value
    report["final"] = {
        "action": out.action.value, "decision": v["decision"], "score": v["score"],
        "score_source": SCORE_SOURCE_UNIFIED, "risk": out.risk.value, "risk_class": v["risk_class"],
        "confidence": out.confidence.value, "completeness": v["completeness"],
        "safety": v["safety"], "early_entry": v["early_entry"],
        "reason_class": v["reason_class"], "held_by_policy": v["held_by_policy"],
        "legacy_score_v23": (report.get("score") or {}).get("total"),
    }
    return True


def _early_class(out) -> str:
    """Reason class for outcomes that never reached the unified engine."""
    reasons = " ".join(str(r) for r in (out.reasons or [])).lower()
    if out.action == Action.INSUFFICIENT_DATA:
        return "PROVIDER_FAILURE" if ("unavailable" in reasons or "failed" in reasons
                                      or "rpc" in reasons) else "CRITICAL_UNKNOWN"
    if out.action == Action.REJECT:
        if reasons.startswith("prefilter") or "prefilter:" in reasons:
            return "PREFILTER"
        failed = {g.get("name") for g in out.report.get("gates") or []
                  if isinstance(g, dict) and g.get("status") == "FAIL"}
        if failed or "gate failed" in reasons:
            from .analysis.unified import THRESHOLD_GATES
            return "RISK_THRESHOLD" if (failed and failed <= THRESHOLD_GATES) else "SAFETY_FAIL"
        if "invalid mint" in reasons or "not a valid spl mint" in reasons:
            return "INVALID"
        return "WEAK_OPPORTUNITY"
    if out.action == Action.WATCHLIST:
        return "BELOW_CALL_LINE"
    return "CALL" if out.action == Action.AUTO_POST else ""
