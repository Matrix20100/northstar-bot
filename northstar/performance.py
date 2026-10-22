"""Outcome tracking and historical evaluation of Northstar's decisions.

* Every decision (AUTO_POST, WATCHLIST, REJECT, INSUFFICIENT_DATA) with an entry price is
  tracked — not only published calls — so the evaluation is not survivorship-biased.
* Dead pools / missing prices are explicit outcomes (UNAVAILABLE), never dropped. The report shows
  both an "observed-only" view and a conservative view that scores UNAVAILABLE as -100%.
* Decision-time scores come from the stored snapshot (no look-ahead). Outcomes are observed later.
* Max gain / drawdown are from periodic samples (~every outcome interval); intra-interval spikes
  are missed, so "reached 2x" is a lower bound on touches and says nothing about achievable exits.
* The report only SUGGESTS threshold changes; it never modifies live configuration.
"""
from __future__ import annotations

import logging
import statistics
import time
from collections import defaultdict
from typing import Optional

from .db import Database
from .net import ProviderError

log = logging.getLogger("northstar.performance")
MIN_SAMPLE = 30


class OutcomeTracker:
    def __init__(self, db: Database, dex):
        self.db, self.dex = db, dex

    async def run_once(self, now: Optional[float] = None) -> int:
        now = now or time.time()
        due = self.db.due_outcomes(now)
        tracking = self.db.active_tracking(int(getattr(self, "poll_limit", 600) or 600))
        snap_pool: dict[int, str] = {}
        ids = {r["snapshot_id"] for r in due} | {t["snapshot_id"] for t in tracking}
        for sid in ids:
            r = self.db.fetchone("SELECT pool_address FROM research_snapshots WHERE id=?", (sid,))
            if r and r["pool_address"]:
                snap_pool[sid] = r["pool_address"]
        mints = sorted({r["mint"] for r in due} | {t["mint"] for t in tracking})
        if not mints:
            return 0
        try:
            pairs = await self.dex.tokens_batch(mints)
        except ProviderError as e:
            log.warning("outcome refresh failed, will retry: %s", e)
            return 0  # do not mark anything UNAVAILABLE because OUR fetch failed
        by_pool = {p.pair_address: p for p in pairs}
        n = 0
        for r in due:
            p = by_pool.get(snap_pool.get(r["snapshot_id"], ""))
            if p and p.price_usd:
                self.db.execute("UPDATE outcomes SET observed_at=?, price=?, liquidity=?, status='OBSERVED' "
                                "WHERE snapshot_id=? AND horizon=?",
                                (now, p.price_usd, p.liquidity_usd, r["snapshot_id"], r["horizon"]))
            else:
                self.db.execute("UPDATE outcomes SET observed_at=?, status='UNAVAILABLE' WHERE snapshot_id=? "
                                "AND horizon=?", (now, r["snapshot_id"], r["horizon"]))
            n += 1
        for t in tracking:
            p = by_pool.get(snap_pool.get(t["snapshot_id"], ""))
            done = 1 if now - t["decided_at"] > 86400 + 600 else 0
            if p and p.price_usd:
                self.db.execute(
                    "UPDATE decision_tracking SET max_price=MAX(COALESCE(max_price,?),?), "
                    "min_price=MIN(COALESCE(min_price,?),?), last_price=?, "
                    "min_liq=MIN(COALESCE(min_liq,?),?), samples=samples+1, done=?, last_polled_at=? "
                    "WHERE snapshot_id=?",
                    (p.price_usd, p.price_usd, p.price_usd, p.price_usd, p.price_usd,
                     p.liquidity_usd or 0, p.liquidity_usd or 0, done, now, t["snapshot_id"]))
            else:
                self.db.execute("UPDATE decision_tracking SET missing=missing+1, done=?, last_polled_at=? "
                                "WHERE snapshot_id=?", (done, now, t["snapshot_id"]))
        return n


def _median(xs: list[float]) -> Optional[float]:
    return round(statistics.median(xs), 1) if xs else None


