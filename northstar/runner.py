"""Runner calls (v3.0.0): the call engine for young pump.fun coins, no AI key needed.

What a call bot like Nexas posts: a pump.fun coin under ~45 minutes old, $15K-$90K market cap, with
real 5-minute volume. Northstar calls the same kind of coin, but only after the research has
checked the coin and nothing failed.

A coin is CALLED when every one of these holds:
  1. momentum: inside the band, young enough, 5-min volume at or above `runner_min_vol_m5`, buys
     at least matching sells (DexScreener, read by the curve radar seconds before research)
  2. safety, every check RAN and PASSED:
     - mint / freeze authority revoked, no restricting token extension, holder concentration and
       dev holding within limits (on-chain curve holders)
     - launch buys organic (no bundled or sniped start), snipers no longer holding a big share
     - no tiny-trade wash by insiders; launch chart not an instant jump, staircase or dump
     - no failed gate of any kind; rug check not RUG LIKELY / HIGH RISK; no recycled socials
  3. runner score at or above `runner_min_score`:
       momentum  45  (volume vs market cap, buy pressure, buyers, rising)
       safety    30  (organic launch, low sniper share, spread holders, small dev bag, no wash)
       attention 25  (attention.py: KOL channels, tracked wallets, launch waves, trending, news)
     The weights are a first, transparent setting. Every candidate's score is stored, and the curve
     watch records what happened, so they can be checked against outcomes.
"""
from __future__ import annotations

import json
import time
from typing import Optional

REQUIRED_PASS = ("mint_authority", "freeze_authority", "token_extensions", "holder_concentration",
                 "creator_holdings", "launch_buys")


def radar_metrics(db, mint: str, now: Optional[float] = None, max_age_s: float = 900) -> Optional[dict]:
    """The latest DexScreener numbers the curve radar saw for this coin (within `max_age_s`)."""
    now = now or time.time()
    row = db.fetchone("SELECT payload, COALESCE(last_ts, ts) AS t FROM discovery_events WHERE mint = ? "
                      "AND source = 'curve_radar' ORDER BY id DESC LIMIT 1", (mint,))
    if not row or now - float(row["t"] or 0) > max_age_s:
        return None
    try:
        p = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        return None
    return p if isinstance(p, dict) and p.get("market_cap") else None


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def score(report: dict, m: dict, attention: Optional[dict]) -> dict:
    """{"score", "momentum", "safety", "attention", "notes"} - every part out of its own maximum."""
    pm = report.get("pre_migration") or {}
    mc = float(m.get("market_cap") or 0) or 1.0
    vol = float(m.get("vol_m5") or 0)
    buys, sells = float(m.get("buys_m5") or 0), float(m.get("sells_m5") or 0)
    buyers = float(pm.get("unique_buyers") or 0)
    chg = m.get("chg_m5")
    mom = (20 * _clamp((vol / mc) / 0.6)                      # 60% of the market cap traded in 5 min = full
           + 10 * _clamp((buys / max(1.0, sells) - 0.8) / 0.8)  # 1.6 buys per sell = full
           + 10 * _clamp(buyers / 30)                          # 30 distinct buyers in the sample = full
           + (5 if isinstance(chg, (int, float)) and chg > 0 else 0))
    lb = report.get("launch_buys") or {}
    h = report.get("holders") or {}
    micro = report.get("micro_trades") or {}
    sniper = lb.get("sniper_held_pct")
    top10 = h.get("top10_adj")
    dev = h.get("creator_pct")
    saf = ((10 if lb.get("verdict") == "ORGANIC" else 0)
           + (5 * _clamp(1 - (sniper or 0) / 15) if sniper is not None else 0)
           + (5 * _clamp(1 - max(0.0, (top10 or 0) - 15) / 20) if top10 is not None else 0)
           + (5 * _clamp(1 - (dev or 0) / 10) if dev is not None else 2.5)
           + (5 if micro.get("status") == "OBSERVED" and not micro.get("insider_micro") else 0))
    att = 25 * _clamp(float((attention or {}).get("score") or 0) / 100)
    total = round(mom + saf + att, 1)
    notes = [f"{vol / mc * 100:.0f}% of market cap traded in 5 min", f"{buys:.0f} buys / {sells:.0f} sells"]
    if lb.get("verdict") == "ORGANIC":
        notes.append(f"organic launch (first buyers took {lb.get('early_buy_pct', 0):.1f}%)")
    return {"score": total, "momentum": round(mom, 1), "safety": round(saf, 1), "attention": round(att, 1),
            "notes": notes}


def blockers(report: dict, m: Optional[dict], s, now: Optional[float] = None) -> list[str]:
    """Why this coin may NOT be called (empty list = it may)."""
    out = []
    if not m:
        return ["no fresh curve-radar numbers (volume, buys) for this coin"]
    mc = float(m.get("market_cap") or 0)
    lo, hi = float(getattr(s, "runner_min_mc", 15_000)), float(getattr(s, "runner_max_mc", 90_000))
    if not (lo <= mc <= hi):
        out.append(f"market cap ${mc:,.0f} outside ${lo:,.0f}-${hi:,.0f}")
    age = m.get("age_min")
    if age is not None and age > float(getattr(s, "runner_max_age_min", 45)):
        out.append(f"{age:.0f} min old (max {getattr(s, 'runner_max_age_min', 45):.0f})")
    vol = float(m.get("vol_m5") or 0)
    if vol < float(getattr(s, "runner_min_vol_m5", 7_500)):
        out.append(f"5-min volume ${vol:,.0f} under ${getattr(s, 'runner_min_vol_m5', 7_500):,.0f}")
    if float(m.get("buys_m5") or 0) < float(m.get("sells_m5") or 0):
        out.append("more sells than buys in the last 5 min")
    gates = {g.get("name"): g for g in report.get("gates") or [] if isinstance(g, dict)}
    for name in REQUIRED_PASS:
        g = gates.get(name)
        if g is None or g.get("status") != "PASS":
            out.append(f"{name.replace('_', ' ')} not passed" + (f": {g.get('reason')}" if g and g.get("reason") else ""))
    for name, g in gates.items():
        if g.get("status") == "FAIL" and name not in REQUIRED_PASS:
            out.append(f"{name.replace('_', ' ')}: {g.get('reason')}")
    lp = report.get("launch") or {}
    if lp.get("verdict") in ("INSTANT_JUMP", "STAIRCASE", "DUMPED"):
        out.append(f"launch chart: {(lp.get('reasons') or [lp['verdict']])[0]}")
    level = (report.get("verdict") or {}).get("level")
    if level in ("RUG LIKELY", "HIGH RISK"):
        out.append(f"rug check {level}")
    pm = report.get("pre_migration") or {}
    if (pm.get("socials") or {}).get("recycled"):
        out.append("recycled socials")
    if pm.get("curve_complete"):
        out.append("curve complete (migrating)")
    return out
