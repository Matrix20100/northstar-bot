"""Decision funnel telemetry: how many coins reached each stage, and exactly why they stopped.

Before v2.5 the only record of "why was nothing called?" was the last reason string on each
candidate row and a handful of in-memory counters. This keeps one row per stage outcome in the
`funnel_events` table and summarises them:

    discovered -> researched -> (not ready | prefiltered | decided)
               -> decided:  REJECT / INSUFFICIENT DATA / CAUTION / WATCH / APPROVED
               -> approved: CALLED / HELD (cooldown, cap, price moved, Discord error)

Every rejection carries a `reason_class` so the causes can be counted separately:

    SAFETY_FAIL         a proven trap (mint/freeze authority, transfer hooks, no sell route...)
    RISK_THRESHOLD      a measured risk past a configured bar (holder concentration, liquidity...)
    PROVIDER_FAILURE    a critical check could not run because a provider was down (retried)
    CRITICAL_UNKNOWN    a critical check ran but could not tell
    SERIOUS_WARNINGS    two or more serious, unproven warnings
    WEAK_OPPORTUNITY    safe enough, but the score is below the watch line
    TIMING              extended / fading / no momentum right now (WATCH)
    EVIDENCE_GAP        missing evidence a call needs, e.g. narrative or a verified trade sample
    ESSENTIAL_DATA_THIN an essential category is too blind or too weak
    LOW_CONFIDENCE      evidence too thin or contradictory for a call
    BELOW_CALL_LINE     a watchable score that is not yet a call
    POLICY_HOLD         a call held back by cooldown / daily cap / callout market-cap minimum
    PREFILTER           dropped by the cheap prefilters (age, liquidity, activity)
    NOT_READY           nothing to judge yet (no pool, no trades) — not a decision

Telemetry must never break research: every function here swallows its own errors.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

log = logging.getLogger("northstar.telemetry")

STAGE_RESEARCH = "RESEARCH"
STAGE_PUBLISH = "PUBLISH"

OUTCOME_BY_ACTION = {"AUTO_POST": "APPROVED", "WATCHLIST": "WATCH", "REJECT": "REJECT",
                     "INSUFFICIENT_DATA": "INSUFFICIENT"}


def _first_reason(out) -> str:
    """The most specific reason: the unified engine's first 'why', else the first reason."""
    u = out.report.get("unified") if isinstance(out.report.get("unified"), dict) else {}
    why = list(u.get("why") or []) if u else []
    for r in why + list(out.reasons or []):
        r = str(r)
        # the headline ("WATCH — score 61/100, ...") is a summary, not a reason
        if r and not re.match(r"^(STRONG OPPORTUNITY|WATCH|CAUTION|REJECT|INSUFFICIENT DATA) — score", r):
            return r[:300]
    return ""


def outcome_row(out, now: Optional[float] = None) -> tuple:
    """(ts, mint, stage, outcome, reason_class, reason, score) for one research outcome."""
    now = now or time.time()
    final = out.report.get("final") if isinstance(out.report.get("final"), dict) else {}
    if getattr(out, "not_ready", False):
        return (now, out.mint, STAGE_RESEARCH, "NOT_READY", "NOT_READY",
                str((out.reasons or [""])[0])[:300], out.score)
    outcome = OUTCOME_BY_ACTION.get(out.action.value, out.action.value)
    decision = final.get("decision")
    if decision == "CAUTION":
        outcome = "CAUTION"          # stored as WATCHLIST, reported separately
    cls = final.get("reason_class") or ""
    if not cls:
        from .final import _early_class
        cls = _early_class(out)
    return (now, out.mint, STAGE_RESEARCH, outcome, cls, _first_reason(out), out.score)


def record_outcome(db, out, now: Optional[float] = None) -> None:
    try:
        db.record_funnel([outcome_row(out, now)])
    except Exception:  # noqa: BLE001 - telemetry must never break research
        log.debug("funnel record failed for %s", getattr(out, "mint", "?"), exc_info=True)


def record_publish(db, mint: str, called: bool, reason: Optional[str], score: Optional[float],
                   call_type: str = "STANDARD", now: Optional[float] = None) -> None:
    try:
        db.record_funnel([(now or time.time(), mint, STAGE_PUBLISH,
                           ("CALLED" if called else "HELD") + ("" if call_type == "STANDARD" else f"_{call_type}"),
                           "CALL" if called else "POLICY_HOLD", (reason or "")[:300], score)])
    except Exception:  # noqa: BLE001
        log.debug("funnel publish record failed for %s", mint, exc_info=True)