def performance_report(db: Database) -> dict:
    groups: dict[str, dict] = {}
    rows = db.fetchall("SELECT * FROM outcomes WHERE status != 'NO_ENTRY'")
    by_action_h: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by_action_h[(r["action"], r["horizon"])].append(r)
    track = db.fetchall("SELECT * FROM decision_tracking WHERE entry_price IS NOT NULL AND entry_price > 0")
    by_action_t: dict[str, list[dict]] = defaultdict(list)
    for t in track:
        by_action_t[t["action"]].append(t)
    actions = sorted({a for a, _ in by_action_h} | set(by_action_t))
    for a in actions:
        g: dict = {"decisions": len(by_action_t.get(a, [])), "horizons": {}}
        for h in ("5m", "15m", "1h", "6h", "24h"):
            rs = by_action_h.get((a, h), [])
            obs = [(r["price"] / r["entry_price"] - 1) * 100 for r in rs if r["status"] == "OBSERVED"
                   and r["entry_price"]]
            unav = sum(1 for r in rs if r["status"] == "UNAVAILABLE")
            pend = sum(1 for r in rs if r["status"] == "PENDING")
            conservative = obs + [-100.0] * unav
            g["horizons"][h] = {
                "resolved": len(obs) + unav, "pending": pend, "observed": len(obs), "unavailable": unav,
                "median_return_observed_pct": _median(obs),
                "median_return_conservative_pct": _median(conservative),
                "share_positive_observed_pct": round(sum(1 for x in obs if x > 0) / len(obs) * 100, 1) if obs else None,
            }
        ts = by_action_t.get(a, [])
        if ts:
            mx = [t["max_price"] / t["entry_price"] for t in ts if t["max_price"]]
            mn = [t["min_price"] / t["entry_price"] for t in ts if t["min_price"]]
            g["max_observed"] = {f"reached_{k}x_pct": round(sum(1 for m in mx if m >= k) / len(ts) * 100, 1)
                                 for k in (2, 5, 10)}
            g["severe_drawdown_50pct_share"] = round(sum(1 for m in mn if m <= 0.5) / len(ts) * 100, 1)
            g["median_max_multiple"] = round(statistics.median(mx), 2) if mx else None
            g["coverage"] = {"samples": sum(t["samples"] for t in ts), "missing": sum(t["missing"] for t in ts)}
        g["sample_warning"] = (f"only {g['decisions']} decisions — far too few to infer predictive value"
                               if g["decisions"] < MIN_SAMPLE else
                               "sample size adequate for description, still not proof of future performance")
        groups[a] = g

    # score-bucket view across all tracked decisions (suggestions only)
    buckets: dict[str, list[float]] = defaultdict(list)
    for t in track:
        if t["score"] is None or not t["max_price"]:
            continue
        b = f"{int(t['score'] // 10 * 10)}-{int(t['score'] // 10 * 10 + 9)}"
        buckets[b].append(t["max_price"] / t["entry_price"])
    bucket_view = {b: {"n": len(v), "reached_2x_pct": round(sum(1 for m in v if m >= 2) / len(v) * 100, 1)}
                   for b, v in sorted(buckets.items())}
    suggestions = []
    big = {b: d for b, d in bucket_view.items() if d["n"] >= MIN_SAMPLE}
    if len(big) >= 2:
        best = max(big.items(), key=lambda kv: kv[1]["reached_2x_pct"])
        suggestions.append(f"bucket {best[0]} has the highest observed 2x-touch rate ({best[1]['reached_2x_pct']}% "
                           f"of {best[1]['n']}); consider reviewing min_opportunity_score — validate before changing.")
    else:
        suggestions.append("not enough data per score bucket to suggest any threshold change")
    early_rows = db.fetchall("SELECT * FROM early_signals")
    early_mults = [r["current_market_cap_sol"] / r["entry_market_cap_sol"] for r in early_rows
                   if r["current_market_cap_sol"] is not None and r["entry_market_cap_sol"] is not None
                   and r["entry_market_cap_sol"] > 0]
    early_peaks = [r["peak_multiple"] for r in early_rows if r["peak_multiple"] is not None]
    early_troughs = [r["min_multiple"] for r in early_rows if r["min_multiple"] is not None]
    early_view = {
        "signals": len(early_rows),
        # posted = has a Discord message (status changes to MIGRATED/CLOSED/UNTRACKED after tracking ends)
        "sent": sum(1 for r in early_rows if r["message_id"] is not None),
        "silent": sum(1 for r in early_rows if r["status"] == "SILENT"),
        "pending_or_failed": sum(1 for r in early_rows if r["status"] == "PENDING"),
        "median_current_multiple": round(statistics.median(early_mults), 2) if early_mults else None,
        "median_peak_multiple": round(statistics.median(early_peaks), 2) if early_peaks else None,
        "median_min_multiple": round(statistics.median(early_troughs), 2) if early_troughs else None,
        "severe_drawdown_50pct_share": round(sum(1 for r in early_rows if (r["min_multiple"] or 1) <= 0.5) /
                                               len(early_rows) * 100, 1) if early_rows else None,
        "reached_2x_pct": round(sum(1 for r in early_rows if (r["peak_multiple"] or 1) >= 2) / len(early_rows) * 100, 1)
                           if early_rows else None,
        "unit": "market-cap multiples measured in SOL terms; separate from post-migration USD tracker",
        "warning": "pre-migration signals have incomplete holder/LP risk; peak observations are not achievable-exit proof",
    }
    calls_view = {}
    try:
        call_rows = db.fetchall("SELECT call_type, max_multiple, min_multiple, warnings FROM alerts "
                                "WHERE status IN ('SENT','DRY_RUN')")
    except Exception:  # noqa: BLE001 - very old databases without call_type
        call_rows = []
    for ct in sorted({r["call_type"] or "STANDARD" for r in call_rows}):
        rs = [r for r in call_rows if (r["call_type"] or "STANDARD") == ct]
        def has(r, k):
            return f"PLAN:{k}" in (r["warnings"] or "")
        peaks = [r["max_multiple"] for r in rs if r["max_multiple"]]
        calls_view[ct] = {
            "calls": len(rs),
            "median_peak": round(statistics.median(peaks), 2) if peaks else None,
            "tp1_pct": round(sum(has(r, "TP1") for r in rs) / len(rs) * 100, 1),
            "stopped_pct": round(sum(has(r, "STOP") or has(r, "DANGER") for r in rs) / len(rs) * 100, 1),
            "time_stop_pct": round(sum(has(r, "TIME") for r in rs) / len(rs) * 100, 1),
        }
    return {"generated_at": time.time(), "groups": groups, "score_buckets": bucket_view,
            "pre_migration": early_view, "suggestions": suggestions, "calls": calls_view,
            "notes": ["UNAVAILABLE outcomes (dead pools / missing prices) are included, never dropped",
                      "max/min multiples are from periodic samples and miss intra-interval moves",
                      "returns are mark-to-market prices, not achievable exits; realized profit is UNKNOWN",
                      "pre-migration SOL market-cap multiples are separate from post-migration USD outcomes",
                      "self-learning (see /learning) may retune selection and plan settings; rug checks never change"]}


