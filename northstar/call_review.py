"""Call review (v5.4.0): what separated the runner calls that died from the ones that ran.

Operator, on the runner calls: of 16 on 24 Oct, 10 never went above about 1x and died within minutes; 5 reached 2x+.
Stops cannot save a coin that gaps -50% to -80% between two checks; only not calling it can. Before any rule is
changed, this review shows, from the bot's own call history, which signals AT THE MOMENT OF THE CALL actually differ
between the two groups.

Every runner/early call (`early_calls`, with the research report stored at the call) is put in one outcome group:

  DIED   never got to 1.3x, and it died (the "dead" close) or fell to 0.5x or lower
  RAN    reached 2x or more
  OTHER  everything else (went a bit up, faded)

Calls younger than 2 hours that have neither died nor run yet are left out (their outcome is not known).

Then, for each signal the bot had at the call (launch bundling and snipers, dev and holders, tracked wallets, launch
waves and copycat names, momentum, score, grade, rug odds, entry market cap), it shows how often calls WITH the signal
died or ran against calls WITHOUT it, and the medians of each number in the DIED and RAN groups. Signals with fewer
than 5 calls on either side are marked "too few to tell". Nothing here changes how the bot calls: it is evidence for
the operator and Claude to choose a rule from.

Peaks are the stored ones. Peaks recorded before v5.2.1 may be inflated by the old price bug; the review notes it.
"""
from __future__ import annotations

import json
import statistics
import time
from typing import Callable, Optional

DIED, RAN, OTHER = "DIED", "RAN", "OTHER"
MIN_SIDE = 5
SETTLE_HOURS = 2.0


