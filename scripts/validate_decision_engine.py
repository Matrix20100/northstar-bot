"""Replay the unified decision engine over Northstar's own tracked history.

    python scripts/validate_decision_engine.py data/northstar.db

Read-only. It answers one question honestly: on the coins this bot actually followed, does the
unified engine rank and filter better than what v2.3.0 did, and is there enough data to say so?

Rules it follows so the numbers mean something:
  * one row per MINT (repeated snapshots and extra pools of the same token are not new coins)
  * the engine only sees the research report as it was stored at decision time
  * the second half of the history is reported separately, so an improvement has to survive on
    coins that were never looked at while the thresholds were being chosen
  * every count is printed, including the ones that make the sample too small to conclude from

Known limitation printed with the results: `decision_tracking` stores the highest and lowest
price seen, not their order, so "reached 2x" and "fell to 0.3x" can both be true of one coin and
this script cannot tell which came first. Any claim that needs ordering (2x BEFORE a drawdown)
needs the minute paths in `shadow_calls`, which only exist for coins called after that feature
shipped.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from northstar.analysis import unified as U          # noqa: E402
from northstar.analysis.verdict import assess        # noqa: E402
from northstar.brain import auc                      # noqa: E402

WIN = 2.0
CRASH = 0.3


class Ro:
    def __init__(self, c):
        self.c = c

    def fetchone(self, sql, p=()):
        r = self.c.execute(sql, tuple(p)).fetchone()
        return dict(r) if r else None

    def fetchall(self, sql, p=()):
        return [dict(r) for r in self.c.execute(sql, tuple(p))]

    def get_candidate(self, mint):
        return self.fetchone("SELECT * FROM candidates WHERE mint=?", (mint,))


def _settings():
    from northstar.config import Settings
    return Settings(discord_token="validation", discord_channel_id=1, discord_vip_channel_id=2)


def load(path: str):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    s = _settings()
    rows = c.execute(
        "SELECT d.mint, d.action old_action, d.score old_score, d.decided_at t, "
        "       d.max_price/d.entry_price peak, d.min_price/d.entry_price trough, d.samples, "
        "       r.report "
        "FROM decision_tracking d JOIN research_snapshots r ON r.id = d.snapshot_id "
        "WHERE d.entry_price > 0 AND d.samples >= 3 ORDER BY d.decided_at").fetchall()
    seen, out = set(), []
    for r in rows:
        if r["mint"] in seen:        # first decision per mint only
            continue
        seen.add(r["mint"])
        try:
            rep = json.loads(r["report"] or "{}")
        except ValueError:
            continue
        full = bool(rep.get("gates")) and bool(rep.get("score"))
        if rep.get("gates") or rep.get("holders"):
            rep.setdefault("verdict", assess(rep, s, None))
        try:
            u = U.evaluate(rep, s)
        except Exception as e:  # noqa: BLE001 - a crash here is a finding, not a stack trace
            print(f"  ! {r['mint'][:8]} could not be evaluated: {type(e).__name__}: {e}")
            continue
        out.append({"mint": r["mint"], "t": r["t"], "peak": r["peak"], "trough": r["trough"],
                    "old_action": r["old_action"], "old_score": r["old_score"],
                    "decision": u["decision"], "score": u["score"], "risk": u["risk"],
                    "completeness": u["completeness"], "full": full,
                    "verdict_risk": -(rep.get("verdict") or {}).get("score", 50),
                    "liquidity": (((rep.get("market") or {}).get("liquidity_usd") or {}).get("value")
                                  if isinstance((rep.get("market") or {}).get("liquidity_usd"), dict)
                                  else (rep.get("market") or {}).get("liquidity_usd")),
                    "band": "80+" if u["score"] >= 80 else "60-79" if u["score"] >= 60
                            else "40-59" if u["score"] >= 40 else "under 40",
                    "vetoes": [v["severity"] for v in u["vetoes"]]})
    return out


def table(rows: list[dict], key: str) -> None:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(str(r[key]), []).append(r)
    print(f"\n  {key:<20}{'coins':>7}{'2x':>8}{'fell to 0.3x':>14}{'median peak':>13}")
    for name, g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        x2 = sum(1 for r in g if r["peak"] >= WIN) / len(g) * 100
        cr = sum(1 for r in g if r["trough"] <= CRASH) / len(g) * 100
        print(f"  {name:<20}{len(g):>7}{x2:>7.1f}%{cr:>13.1f}%{statistics.median(r['peak'] for r in g):>13.2f}")


def ranking(rows: list[dict], label: str, field: str) -> None:
    usable = [r for r in rows if r.get(field) is not None]
    if len(usable) < 30:
        print(f"  {label:<34} not enough coins have this value ({len(usable)})")
        return
    y = [1 if r["peak"] >= WIN else 0 for r in usable]
    a = auc(y, [r[field] for r in usable])
    print(f"  {label:<34} AUC {a:.3f}  on {len(usable)} coins ({sum(y)} reached 2x)")


def decile(rows: list[dict]) -> None:
    usable = sorted((r for r in rows if r["score"] is not None), key=lambda r: -r["score"])
    if len(usable) < 50:
        print("  top-decile precision: sample too small to report")
        return
    k = max(5, len(usable) // 10)
    top = usable[:k]
    base = sum(1 for r in usable if r["peak"] >= WIN) / len(usable) * 100
    hit = sum(1 for r in top if r["peak"] >= WIN) / k * 100
    print(f"  top {k} by score reached 2x {hit:.1f}% of the time vs {base:.1f}% for all "
          f"{len(usable)} coins ({hit / base:.2f}x the base rate)" if base else "")


def main(path: str) -> int:
    rows = load(path)
    if not rows:
        print("no tracked coins with a price path in this database")
        return 1
    full = [r for r in rows if r["full"]]
    print(f"\nNORTHSTAR DECISION ENGINE — HISTORICAL REPLAY on {path}")
    print(f"\n  coins followed with a usable price path : {len(rows)} unique mints")
    print(f"  of those, with a COMPLETE research report: {len(full)}")
    print(f"  reached {WIN:g}x at any point             : "
          f"{sum(1 for r in rows if r['peak'] >= WIN)} ({sum(1 for r in rows if r['peak'] >= WIN) / len(rows) * 100:.1f}%)")
    print(f"  fell to {CRASH:g}x at any point             : "
          f"{sum(1 for r in rows if r['trough'] <= CRASH)} "
          f"({sum(1 for r in rows if r['trough'] <= CRASH) / len(rows) * 100:.1f}%)")

    print("\nBY UNIFIED DECISION")
    table(rows, "decision")
    print("\nBY UNIFIED SCORE BAND")
    table(rows, "band")
    print("\nBY RISK CLASS")
    table(rows, "risk")
    print("\nWHAT v2.3.0 DID WITH THE SAME COINS")
    table(rows, "old_action")

    print("\nRANKING POWER (0.50 = coin flip; higher = better ordering of winners)")
    ranking(rows, "unified score, all coins", "score")
    ranking(full, "unified score, complete reports", "score")
    ranking(rows, "v2.3.0 opportunity score", "old_score")
    print("  baselines:")
    ranking(rows, "  liquidity alone", "liquidity")
    ranking(rows, "  rug verdict (inverted risk)", "verdict_risk")
    decile(rows)

    half = len(rows) // 2
    early, late = rows[:half], rows[half:]
    print(f"\nDOES IT HOLD UP LATER? (first {len(early)} coins vs the last {len(late)})")
    ranking(early, "first half", "score")
    ranking(late, "second half (unseen)", "score")

    print("\nVETO LAYER")
    sev = {}
    for r in rows:
        for x in r["vetoes"]:
            sev[x] = sev.get(x, 0) + 1
    for k, v in sorted(sev.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<22}{v:>6} coins")

    print("\nLIMITATIONS")
    print("  * highest and lowest price are stored without their order, so this cannot measure")
    print("    'reached 2x BEFORE a major drawdown' — both columns can be true of one coin")
    print("  * a market-cap multiple is not a realised return: fees, slippage and the exit size")
    print("    the pool can absorb are not in these numbers")
    print("  * the category weights are starting assumptions, not coefficients fitted on this")
    print(f"    data; {len(full)} complete reports is far too few to fit or calibrate anything")
    print("  * these are the coins the bot CHOSE to follow, so the sample is not the market")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "data/northstar.db"))
