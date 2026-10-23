"""Confirmed rugs are blocked for good (v4.5.0).

Operator: "we need to stop even allowing rug coins to show up" - and, asked which coins: "confirmed
rugs only". A coin that is merely flagged (mint authority, RugCheck danger, high rug odds) is still
re-checked when the normal rules allow it, but it is never shown on the Watching tab (see `flags`).

A coin is a CONFIRMED rug when one of these is observed:

  * died: its market cap is back at `dead_mcap_usd` ($5K) or lower after a high of twice that or more
    (brain.is_dead, the same rule calls and the learners use);
  * liquidity pulled: its pool's liquidity fell by `rug_pulled_liquidity_pct` (90%) or more from the
    highest liquidity the bot saw for it, and that high was $5K or more;
  * dead pool: a DEX pool with less than `rug_dead_liquidity_usd` ($1,000) of liquidity. A pump.fun
    graduation starts with far more than that; a pool this empty has been drained or abandoned.

Such a coin gets the state RUGGED: it is never researched again, never re-queued by trades, the
radar, the wallet or Twitter feeds, and never shown. Its research stays stored, so the learners keep
it as a rug example.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from . import brain as B

log = logging.getLogger("northstar.rugs")

STATE = "RUGGED"
# a FAIL in one of these is a property of the token itself (analysis/unified.py SECURITY_GATES)
RUG_GATES = ("mint_authority", "freeze_authority", "token_extensions", "mint_account", "rugcheck_danger",
             "liquidity_removal", "lp_lock")
PULLED_MIN_PEAK_LIQ = 5_000.0


def _f(x) -> Optional[float]:
    if isinstance(x, dict):
        x = x.get("value")
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def numbers(report: dict, pair=None) -> tuple[Optional[float], Optional[float], bool]:
    """(market cap, liquidity, trades in a DEX pool) from a research report. Curve coins have no
    liquidity number: (market cap, None, False)."""
    if report.get("lifecycle") == "PRE_MIGRATION_BONDING_CURVE":
        pm = report.get("pre_migration") or {}
        return _f(pm.get("market_cap_usd_current")), None, False
    if pair is not None:
        return _f(pair.market_cap or pair.fdv), _f(pair.liquidity_usd), True
    m = report.get("market") or {}
    return _f(m.get("market_cap")), _f(m.get("liquidity_usd")), bool(m)


def confirmed(mc: Optional[float], liq: Optional[float], pooled: bool, peak_mc: Optional[float],
              peak_liq: Optional[float], s) -> Optional[str]:
    """Why this coin is a confirmed rug, or None."""
    dead_mc = float(getattr(s, "dead_mcap_usd", B.DEAD_MCAP) or 0)
    if mc and peak_mc and B.is_dead(mc, peak_mc, dead_mc):
        return f"died: market cap ${mc:,.0f} after a ${peak_mc:,.0f} high"
    if pooled and liq is not None:
        pulled = float(getattr(s, "rug_pulled_liquidity_pct", 90.0) or 0)
        if pulled and peak_liq and peak_liq >= PULLED_MIN_PEAK_LIQ and liq <= peak_liq * (1 - pulled / 100):
            return f"liquidity pulled: ${liq:,.0f} left of a ${peak_liq:,.0f} high"
        dead_liq = float(getattr(s, "rug_dead_liquidity_usd", 1_000.0) or 0)
        if dead_liq and liq < dead_liq:
            return f"dead pool: ${liq:,.0f} liquidity"
    return None


def flags(report: dict, s=None) -> list[str]:
    """Rug flags on a coin that is still alive: failed token-safety checks, high learned rug odds and
    the runner check's extreme rejects. A flagged coin can be re-checked but is never shown as watched."""
    out = []
    for g in report.get("gates") or []:
        if isinstance(g, dict) and g.get("status") == "FAIL" and g.get("name") in RUG_GATES:
            out.append(str(g["name"]).replace("_", " "))
    u = report.get("unified") if isinstance(report.get("unified"), dict) else {}
    odds = _f(u.get("rug_odds"))
    bar = float(getattr(s, "rug_flag_odds", 0.6) or 0) if s is not None else 0.6
    if odds is not None and bar and odds >= bar:
        out.append(f"rug odds {odds * 100:.0f}%")
    for b in ((report.get("runner") or {}).get("blockers") or []):
        if isinstance(b, str) and b.startswith("extreme:"):
            out.append(b[:80])
    return out[:6]


def peaks(cand: Optional[dict], mc: Optional[float], liq: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    """The highest market cap and liquidity seen for the coin, including this look."""
    cand = cand or {}
    pm = max([x for x in (_f(cand.get("peak_mcap")), mc) if x is not None], default=None)
    pl = max([x for x in (_f(cand.get("peak_liq")), liq) if x is not None], default=None)
    return pm, pl


def recorded_peak(db, mint: str) -> Optional[float]:
    """Highest market cap other parts of the bot recorded for the coin (curve watch, runner calls, VIP
    calls), so a coin's first research can already tell it died. Never raises."""
    best = None
    for sql in ("SELECT MAX(peak_mc) AS v FROM curve_watch WHERE mint = ?",
                "SELECT MAX(peak_mcap) AS v FROM early_calls WHERE mint = ?",
                "SELECT MAX(COALESCE(peak_mcap, entry_mcap * max_multiple)) AS v FROM alerts WHERE mint = ?"):
        try:
            row = db.fetchone(sql, (mint,))
        except Exception:  # noqa: BLE001 - table not created yet
            continue
        v = _f((row or {}).get("v"))
        if v is not None and (best is None or v > best):
            best = v
    return best


def block(db, mint: str, why: str, now: Optional[float] = None, **fields) -> None:
    """Mark a coin as a confirmed rug: never researched or shown again."""
    now = now or time.time()
    reasons = [f"confirmed rug: {why}"] + list(fields.pop("reasons", None) or [])[:19]
    db.set_state(mint, STATE, next_research_at=None, reasons=reasons, **fields)
    log.info("confirmed rug %s: %s (blocked)", mint, why)


def blocked_since(db, since: float) -> int:
    row = db.fetchone("SELECT COUNT(*) AS n FROM candidates WHERE state = ? AND updated_at > ?", (STATE, since))
    return int((row or {}).get("n") or 0)


def is_flagged(row: dict) -> bool:
    try:
        return bool(json.loads(row.get("rug_flags") or "[]"))
    except (TypeError, ValueError):
        return False
