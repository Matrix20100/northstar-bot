"""Runner calls: the call engine for young pump.fun coins, no AI key needed (v3.0.0, reworked v4.0.0).

What call bots like Nexas post: a pump.fun coin under ~45 minutes old, $15K-$90K market cap, with
real 5-minute volume. Northstar calls the same kind of coin after its own research.

v4.0.0 (operator: "I don't want all coins to reject simply cause of 1 thing"): only REAL TRAPS block a
call. v4.0.1 (operator: "traps + extreme only"): plus two extreme cases, a bundle of 40%+ bought in
the creation block and a dev holding or buying 20%+ of supply (extreme()). Everything else is a penalty in the score, scaled by how bad it is, so one weak signal no
longer kills an otherwise strong coin, while a coin with several problems still scores too low.

A coin is CALLED when:
  1. it is a candidate: inside the band, young enough, 5-min volume at least `runner_min_vol_m5`,
     buys at least matching sells (DexScreener, read by the curve radar seconds before research);
  2. no trap: mint authority, freeze authority and token extensions RAN and PASSED (unknown is not a
     pass); the rug check is not RUG LIKELY; the curve is not complete;
  3. the runner score is at least `runner_min_score` (learned by the curve learner):
       momentum  40  (volume vs market cap, buy pressure, distinct buyers, rising)
       pulse    +10  (the radar's 15-minute pattern: a first pump, or a first pullback that bounces)
       safety    35  (35 minus penalties: bundled / sniped launch, snipers still holding, holder
                      concentration, dev bag, insider tiny trades, launch chart, rug-check red flags,
                      recycled socials, linked fresh wallets, checks that could not run)
       attention 25  (attention.py: tracked wallets buying now, KOL channels, waves, trending, news)
     The total is capped at 100. Every candidate's score, parts and penalties are stored, and the
     curve watch records what happened, so the weights can be checked against outcomes.
"""
from __future__ import annotations

import json
import time
from typing import Optional

# the only checks that block a call outright: a trap (v4.0.0)
TRAP_GATES = ("mint_authority", "freeze_authority", "token_extensions")
REQUIRED_PASS = TRAP_GATES          # kept for older imports


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


def _ramp(x: Optional[float], start: float, full: float, points: float) -> float:
    """0 penalty up to `start`, rising linearly to `points` at `full` and beyond."""
    if x is None or x <= start:
        return 0.0
    return round(points * _clamp((x - start) / max(1e-9, full - start)), 2)


def penalties(report: dict) -> list[tuple[str, float]]:
    """(reason, points) taken off the 35 safety points. Only problems; never a block."""
    out: list[tuple[str, float]] = []
    lb = report.get("launch_buys") or {}
    h = report.get("holders") or {}
    gates = {g.get("name"): g for g in report.get("gates") or [] if isinstance(g, dict)}

    def add(reason, pts):
        if pts > 0:
            out.append((reason, round(pts, 1)))

    if lb.get("verdict") in ("ORGANIC", "BUNDLED", "SNIPED"):
        add(f"bundled launch: {lb.get('bundle_pct') or 0:.0f}% bought in the creation block",
            _ramp(lb.get("bundle_pct"), 10, 50, 12))
        add(f"sniped launch: {lb.get('early_buy_pct') or 0:.0f}% bought in the first seconds",
            _ramp(lb.get("early_buy_pct"), 25, 60, 8))
        add(f"dev bought {lb.get('dev_buy_pct') or 0:.0f}% at launch", _ramp(lb.get("dev_buy_pct"), 10, 40, 5))
    else:
        add("launch buys could not be read", 4)
    if lb.get("sniper_held_pct") is not None:
        add(f"launch snipers still hold {lb['sniper_held_pct']:.0f}%", _ramp(lb.get("sniper_held_pct"), 15, 40, 6))
    if h.get("top10_adj") is not None:
        add(f"top 10 hold {h['top10_adj']:.0f}%", _ramp(h.get("top10_adj"), 25, 50, 6))
        add(f"largest holder {h.get('max_single_adj') or 0:.0f}%", _ramp(h.get("max_single_adj"), 8, 20, 4))
    else:
        add("holders could not be read", 4)
    add(f"dev holds {h.get('creator_pct') or 0:.0f}%", _ramp(h.get("creator_pct"), 5, 20, 6))
    if (gates.get("insider_micro_trades") or {}).get("status") == "FAIL":
        add("insiders making tiny trades to fake activity", 4)
    if (gates.get("wallet_network") or {}).get("status") == "FAIL":
        add("fresh or linked wallets hold a big share", 6)
    lp = report.get("launch") or {}
    add({"INSTANT_JUMP": "launch chart jumped in its first minutes", "STAIRCASE": "staircase launch chart",
         "DUMPED": "already dumped from its high"}.get(lp.get("verdict"), ""),
        {"INSTANT_JUMP": 5, "STAIRCASE": 5, "DUMPED": 6}.get(lp.get("verdict"), 0))
    v = report.get("verdict") or {}
    add("rug check: HIGH RISK", 8 if v.get("level") == "HIGH RISK" else 0)
    add("rug check: CAUTION", 2 if v.get("level") == "CAUTION" else 0)
    serious = [x for x in v.get("red") or [] if isinstance(x, dict) and x.get("level") in ("high", "critical")]
    add(f"{len(serious)} serious rug-check red flag(s)", min(6, 2 * len(serious)))
    if ((report.get("pre_migration") or {}).get("socials") or {}).get("recycled"):
        add("recycled socials", 4)
    return out


