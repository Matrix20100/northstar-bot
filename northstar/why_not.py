"""Why wasn't this called? (v5.5.0)

Paste any coin address on the dashboard and get one plain answer, with the timeline behind it, from what the bot itself
recorded: whether it ever saw the coin and where from, every research decision with its reasons, the runner-call
check (score, the first safety blocker, how the coin did afterwards on the curve), confirmed-rug blocks, and the calls
it did make. Nothing is fetched or guessed: a coin the bot never saw says so.
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional

BASE58 = re.compile(r"[1-9A-HJ-NP-Za-km-z]+")


def clean_mint(text: str) -> Optional[str]:
    """The coin address in pasted text (stray punctuation, a trailing \\, spaces, a full URL all removed)."""
    best = max(BASE58.findall(text or ""), key=len, default="")
    return best if 32 <= len(best) <= 44 else None


def _rows(db, sql: str, args: tuple) -> list[dict]:
    try:
        return [dict(r) for r in db.fetchall(sql, args)]
    except Exception:  # noqa: BLE001 - a table that does not exist yet
        return []


def _json(v, default):
    try:
        out = json.loads(v) if isinstance(v, str) else v
    except (TypeError, ValueError):
        return default
    return default if out is None else out


def _money(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    return f"${v / 1e6:.2f}M" if v >= 1e6 else f"${v / 1e3:.1f}K" if v >= 1e3 else f"${v:,.0f}"


def explain(db, mint: str, now: Optional[float] = None) -> dict:
    now = now or time.time()
    cand = (_rows(db, "SELECT * FROM candidates WHERE mint = ?", (mint,)) or [None])[0]
    early = (_rows(db, "SELECT mint, created_at, entry_mcap, peak_mcap, status, report FROM early_calls WHERE mint = ?",
                   (mint,)) or [None])[0]
    alerts = _rows(db, "SELECT id, created_at, entry_mcap, max_multiple, status, call_type FROM alerts WHERE mint = ? "
                       "AND status IN ('SENT','DRY_RUN') ORDER BY created_at", (mint,))
    snaps = _rows(db, "SELECT ts, action, score, risk, report FROM research_snapshots WHERE mint = ? ORDER BY ts DESC "
                      "LIMIT 12", (mint,))
    curve = (_rows(db, "SELECT * FROM curve_watch WHERE mint = ?", (mint,)) or [None])[0]
    funnel = _rows(db, "SELECT ts, stage, outcome, reason FROM funnel_events WHERE mint = ? ORDER BY ts DESC LIMIT 15",
                   (mint,))

    symbol = (cand or {}).get("symbol") or (curve or {}).get("symbol")
    if early:
        symbol = symbol or ((_json(early.get("report"), {}) or {}).get("identity") or {}).get("symbol")
    timeline: list[dict] = []

    def add(ts, what, detail=""):
        if ts:
            timeline.append({"at": ts, "what": what, "detail": detail})

    if cand:
        add(cand.get("discovered_at"), "Discovered", "from " + ", ".join(_json(cand.get("sources"), []) or ["?"]))
    if curve:
        add(curve.get("entered_at"), "Entered the early band",
            f"{_money(curve.get('entry_mc'))} on the curve, {curve.get('age_min') or 0:.0f} min old")
        if curve.get("runner_at"):
            add(curve["runner_at"], "Runner-call check",
                f"score {curve.get('runner_score') or 0:.0f}; "
                + ("every safety check passed" if curve.get("runner_safe") else
                   f"blocked by: {curve.get('runner_block') or 'a safety check'}"))
    research = []
    for sn in reversed(snaps):
        rep = _json(sn.get("report"), {}) or {}
        reasons = [str(x) for x in (rep.get("reasons") or [])][:4]
        runner = rep.get("runner") or {}
        research.append({"at": sn["ts"], "action": sn["action"], "score": sn.get("score"), "risk": sn.get("risk"),
                         "reasons": reasons, "runner_blockers": [str(b) for b in runner.get("blockers") or []][:5],
                         "runner_score": runner.get("score")})
        add(sn["ts"], f"Researched: {sn['action']}",
            "; ".join((runner.get("blockers") or [])[:2] or reasons[:2]))
    for a in alerts:
        add(a["created_at"], "Called (standard call)", f"at {_money(a.get('entry_mcap'))}")
    if early:
        add(early["created_at"], "Called (runner / early call)", f"at {_money(early.get('entry_mcap'))}")
    timeline.sort(key=lambda e: e["at"])

    # the one-line answer
    blocked = (cand or {}).get("state") == "RUGGED"
    last = research[-1] if research else None
    if early or alerts:
        first = early["created_at"] if early else alerts[0]["created_at"]
        entry = early["entry_mcap"] if early else alerts[0]["entry_mcap"]
        verdict, answer = "CALLED", f"It was called, at {_money(entry)} ({time.strftime('%d %b %H:%M UTC', time.gmtime(first))})."
    elif not cand and not curve:
        verdict, answer = "NEVER_SEEN", ("The bot never saw this coin: none of its feeds (pump.fun launches and "
                                         "migrations, DexScreener profiles/boosts, the surge and curve radars, "
                                         "tracked wallets) brought it in, so it was never researched.")
    elif blocked:
        why = (cand.get("reasons") and _json(cand["reasons"], [])[:1]) or ["a confirmed rug"]
        verdict, answer = "BLOCKED", f"It was blocked as a confirmed rug: {why[0]}."
    elif last is None:
        verdict, answer = "NOT_RESEARCHED", ("The bot saw it but never finished researching it (it was waiting in the "
                                             "queue, or not ready: no pool or curve data yet).")
    else:
        rb = last["runner_blockers"]
        if rb:
            verdict, answer = "RUNNER_BLOCKED", f"The runner-call check stopped it: {rb[0]}."
        elif curve and curve.get("runner_at") and not curve.get("runner_safe"):
            verdict, answer = "RUNNER_BLOCKED", f"The runner-call check stopped it: {curve.get('runner_block')}."
        else:
            top = last["reasons"][0] if last["reasons"] else "no reason recorded"
            verdict, answer = last["action"] or "REJECT", f"Its last research said {last['action']}: {top}"

    after = None
    if curve and curve.get("entry_mc"):
        peak_x = (curve.get("peak_mc") or curve["entry_mc"]) / curve["entry_mc"]
        after = (f"After entering the band at {_money(curve['entry_mc'])} it went as high as "
                 f"{_money(curve.get('peak_mc'))} ({peak_x:.1f}x)" + (", then migrated" if curve.get("migrated") else ""))
    return {"mint": mint, "symbol": symbol, "verdict": verdict, "answer": answer, "after": after,
            "state": (cand or {}).get("state"), "research_count": (cand or {}).get("research_count"),
            "timeline": timeline[-30:], "research": research[-6:], "funnel": funnel}