def _norm(reason: str) -> str:
    """Group reasons that differ only by numbers ('score 41.2 below' == 'score 39.0 below')."""
    r = re.sub(r"\$[\d,]+(\.\d+)?", "$N", reason or "")
    r = re.sub(r"[-+]?\d+(\.\d+)?", "N", r)
    return r[:160]


def summary(db, hours: float = 24.0, now: Optional[float] = None, top: int = 8) -> dict[str, Any]:
    """The funnel for the last `hours`, with counts and exact reasons at every stage."""
    now = now or time.time()
    since = now - hours * 3600
    out: dict[str, Any] = {"hours": hours, "since": since}
    try:
        out["discovered"] = db.fetchone("SELECT COUNT(*) n FROM candidates WHERE discovered_at > ?",
                                        (since,))["n"]
        rows = db.fetchall("SELECT stage, outcome, reason_class, reason FROM funnel_events WHERE ts > ?",
                           (since,))
    except Exception as e:  # noqa: BLE001
        return {**out, "error": f"{type(e).__name__}: {e}"}
    research = [r for r in rows if r["stage"] == STAGE_RESEARCH]
    publish = [r for r in rows if r["stage"] == STAGE_PUBLISH]
    by_outcome: dict[str, int] = {}
    by_class: dict[str, dict[str, int]] = {}
    reasons: dict[str, dict[str, list]] = {}
    for r in research:
        o, c = r["outcome"], r["reason_class"] or "UNCLASSIFIED"
        by_outcome[o] = by_outcome.get(o, 0) + 1
        by_class.setdefault(o, {})
        by_class[o][c] = by_class[o].get(c, 0) + 1
        key = _norm(r["reason"] or "")
        slot = reasons.setdefault(o, {}).setdefault(key, [0, r["reason"] or ""])
        slot[0] += 1
    out["researched"] = sum(v for k, v in by_outcome.items() if k != "NOT_READY")
    out["not_ready"] = by_outcome.get("NOT_READY", 0)
    out["decisions"] = {k: v for k, v in by_outcome.items() if k != "NOT_READY"}
    out["approved"] = by_outcome.get("APPROVED", 0)
    out["called"] = sum(1 for r in publish if str(r["outcome"]).startswith("CALLED"))
    out["held"] = sum(1 for r in publish if str(r["outcome"]).startswith("HELD"))
    out["by_reason_class"] = by_class
    out["top_reasons"] = {o: [{"count": n, "example": ex} for _, (n, ex) in
                              sorted(d.items(), key=lambda kv: -kv[1][0])[:top]]
                          for o, d in reasons.items()}
    out["held_reasons"] = [r["reason"] for r in publish if str(r["outcome"]).startswith("HELD")][:top]
    return out


def format_summary(sm: dict) -> str:
    """Plain-text funnel for the CLI and logs."""
    if sm.get("error"):
        return f"funnel unavailable: {sm['error']}"
    lines = [f"Northstar decision funnel — last {sm['hours']:g}h",
             f"  discovered (new candidates)   {sm.get('discovered', 0)}",
             f"  researched to a decision      {sm.get('researched', 0)}",
             f"  not ready (nothing to judge)  {sm.get('not_ready', 0)}"]
    for o in ("REJECT", "INSUFFICIENT", "CAUTION", "WATCH", "APPROVED"):
        n = (sm.get("decisions") or {}).get(o, 0)
        lines.append(f"  {o:<29} {n}")
        for cls, k in sorted(((sm.get("by_reason_class") or {}).get(o) or {}).items(), key=lambda kv: -kv[1]):
            lines.append(f"      {cls:<26} {k}")
    lines.append(f"  called (posted)               {sm.get('called', 0)}")
    lines.append(f"  approved but held             {sm.get('held', 0)}")
    for o, items in (sm.get("top_reasons") or {}).items():
        if not items or o in ("APPROVED",):
            continue
        lines.append(f"\n  Top reasons — {o}:")
        lines += [f"    {it['count']:>4} × {it['example'][:140]}" for it in items]
    return "\n".join(lines)