def score(report: dict, m: dict, attention: Optional[dict]) -> dict:
    """{"score", "momentum", "pulse", "safety", "attention", "penalties", "notes"}."""
    pm = report.get("pre_migration") or {}
    mc = float(m.get("market_cap") or 0) or 1.0
    vol = float(m.get("vol_m5") or 0)
    buys, sells = float(m.get("buys_m5") or 0), float(m.get("sells_m5") or 0)
    buyers = float(pm.get("unique_buyers") or 0)
    chg = m.get("chg_m5")
    mom = (15 * _clamp((vol / mc) / 0.6)                      # 60% of the market cap traded in 5 min = full
           + 10 * _clamp((buys / max(1.0, sells) - 0.8) / 0.8)  # 1.6 buys per sell = full
           + 10 * _clamp(buyers / 30)                          # 30 distinct buyers in the sample = full
           + (5 if isinstance(chg, (int, float)) and chg > 0 else 0))
    pulse = (m.get("pulse") or {}) if isinstance(m.get("pulse"), dict) else {}
    pb = float(pulse.get("bonus") or 0)
    pen = penalties(report)
    saf = max(0.0, 35.0 - sum(p for _, p in pen))
    att = 25 * _clamp(float((attention or {}).get("score") or 0) / 100)
    total = round(min(100.0, max(0.0, mom + pb + saf + att)), 1)
    notes = [f"{vol / mc * 100:.0f}% of market cap traded in 5 min", f"{buys:.0f} buys / {sells:.0f} sells"]
    if pulse.get("pattern"):
        notes.append(f"pulse: {pulse['pattern']}")
    return {"score": total, "momentum": round(mom, 1), "pulse": round(pb, 1), "safety": round(saf, 1),
            "attention": round(att, 1), "penalties": [{"why": w, "points": p} for w, p in pen], "notes": notes}


def extreme(report: dict, s) -> list[str]:
    """v4.0.1 (operator: "traps + extreme only"): cases so extreme they reject outright, however
    strong the rest of the coin looks. Everything milder stays a score penalty (penalties())."""
    out = []
    lb = report.get("launch_buys") or {}
    h = report.get("holders") or {}
    bundle_max = float(getattr(s, "runner_reject_bundle_pct", 40.0) or 0)
    dev_max = float(getattr(s, "runner_reject_dev_pct", 20.0) or 0)
    if bundle_max and (lb.get("bundle_pct") or 0) >= bundle_max:
        out.append(f"extreme: {lb['bundle_pct']:.0f}% bought in the creation block (bundled; reject at "
                   f"{bundle_max:.0f}%)")
    dev = max(float(lb.get("dev_buy_pct") or 0), float(h.get("creator_pct") or 0))
    if dev_max and dev >= dev_max:
        out.append(f"extreme: the dev holds or bought {dev:.0f}% of supply (reject at {dev_max:.0f}%)")
    return out


def blockers(report: dict, m: Optional[dict], s, now: Optional[float] = None) -> list[str]:
    """Why this coin may NOT be called (empty list = it may). v4.0.0: candidate rules and traps only."""
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
    for name in TRAP_GATES:
        g = gates.get(name)
        if g is None or g.get("status") != "PASS":
            out.append(f"trap check: {name.replace('_', ' ')} not passed" + (f": {g.get('reason')}" if g and g.get("reason") else ""))
    level = (report.get("verdict") or {}).get("level")
    if level == "RUG LIKELY":
        out.append("trap check: rug check RUG LIKELY")
    out += extreme(report, s)
    if (report.get("pre_migration") or {}).get("curve_complete"):
        out.append("curve complete (migrating)")
    return out


# ----------------------------------------------------------------------------- pulse pattern
def pulse_pattern(samples: list[tuple[float, float]], now: float, window_s: float = 900) -> dict:
    """The radar's market-cap samples over the last 15 minutes -> a pattern and a score bonus.

    first pullback (+10): it pumped 1.5x+ from its low, pulled back to 55-85% of that high, and is
        bouncing (5%+ above the low it made after the high): the entry call bots look for
    first pump    (+6):  up 1.4x+ inside the window and still within 15% of its high
    dumping       (-8):  under half of its high in the window
    """
    pts = [(t, mc) for t, mc in samples if now - t <= window_s and mc]
    out = {"samples": len(pts), "pattern": None, "bonus": 0.0}
    if len(pts) < 3:
        return out
    first = pts[0][1]
    i_hi = max(range(len(pts)), key=lambda i: pts[i][1])
    hi = pts[i_hi][1]
    low_before = min(mc for _, mc in pts[:i_hi + 1])
    last = pts[-1][1]
    after = [mc for _, mc in pts[i_hi + 1:]]
    if last < 0.5 * hi:
        out.update(pattern="dumping: under half its 15-minute high", bonus=-8.0)
    elif after and hi >= 1.5 * low_before and 0.55 * hi <= min(after) <= 0.85 * hi and last >= 1.05 * min(after):
        out.update(pattern=f"first pullback to {min(after) / hi * 100:.0f}% of its high, bouncing", bonus=10.0)
    elif hi >= 1.4 * first and last >= 0.85 * hi:
        out.update(pattern=f"first pump: {hi / first:.1f}x in 15 min, holding near the high", bonus=6.0)
    return out