def _f(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x != x else x


def outcome(row: dict, now: float) -> Optional[str]:
    entry = _f(row.get("entry_mcap"))
    if not entry:
        return None
    peak_x = (_f(row.get("peak_mcap")) or entry) / entry
    low_x = (_f(row.get("low_mcap")) or entry) / entry
    try:
        sent = set(json.loads(row.get("plan_sent") or "[]"))
    except (TypeError, ValueError):
        sent = set()
    if peak_x >= 2.0:
        return RAN
    if peak_x < 1.3 and ("DEAD" in sent or low_x <= 0.5):
        return DIED
    if now - (row.get("created_at") or now) < SETTLE_HOURS * 3600 and row.get("status") in ("SENT", "DRY_RUN"):
        return None                                      # still open, not decided yet
    return OTHER


def features(report: dict) -> dict:
    """The numbers and flags the bot had at the moment of the call."""
    r = report if isinstance(report, dict) else {}
    lb = r.get("launch_buys") or {}
    h = r.get("holders") or {}
    pm = r.get("pre_migration") or {}
    run = r.get("runner") or {}
    m = run.get("metrics") or {}
    att = r.get("attention") or {}
    reasons = " | ".join(str(x) for x in att.get("reasons") or [])
    ctx = r.get("context") or {}
    tw = r.get("tracked_wallets") or {}
    u = r.get("unified") if isinstance(r.get("unified"), dict) else {}
    mc = _f(m.get("market_cap")) or _f(pm.get("market_cap_usd_current"))
    vol = _f(m.get("vol_m5"))
    buys, sells = _f(m.get("buys_m5")), _f(m.get("sells_m5"))
    conv = None
    try:
        from . import conviction as CV
        conv = CV.assess(r).score
    except Exception:  # noqa: BLE001
        pass
    return {
        "bundle_pct": _f(lb.get("bundle_pct")), "early_buy_pct": _f(lb.get("early_buy_pct")),
        "dev_pct": max(_f(lb.get("dev_buy_pct")) or 0, _f(h.get("creator_pct")) or 0) if (lb or h) else None,
        "sniper_held_pct": _f(lb.get("sniper_held_pct")), "launch_verdict": lb.get("verdict"),
        "top10_pct": _f(h.get("top10_adj")),
        "tracked_wallets": int(_f(tw.get("holding_count")) or 0) if tw.get("status") == "OBSERVED" else None,
        "launch_wave": "launch wave" in reasons,
        "copycat_launches_6h": _f(ctx.get("ticker_crowding_6h")),
        "unique_buyers": _f(pm.get("unique_buyers")),
        "vol_to_mcap_5m": (vol / mc) if vol is not None and mc else None,
        "buys_per_sell_5m": (buys / max(1.0, sells)) if buys is not None and sells is not None else None,
        "chg_m5": _f(m.get("chg_m5")),
        "score": _f(run.get("score")), "conviction": conv,
        "rug_odds": _f(u.get("rug_odds")), "entry_mcap": mc,
    }


# (label, test on the features; None = unknown for that call)
FLAGS: list[tuple[str, Callable[[dict], Optional[bool]]]] = [
    ("launch wave (many launches with the same word)", lambda f: f["launch_wave"]),
    ("copycat name (2+ other launches with the ticker in 6 h)",
     lambda f: None if f["copycat_launches_6h"] is None else f["copycat_launches_6h"] >= 2),
    ("bundled launch (20%+ bought in the creation block)",
     lambda f: None if f["bundle_pct"] is None else f["bundle_pct"] >= 20),
    ("sniped launch (35%+ bought in the first seconds)",
     lambda f: None if f["early_buy_pct"] is None else f["early_buy_pct"] >= 35),
    ("organic launch", lambda f: None if not f["launch_verdict"] else f["launch_verdict"] == "ORGANIC"),
    ("launch snipers still hold 10%+",
     lambda f: None if f["sniper_held_pct"] is None else f["sniper_held_pct"] >= 10),
    ("dev holds or bought 5%+", lambda f: None if f["dev_pct"] is None else f["dev_pct"] >= 5),
    ("top 10 holders own 30%+", lambda f: None if f["top10_pct"] is None else f["top10_pct"] >= 30),
    ("tracked wallets hold it", lambda f: None if f["tracked_wallets"] is None else f["tracked_wallets"] >= 1),
    ("price up 50%+ in the 5 min before the call", lambda f: None if f["chg_m5"] is None else f["chg_m5"] >= 50),
    ("2+ buys per sell in the last 5 min",
     lambda f: None if f["buys_per_sell_5m"] is None else f["buys_per_sell_5m"] >= 2),
    ("runner score 80+", lambda f: None if f["score"] is None else f["score"] >= 80),
    ("conviction grade C (under 50)", lambda f: None if f["conviction"] is None else f["conviction"] < 50),
    ("rug odds 30%+", lambda f: None if f["rug_odds"] is None else f["rug_odds"] >= 0.30),
    ("called at $25K+ market cap", lambda f: None if f["entry_mcap"] is None else f["entry_mcap"] >= 25_000),
]

NUMBERS = [("bundle_pct", "% bundled at launch"), ("early_buy_pct", "% sniped at launch"), ("dev_pct", "dev %"),
           ("top10_pct", "top 10 holders %"), ("unique_buyers", "unique buyers"),
           ("vol_to_mcap_5m", "5-min volume / market cap"), ("buys_per_sell_5m", "buys per sell (5 min)"),
           ("chg_m5", "5-min price change %"), ("copycat_launches_6h", "same-ticker launches (6 h)"),
           ("score", "runner score"), ("conviction", "conviction"), ("rug_odds", "rug odds"),
           ("entry_mcap", "market cap at the call")]


def _rate(rows: list, group: str) -> Optional[float]:
    return round(100 * sum(1 for g in rows if g == group) / len(rows), 1) if rows else None


def review(db, now: Optional[float] = None, days: float = 30.0) -> dict:
    now = now or time.time()
    try:
        rows = [dict(r) for r in db.fetchall(
            "SELECT mint, created_at, entry_mcap, peak_mcap, low_mcap, status, plan_sent, report FROM early_calls "
            "WHERE created_at >= ? AND status IN ('SENT','DRY_RUN','CLOSED','UNTRACKED') ORDER BY created_at DESC",
            (now - days * 86400,))]
    except Exception:  # noqa: BLE001 - no early_calls table yet
        return {"ready": False, "calls": 0}
    calls = []
    for r in rows:
        g = outcome(r, now)
        if g is None:
            continue
        try:
            rep = json.loads(r.get("report") or "{}")
        except (TypeError, ValueError):
            rep = {}
        entry = _f(r["entry_mcap"]) or 1.0
        calls.append({"mint": r["mint"], "symbol": (rep.get("identity") or {}).get("symbol"), "at": r["created_at"],
                      "group": g, "peak_x": round((_f(r.get("peak_mcap")) or entry) / entry, 2), "f": features(rep)})
    counts = {k: sum(1 for c in calls if c["group"] == k) for k in (DIED, RAN, OTHER)}
    flags = []
    for label, test in FLAGS:
        with_, without = [], []
        for c in calls:
            v = test(c["f"])
            if v is True:
                with_.append(c["group"])
            elif v is False:
                without.append(c["group"])
        if not with_ and not without:
            continue
        row = {"signal": label, "with": len(with_), "without": len(without),
               "died_with": _rate(with_, DIED), "died_without": _rate(without, DIED),
               "ran_with": _rate(with_, RAN), "ran_without": _rate(without, RAN),
               "enough": len(with_) >= MIN_SIDE and len(without) >= MIN_SIDE}
        if row["died_with"] is not None and row["died_without"] is not None:
            row["died_diff"] = round(row["died_with"] - row["died_without"], 1)
        flags.append(row)
    flags.sort(key=lambda r: (not r["enough"], -abs(r.get("died_diff") or 0)))
    numbers = []
    for key, label in NUMBERS:
        vals = {g: [c["f"][key] for c in calls if c["group"] == g and c["f"].get(key) is not None] for g in (DIED, RAN)}
        if not vals[DIED] and not vals[RAN]:
            continue
        numbers.append({"key": key, "label": label,
                        "died_median": round(statistics.median(vals[DIED]), 3) if vals[DIED] else None,
                        "ran_median": round(statistics.median(vals[RAN]), 3) if vals[RAN] else None,
                        "died_n": len(vals[DIED]), "ran_n": len(vals[RAN])})
    return {"ready": True, "days": days, "calls": len(calls), "counts": counts, "flags": flags, "numbers": numbers,
            "died_rate": _rate([c["group"] for c in calls], DIED), "ran_rate": _rate([c["group"] for c in calls], RAN),
            "recent": [{k: c[k] for k in ("mint", "symbol", "at", "group", "peak_x")} for c in calls[:40]],
            "note": "Peaks recorded before v5.2.1 (30 Oct 2026) may be inflated by the old price bug."}