def format_report(rep: dict) -> str:
    lines = ["NORTHSTAR PERFORMANCE REPORT", "=" * 60]
    for ct, c in (rep.get("calls") or {}).items():
        label = "☠️ RUG SCALPS" if ct == "RUG_SCALP" else "🚀 VIP CALLS"
        lines.append(f"\n{label}: {c['calls']} posted | median peak {c['median_peak']}x | hit TP1 {c['tp1_pct']}% | "
                     f"stopped out {c['stopped_pct']}% | time-stopped {c['time_stop_pct']}%")
    for action, g in rep["groups"].items():
        lines.append(f"\n{action}: {g['decisions']} tracked decisions — {g['sample_warning']}")
        for h, d in g["horizons"].items():
            lines.append(f"  {h:>4}: resolved {d['resolved']:>4} (unavailable {d['unavailable']:>3}, pending "
                         f"{d['pending']:>3}) | median observed {d['median_return_observed_pct']}% | "
                         f"median conservative {d['median_return_conservative_pct']}%")
        if "max_observed" in g:
            mo = g["max_observed"]
            lines.append(f"  touched 2x/5x/10x: {mo['reached_2x_pct']}% / {mo['reached_5x_pct']}% / "
                         f"{mo['reached_10x_pct']}% | severe drawdown (<= -50%): "
                         f"{g['severe_drawdown_50pct_share']}%")
    early = rep.get("pre_migration") or {}
    lines.append("\nPre-migration early watches: " + str(early.get("signals", 0)) +
                 f" signals / {early.get('sent', 0)} posted / {early.get('silent', 0)} tracked silently"
                 " | median current multiple " +
                 str(early.get("median_current_multiple")) + "x | median peak " +
                 str(early.get("median_peak_multiple")) + "x | median trough " +
                 str(early.get("median_min_multiple")) + "x | 2x touched " +
                 str(early.get("reached_2x_pct")) + "%")
    lines.append("  " + early.get("warning", ""))
    buckets = ", ".join(f"{b}: n={d['n']} 2x={d['reached_2x_pct']}%" for b, d in rep["score_buckets"].items())
    lines.append("\nScore buckets (all decisions): " + (buckets or "none yet"))
    lines.append("\nSuggestions (never applied automatically):")
    lines += [f"  - {s}" for s in rep["suggestions"]]
    lines.append("\nNotes:")
    lines += [f"  - {n}" for n in rep["notes"]]
    return "\n".join(lines)
