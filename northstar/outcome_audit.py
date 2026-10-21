"""False rejections vs rugs called, measured on Northstar's own history.

    python -m northstar.outcome_audit data/northstar.db [--json]
    (or scripts/audit_outcomes.py)

Read-only. Answers, with counts:
  * of the coins Northstar CALLED (standard, rug-scalp, early narrative, manual), how many rugged
    and how many ran;
  * of the coins it REJECTED or left at INSUFFICIENT DATA, how many ran anyway (false
    rejections), broken down by the reason, the failed gate and the rug-check red flag;
  * which gates/flags actually separate rugs from runners on this history, and which do not;
  * what the CURRENT decision engine would have decided on the same stored reports
    (replay), so a change can be judged on coins it was not tuned on.

Outcome labels — the bot's single definition, brain.label_path / brain.label_extremes
--------------
    RUNNER   reached 2x before falling to 0.6x
    RUG      fell to 0.3x before reaching 1.3x
    NEUTRAL  (brain.FLAT) decided, and neither
    MIXED    (brain.AMBIGUOUS) the decisive events happened in an unknown order — never guessed
Coins whose history is INCOMPLETE (nothing decisive and the window not covered) are left out.
With only the high and low stored (decision_tracking, alerts, early_calls): peak >= 2x with
trough > 0.6x is a runner; trough <= 0.3x with peak < 1.3x is a rug; a 2x AND a 0.6x, or a
0.3x after a 1.3x was seen, is MIXED.
A market-cap multiple is not a realised return; fees and slippage are not modelled here.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from typing import Any, Iterable, Optional

from . import brain as B

# One label definition for the whole bot (brain.label_path / brain.label_extremes).
RUN_X, STOP_X, RUG_X, ESCAPE_X = B.WIN_X, B.LOSS_X, B.RUG_X, B.ESCAPE_X
RUNNER, RUG, NEUTRAL, MIXED, INCOMPLETE = B.RUNNER, B.RUG, B.FLAT, B.AMBIGUOUS, B.INCOMPLETE
REJECTED = ("REJECT", "INSUFFICIENT_DATA")
MIN_GROUP = 8          # below this a rate is printed but flagged as too few to act on


# ----------------------------------------------------------------------------- labels
def label_path(path: list) -> Optional[str]:
    """Ordered label (brain.label_path). INCOMPLETE paths are reported as None: no label."""
    lab = B.label_path(path)
    return None if lab == INCOMPLETE else lab


def label_extremes(peak: Optional[float], trough: Optional[float], complete: bool = True) -> Optional[str]:
    """High/low-only label (brain.label_extremes). INCOMPLETE is reported as None: no label."""
    lab = B.label_extremes(peak, trough, complete)
    return None if lab == INCOMPLETE else lab


# ----------------------------------------------------------------------------- reasons
def reason_family(reasons: Iterable[str]) -> str:
    """One stable bucket per rejection cause, so 41.2 and 39.0 land in the same group."""
    text = " | ".join(str(r) for r in reasons or []).lower()
    rules = [
        ("prefilter: pool age", "prefilter: pool too old"),
        ("prefilter: liquidity", "prefilter: liquidity"),
        ("prefilter: only", "prefilter: activity"),
        ("source_consistency", "provider price disagreement"),
        ("rugcheck_danger", "RugCheck danger"),
        ("rugcheck_rugged", "RugCheck rugged"),
        ("mint_authority", "mint authority"),
        ("freeze_authority", "freeze authority"),
        ("token_extensions", "Token-2022 extension"),
        ("lp_lock", "LP lock"),
        ("liquidity_removal", "liquidity removal"),
        ("gate failed: liquidity", "liquidity floor/ratio"),
        ("holder_concentration", "holder concentration"),
        ("holder concentration", "holder concentration"),
        ("creator_holdings", "creator holdings"),
        ("creator_dumping", "creator dumping"),
        ("bundled_supply", "bundled supply"),
        ("launch_bundle", "launch bundle"),
        ("cannot sell", "no sell route"),
        ("price collapsed", "price collapse"),
        ("price impact", "exit cost"),
        ("narrative evidence", "narrative evidence"),
        ("extended", "price extended"),
        ("volume fading", "volume fading"),
        ("weak buying", "weak buying"),
        ("not verified on-chain", "unverified trade sample"),
        ("wash trading", "wash trading"),
        ("pre-migration activity thresholds", "curve activity thresholds"),
        ("buy flow", "curve buy flow"),
        ("unique buyers", "curve buyers"),
        ("market cap already", "curve already extended"),
        ("not sol-backed", "curve not SOL-backed"),
        ("trustworthy decoded swap", "curve trades unverified"),
        ("verified mint", "mint unverified"),
        ("market data unavailable", "provider failure"),
        ("unresolved critical check", "critical check unresolved"),
        ("critical check unresolved", "critical check unresolved"),
        ("below the", "score below watch line"),
        ("opportunity too weak", "score below watch line"),
        ("score ", "score"),
    ]
    for needle, fam in rules:
        if needle in text:
            return fam
    return "other" if text else "no reason recorded"


# ----------------------------------------------------------------------------- loading
def _connect(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _has(c, table: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _cols(c, table: str) -> set:
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


def _json(x, default):
    try:
        v = json.loads(x) if isinstance(x, str) else x
        return v if v is not None else default
    except (TypeError, ValueError):
        return default


def _snapshot_near(c, mint: str, ts: float, window: float = 900.0) -> Optional[dict]:
    r = c.execute("SELECT id, ts, action, score, report FROM research_snapshots WHERE mint=? AND ts<=? AND ts>=? "
                  "ORDER BY ts DESC LIMIT 1", (mint, ts + 60, ts - window)).fetchone()
    return dict(r) if r else None


def load_observations(c) -> list[dict]:
    """One row per (mint, decision kind): what Northstar decided and what happened next."""
    obs: list[dict] = []
    seen: set = set()

    # 1. ordered paths: every post-migration decision
    if _has(c, "shadow_calls"):
        for r in c.execute("SELECT * FROM shadow_calls WHERE evaluated=1 AND path IS NOT NULL ORDER BY ts"):
            r = dict(r)
            path = _json(r.get("path"), [])
            lab = label_path(path)
            if lab is None:
                continue
            kind = "CALL" if r.get("called") else r.get("action")
            key = (r["mint"], kind)
            if key in seen:
                continue
            seen.add(key)
            snap = _snapshot_near(c, r["mint"], r["ts"])
            rep = _json((snap or {}).get("report"), {})
            obs.append({"mint": r["mint"], "ts": r["ts"], "source": "shadow (ordered path)",
                        "stage": "POOL", "action": r.get("action"), "called": bool(r.get("called")),
                        "call_type": r.get("call_type") or ("STANDARD" if r.get("called") else None),
                        "label": lab, "ordered": True,
                        "peak": r.get("peak_x"), "trough": r.get("trough_x"),
                        "features": _json(r.get("features"), {}), "report": rep})

    # 2. unordered extremes for decisions with no shadow path (incl. pre-migration snapshots)
    if _has(c, "decision_tracking"):
        q = ("SELECT d.mint, d.action, d.decided_at ts, d.max_price/d.entry_price peak, "
             "d.min_price/d.entry_price trough, d.entry_price, d.max_price, d.min_price, d.samples, d.done, "
             "r.report FROM decision_tracking d "
             "JOIN research_snapshots r ON r.id=d.snapshot_id WHERE d.entry_price>0 AND d.samples>=3 "
             "ORDER BY d.decided_at")
        for r in c.execute(q):
            r = dict(r)
            kind = r["action"]
            if (r["mint"], kind) in seen or (r["mint"], "CALL") in seen:
                continue
            lab = B.tracking_label(r)
            if lab in (None, INCOMPLETE):
                continue
            seen.add((r["mint"], kind))
            rep = _json(r.get("report"), {})
            obs.append({"mint": r["mint"], "ts": r["ts"], "source": "decision_tracking (no order)",
                        "stage": "CURVE" if rep.get("lifecycle") == "PRE_MIGRATION_BONDING_CURVE" else "POOL",
                        "action": kind, "called": False, "call_type": None, "label": lab, "ordered": False,
                        "peak": r["peak"], "trough": r["trough"], "features": {}, "report": rep})

    # 3. posted calls: alerts (standard / scalp / manual)
    if _has(c, "alerts"):
        ct = "call_type" if "call_type" in _cols(c, "alerts") else "'STANDARD' AS call_type"
        for r in c.execute(f"SELECT mint, created_at ts, max_multiple, min_multiple, {ct}, snapshot_id, closed_at "
                           "FROM alerts WHERE status IN ('SENT','DRY_RUN') ORDER BY created_at"):
            r = dict(r)
            if (r["mint"], "CALL") in seen:
                # the ordered shadow already covers it; keep its posted call type
                for o in obs:
                    if o["mint"] == r["mint"] and o["called"]:
                        o["call_type"] = r["call_type"] or o["call_type"]
                continue
            lab = label_extremes(r["max_multiple"], r["min_multiple"], complete=bool(r.get("closed_at")))
            if lab is None:
                continue
            seen.add((r["mint"], "CALL"))
            snap = c.execute("SELECT report FROM research_snapshots WHERE id=?", (r["snapshot_id"],)).fetchone() \
                if r.get("snapshot_id") else None
            obs.append({"mint": r["mint"], "ts": r["ts"], "source": "alerts (no order)", "stage": "POOL",
                        "action": "AUTO_POST", "called": True, "call_type": r["call_type"] or "STANDARD",
                        "label": lab, "ordered": False, "peak": r["max_multiple"], "trough": r["min_multiple"],
                        "features": {}, "report": _json(snap["report"] if snap else None, {})})

    # 4. early narrative calls (bonding curve)
    if _has(c, "early_calls"):
        for r in c.execute("SELECT mint, created_at ts, entry_mcap, peak_mcap, low_mcap, report, closed_at FROM early_calls "
                           "WHERE status IN ('SENT','DRY_RUN','CLOSED','MIGRATED','OPEN') ORDER BY created_at"):
            r = dict(r)
            if not r["entry_mcap"]:
                continue
            peak = (r["peak_mcap"] or r["entry_mcap"]) / r["entry_mcap"]
            trough = (r["low_mcap"] or r["entry_mcap"]) / r["entry_mcap"]
            lab = label_extremes(peak, trough, complete=bool(r.get("closed_at")))
            if lab is None:
                continue
            obs.append({"mint": r["mint"], "ts": r["ts"], "source": "early_calls (no order)", "stage": "CURVE",
                        "action": "AUTO_POST", "called": True, "call_type": "EARLY_NARRATIVE", "label": lab,
                        "ordered": False, "peak": peak, "trough": trough, "features": {},
                        "report": _json(r.get("report"), {})})
    return obs


# ----------------------------------------------------------------------------- stats
def _rates(rows: list[dict]) -> dict:
    n = len(rows)
    cnt = {k: sum(1 for r in rows if r["label"] == k) for k in (RUNNER, RUG, MIXED, NEUTRAL)}
    peaks = [r["peak"] for r in rows if isinstance(r.get("peak"), (int, float))]
    return {"n": n, "runner": cnt[RUNNER], "rug": cnt[RUG], "mixed": cnt[MIXED], "neutral": cnt[NEUTRAL],
            "runner_pct": round(cnt[RUNNER] / n * 100, 1) if n else None,
            "rug_pct": round(cnt[RUG] / n * 100, 1) if n else None,
            "mixed_pct": round(cnt[MIXED] / n * 100, 1) if n else None,
            "median_peak": round(statistics.median(peaks), 2) if peaks else None,
            "small_sample": n < MIN_GROUP}


def _group(rows: list[dict], key) -> dict:
    out: dict[str, list] = {}
    for r in rows:
        for k in (key(r) if callable(key) else [r.get(key)]) or ["(none)"]:
            out.setdefault(str(k), []).append(r)
    return {k: _rates(v) for k, v in sorted(out.items(), key=lambda kv: -len(kv[1]))}


def _reasons_of(r: dict) -> list:
    rep = r.get("report") or {}
    return list(rep.get("reasons") or (rep.get("decision") or {}).get("reasons") or [])


def _failed_gates(r: dict) -> list:
    rep = r.get("report") or {}
    gates = [g.get("name") for g in rep.get("gates") or [] if isinstance(g, dict) and g.get("status") == "FAIL"]
    return gates or list((r.get("features") or {}).get("failed") or [])


def _red_flags(r: dict) -> list:
    v = (r.get("report") or {}).get("verdict") or {}
    return [x.get("key") for x in v.get("red") or [] if isinstance(x, dict) and x.get("key")]


def discrimination(rows: list[dict], base: dict, keyfn) -> list[dict]:
    """For each gate/flag: does it fire more on rugs than on runners, relative to the base rates?

    lift_rug    = rug rate among coins with the signal / overall rug rate     (>1 = catches rugs)
    lift_runner = runner rate among coins with it    / overall runner rate   (>1 = blocks runners)
    A signal with lift_runner >= 1 and lift_rug <= 1 rejects good coins without catching rugs.
    """
    out = []
    for name, st in _group(rows, keyfn).items():
        if name == "(none)":
            continue
        lr = (st["rug_pct"] / base["rug_pct"]) if base.get("rug_pct") else None
        lw = (st["runner_pct"] / base["runner_pct"]) if base.get("runner_pct") else None
        if st["small_sample"] or lr is None or lw is None:
            verdict = "too few to judge"
        elif lw >= 1 and lr <= 1:
            verdict = "blocks runners, does not catch rugs"
        elif lr >= 1.25 and lw < 1:
            verdict = "catches rugs"
        else:
            verdict = "weak signal"
        out.append({"signal": name, **st, "lift_rug": None if lr is None else round(lr, 2),
                    "lift_runner": None if lw is None else round(lw, 2), "verdict": verdict})
    return out


# ----------------------------------------------------------------------------- replay
def replay(rows: list[dict], s: Any = None) -> dict:
    """Re-decide stored post-migration reports with the CURRENT unified engine.

    Gates produced by older code are re-graded only where the meaning of a gate changed and the
    stored report says enough to do it honestly (provider price disagreement is no longer a hard
    fail). Reports that stopped at a gate before the analysis ran cannot be replayed in full;
    they are counted separately.
    """
    from .analysis import unified as U
    from .analysis.verdict import assess

    if s is None:
        from .config import Settings
        s = Settings(_env_file=None, discord_token="audit", discord_channel_id=1, discord_vip_channel_id=2)
    out_rows, partial = [], 0
    for r in rows:
        rep = r.get("report") or {}
        if r["stage"] != "POOL" or not rep.get("gates"):
            continue
        rep = json.loads(json.dumps(rep, default=str))
        for g in rep["gates"]:
            if g.get("name") == "source_consistency" and g.get("status") == "FAIL":
                g["status"], g["critical"] = "UNKNOWN", False
                rep.setdefault("flags", []).append({"code": "PRICE_DISCREPANCY", "message": g.get("reason")})
        if not rep.get("holders") and not rep.get("trades"):
            partial += 1
        rep.pop("unified", None)
        try:
            rep["verdict"] = assess(rep, s, None)
            u = U.evaluate(rep, s)
        except Exception:  # noqa: BLE001 - a report that cannot be replayed is skipped, not fatal
            continue
        new = U.LEGACY_ACTION.get(u["decision"], "REJECT")
        out_rows.append({**r, "new_action": new, "new_decision": u["decision"],
                         "new_class": u.get("reason_class")})

    def tally(field: str) -> dict:
        called = [x for x in out_rows if x[field] == "AUTO_POST"]
        rejected = [x for x in out_rows if x[field] in REJECTED]
        watched = [x for x in out_rows if x[field] == "WATCHLIST"]
        return {"called": _rates(called), "watched": _rates(watched), "rejected": _rates(rejected),
                "rugs_called": sum(1 for x in called if x["label"] == RUG),
                "runners_rejected": sum(1 for x in rejected if x["label"] == RUNNER)}

    for x in out_rows:
        x["old_action"] = "AUTO_POST" if x["called"] else x["action"]
    moves: dict[str, dict] = {}
    for x in out_rows:
        k = f"{x['old_action']} -> {x['new_action']}"
        moves.setdefault(k, []).append(x)
    return {"replayed": len(out_rows), "stopped_before_analysis": partial,
            "old": tally("old_action"), "new": tally("new_action"),
            "moves": {k: _rates(v) for k, v in sorted(moves.items(), key=lambda kv: -len(kv[1]))},
            "new_reject_classes": _group([x for x in out_rows if x["new_action"] in REJECTED], "new_class")}


def early_gate_replay(rows: list[dict]) -> dict:
    """Which posted early calls the v2.5 early-call gate would have held back, by outcome."""
    from .early_calls import safety_unknowns, serious_red_flags
    calls = [r for r in rows if r["call_type"] == "EARLY_NARRATIVE" and r["label"]]
    held = [r for r in calls if safety_unknowns(r.get("report") or {})
            or serious_red_flags(r.get("report") or {})]
    kept = [r for r in calls if r not in held]
    return {"posted": _rates(calls), "would_hold": _rates(held), "would_keep": _rates(kept)}


# ----------------------------------------------------------------------------- report
def audit(path: str, s: Any = None) -> dict:
    c = _connect(path)
    try:
        rows = load_observations(c)
    finally:
        c.close()
    calls = [r for r in rows if r["called"]]
    decided = [r for r in rows if not r["called"]]
    rejected = [r for r in decided if r["action"] in REJECTED]
    base = _rates(decided) if decided else {"rug_pct": None, "runner_pct": None}
    res = {
        "observations": len(rows),
        "ordered": sum(1 for r in rows if r["ordered"]),
        "base_rates_all_decisions": base,
        "calls_by_type": _group(calls, "call_type"),
        "calls_all": _rates(calls),
        "decisions_by_action": _group(decided, "action"),
        "rejections_by_reason": _group(rejected, lambda r: [reason_family(_reasons_of(r))]),
        "rejections_by_stage": _group(rejected, "stage"),
        "gate_discrimination": discrimination(decided, base, lambda r: _failed_gates(r) or ["(none)"]),
        "flag_discrimination": discrimination(decided, base, lambda r: _red_flags(r) or ["(none)"]),
        "false_rejections": sum(1 for r in rejected if r["label"] == RUNNER),
        "rugs_called": sum(1 for r in calls if r["label"] == RUG),
    }
    res["replay"] = replay(rows, s)
    try:
        res["early_gate_replay"] = early_gate_replay(rows)
    except Exception as e:  # noqa: BLE001
        res["early_gate_replay"] = {"error": f"{type(e).__name__}: {e}"}
    return res


def _line(name: str, st: dict, width: int = 34) -> str:
    if not st or not st.get("n"):
        return f"  {name:<{width}}{0:>6}"
    flag = "  (few)" if st.get("small_sample") else ""
    return (f"  {name[:width]:<{width}}{st['n']:>6}{st['runner_pct']:>8.1f}%{st['rug_pct']:>8.1f}%"
            f"{st['mixed_pct']:>8.1f}%{(st['median_peak'] or 0):>9.2f}{flag}")


HEAD = f"  {'':<34}{'coins':>6}{'runner':>9}{'rug':>9}{'mixed':>9}{'med.peak':>9}"


def format_audit(a: dict) -> str:
    L = ["NORTHSTAR OUTCOME AUDIT — false rejections vs rugs called",
         f"  observations: {a['observations']} ({a['ordered']} with an ordered price path)",
         f"  runner = {RUN_X:g}x before {STOP_X:g}x · rug = {RUG_X:g}x before {ESCAPE_X:g}x · "
         "mixed = both seen, order unknown", ""]
    L += ["CALLS (what members were sent)", HEAD, _line("all calls", a["calls_all"])]
    L += [_line(k, v) for k, v in a["calls_by_type"].items()]
    L += [f"  -> rugs called: {a['rugs_called']}", "", "ALL DECISIONS (base rates)", HEAD,
          _line("every non-called decision", a["base_rates_all_decisions"])]
    L += [_line(k, v) for k, v in a["decisions_by_action"].items()]
    L += [f"  -> false rejections (REJECT/INSUFFICIENT that ran {RUN_X:g}x): {a['false_rejections']}", "",
          "REJECTIONS BY REASON", HEAD]
    L += [_line(k, v) for k, v in a["rejections_by_reason"].items()]
    for title, key in (("FAILED GATES — do they separate rugs from runners?", "gate_discrimination"),
                       ("RUG-CHECK RED FLAGS", "flag_discrimination")):
        L += ["", title, f"  {'signal':<30}{'coins':>6}{'runner':>9}{'rug':>9}{'rug lift':>10}{'run lift':>10}  verdict"]
        for d in sorted(a[key], key=lambda d: -d["n"])[:25]:
            L.append(f"  {d['signal'][:30]:<30}{d['n']:>6}{(d['runner_pct'] or 0):>8.1f}%{(d['rug_pct'] or 0):>8.1f}%"
                     f"{(d['lift_rug'] or 0):>10.2f}{(d['lift_runner'] or 0):>10.2f}  {d['verdict']}")
    rp = a["replay"]
    L += ["", f"REPLAY WITH THE CURRENT ENGINE ({rp['replayed']} stored pool reports; "
              f"{rp['stopped_before_analysis']} stopped at a gate before analysis, replayed as stored)",
          f"  {'':<16}{'calls':>7}{'rugs called':>13}{'rejected':>10}{'runners rejected':>18}"]
    for k in ("old", "new"):
        t = rp[k]
        L.append(f"  {('before' if k == 'old' else 'current code'):<16}{t['called']['n']:>7}{t['rugs_called']:>13}"
                 f"{t['rejected']['n']:>10}{t['runners_rejected']:>18}")
    L += ["  moves:", HEAD] + [_line(k, v) for k, v in list(rp["moves"].items())[:12]]
    eg = a.get("early_gate_replay") or {}
    if "posted" in eg:
        L += ["", "EARLY NARRATIVE CALLS — v2.5 gate (required checks ran, no serious red flag)", HEAD,
              _line("posted", eg["posted"]), _line("would be held back", eg["would_hold"]),
              _line("would still be posted", eg["would_keep"])]
    L += ["", "LIMITS: mixed outcomes are never counted as runners or rugs; groups marked (few) are too",
          "small to act on; these are coins Northstar chose to follow, not the whole market."]
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    path = argv[0] if argv else "data/northstar.db"
    a = audit(path)
    print(json.dumps(a, indent=2, default=str) if as_json else format_audit(a))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
