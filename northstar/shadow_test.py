"""Shadow testing: what a PROPOSED decision policy would have done, next to what Northstar did.

Nothing here changes a live decision, a callout, a stored snapshot or a setting. For every coin
the engine decides (post-migration coins with a unified result), one row is written to the
`shadow_tests` table with:

  * the LIVE decision: decision, action, score, risk, confidence, reason class, reasons;
  * the PROPOSED decision for the same coin, re-derived by the same scoring code
    (analysis/unified.py) from the SAME report, the same learning-brain tilt and the same AI read,
    with the proposal's rules and setting overrides applied;
  * both score breakdowns (points per category, suspicion penalty, brain tilt, completeness);
  * which proposal rules actually changed something for this coin.

Outcomes are attached later from price data Northstar already records — no extra API calls:

  1. the coin's learning shadow (`shadow_calls`, linked by id at record time): a minute price
     path that starts at the decision -> the shared runner/rug label (brain.label_path);
  2. otherwise the 24h decision tracking of the SAME research snapshot (`decision_tracking`)
     -> brain.tracking_label (high/low only, so ambiguous orders stay AMBIGUOUS).

No future data
--------------
* The proposal only reads the report as it was at decision time; the row is written in the same
  research pass and its decision columns are never updated afterwards.
* Outcome sources must start at the decision: the shadow path is linked by id (recorded in the
  same pass, candles before its timestamp are dropped by learning.path_from) and the tracking row
  must belong to the same snapshot. A row is only labelled after its whole window has passed;
  INCOMPLETE paths are retried until the 24h tracking finishes.

No duplicates
-------------
* at most one row per (research snapshot, policy) — enforced by a unique index;
* a re-research of the same coin within `shadow_test_dedupe_minutes` is skipped unless the live
  or the proposed decision changed, so a coin researched 40 times an hour is not counted 40 times.

Settings (all optional):
  NORTHSTAR_SHADOW_TEST_ENABLED=true          record shadow tests (record-only, no live effect)
  NORTHSTAR_SHADOW_TEST_RULES=lp_unverified_warn,holder_gate_warn
  NORTHSTAR_SHADOW_TEST_OVERRIDES={"u_watch_min": 45}   unified-engine (u_*) settings to try
  NORTHSTAR_SHADOW_TEST_DEDUPE_MINUTES=30
  NORTHSTAR_SHADOW_TEST_DEEP_RESEARCH=true    finish the analysis of coins whose ONLY failed gates
                                              the LP rule would soften (their live decision stays
                                              REJECT; costs the normal holder/trade lookups)

Report: `python main.py shadow [--days 7] [--json]`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Optional

from . import brain as B

log = logging.getLogger("northstar.shadow_test")

POLICY_VERSION = "p1"
EVAL_AFTER = 4 * 3600             # the learning path covers 4h after the decision (learning.PATH_MINUTES)
GIVE_UP_AFTER = 26 * 3600         # 24h tracking + slack: after this a row without an outcome stays without
KEEP_DAYS = 45.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mint TEXT NOT NULL,
    snapshot_id INTEGER,
    shadow_call_id INTEGER,
    policy TEXT NOT NULL,
    entry_price REAL,
    entry_mcap REAL,
    liquidity REAL,
    live_decision TEXT NOT NULL,
    live_action TEXT NOT NULL,
    live_score REAL,
    live_risk TEXT,
    live_confidence TEXT,
    live_class TEXT,
    live_reasons TEXT,
    prop_decision TEXT NOT NULL,
    prop_action TEXT NOT NULL,
    prop_score REAL,
    prop_risk TEXT,
    prop_confidence TEXT,
    prop_class TEXT,
    prop_reasons TEXT,
    changed INTEGER NOT NULL DEFAULT 0,
    rules_fired TEXT,
    components TEXT,
    outcome_label TEXT,
    outcome_src TEXT,
    peak_x REAL,
    trough_x REAL,
    outcome_checked_at REAL,
    after_2x_low REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_shadow_tests_snap ON shadow_tests(snapshot_id, policy);
CREATE INDEX IF NOT EXISTS ix_shadow_tests_mint ON shadow_tests(mint, policy, ts);
CREATE INDEX IF NOT EXISTS ix_shadow_tests_due ON shadow_tests(outcome_label, ts);
"""

# ------------------------------------------------------------------------------- the proposal
LP_DANGER_WORDS = ("lp unlocked", "lp providers", "liquidity unlocked")

RULES = {
    "lp_unverified_warn": (
        "PumpSwap pool, LP not verified on-chain: a provider-reported unlocked LP (RugCheck danger "
        "or lp_lock FAIL) is a serious warning that caps at WATCH, not a hard veto"),
    "holder_gate_warn": (
        "holder concentration over the bar is a serious warning that caps at WATCH, not a rejection"),
    # v2.5.5 call profiles (operator, 22 Oct: "test it first, no live change"). They only ever
    # make a call stricter: a STRONG call that misses the profile becomes WATCH.
    "deep_liquidity": "a call needs at least $100,000 of liquidity",
    "pool_age_1h": "a call needs a pool that has survived its first hour",
    # from 318 ordered 4-hour price paths (21 Oct backup, see docs/DECISION_ENGINE.md): pools
    # under 5 minutes old were the worst group (58% rug or pump-then-rug); $50K+ liquidity
    # roughly halved rugs while keeping most 2x runs, unlike $100K+ (2x runs fell to 9%)
    "pool_age_5m": "a call needs a pool that is at least 5 minutes old",
    "liquidity_50k": "a call needs at least $50,000 of liquidity",
}
PROFILE_RULES = {"deep_liquidity", "pool_age_1h", "pool_age_5m", "liquidity_50k"}
PROFILE_LIQUIDITY = {"deep_liquidity": 100_000.0, "liquidity_50k": 50_000.0}
PROFILE_POOL_AGE_H = {"pool_age_1h": 1.0, "pool_age_5m": 5 / 60}
DEEP_LIQUIDITY_USD = PROFILE_LIQUIDITY["deep_liquidity"]
MIN_POOL_AGE_HOURS = PROFILE_POOL_AGE_H["pool_age_1h"]
# policies are separated by ";" (one row per policy per decision), rules inside one by ","
DEFAULT_RULES = "lp_unverified_warn,holder_gate_warn;pool_age_5m;liquidity_50k,pool_age_5m;deep_liquidity"


def init_schema(db) -> None:
    with db._lock:
        db._conn.executescript(SCHEMA)
        have = {r[1] for r in db._conn.execute("PRAGMA table_info(shadow_tests)")}
        if "after_2x_low" not in have:        # v2.5.5: lowest price after the first 2x (pump-then-rug)
            db._conn.execute("ALTER TABLE shadow_tests ADD COLUMN after_2x_low REAL")


class _Overlay:
    """The live settings with a few unified-engine values replaced. Read-only."""

    def __init__(self, base: Any, overrides: dict):
        self._base, self._over = base, dict(overrides or {})

    def __getattr__(self, name: str) -> Any:
        if name in self._over:
            return self._over[name]
        return getattr(self._base, name)


def parse_policies(text: Optional[str]) -> list[list[str]]:
    """NORTHSTAR_SHADOW_TEST_RULES -> one rule list per policy (";" separates policies)."""
    out: list[list[str]] = []
    for part in str(text if text is not None else DEFAULT_RULES).split(";"):
        rules = parse_rules(part) if part.strip() else []
        if rules and rules not in out:
            out.append(rules)
    return out


def parse_rules(text: Optional[str]) -> list[str]:
    """One policy's rules (comma-separated). A ';'-separated setting is read as all its policies' rules."""
    out = []
    for r in str(text if text is not None else DEFAULT_RULES).replace(";", ",").split(","):
        r = r.strip()
        if not r:
            continue
        if r not in RULES:
            log.warning("shadow test: unknown rule %r ignored (known: %s)", r, ", ".join(RULES))
            continue
        if r not in out:
            out.append(r)
    return sorted(out)


def parse_overrides(text: Optional[str], s: Any = None) -> dict:
    """Only unified-engine settings (u_*) that exist and are numbers/booleans can be overridden."""
    if not text:
        return {}
    try:
        raw = json.loads(text)
    except (TypeError, ValueError):
        log.warning("shadow test: NORTHSTAR_SHADOW_TEST_OVERRIDES is not JSON; ignored")
        return {}
    out = {}
    for k, v in (raw.items() if isinstance(raw, dict) else []):
        if not str(k).startswith("u_") or (s is not None and not hasattr(s, k)) \
                or not isinstance(v, (int, float, bool)):
            log.warning("shadow test: override %r ignored (only existing numeric u_* settings)", k)
            continue
        out[str(k)] = v
    return dict(sorted(out.items()))


def policy_name(rules: list[str], overrides: dict) -> str:
    """Stable name: rows from different proposals are never mixed in a report."""
    over = json.dumps(overrides, sort_keys=True) if overrides else ""
    tag = hashlib.sha1(over.encode()).hexdigest()[:6] if over else "-"
    return f"{POLICY_VERSION}:{'+'.join(rules) or 'none'}:{tag}"


def _lp_only_gate(g: dict) -> bool:
    """A failed rugcheck_danger gate whose ONLY dangers are about unlocked LP."""
    names = [n.strip() for n in str(g.get("reason") or "").replace("RugCheck danger:", "").split(",")
             if n.strip()]
    return bool(names) and all(any(w in n.lower() for w in LP_DANGER_WORDS) for n in names)


def _lp_only_danger(report: dict) -> bool:
    g = next((g for g in report.get("gates") or [] if isinstance(g, dict)
              and g.get("name") == "rugcheck_danger" and g.get("status") == "FAIL"), None)
    return bool(g) and _lp_only_gate(g)


def _lp_scope(report: dict) -> bool:
    """The pool type the audit measured: PumpSwap, with the LP NOT read on-chain by Northstar."""
    dex = str((report.get("identity") or {}).get("dex") or (report.get("market") or {}).get("dex") or "")
    return dex.lower() == "pumpswap" and not str(report.get("lp_onchain_check") or "").startswith("verified")


def continue_research(gates: list[dict], report: dict, s: Any) -> bool:
    """Should research go on past failed security gates so the proposal can be judged?

    Live research stops at the first failed security gate, so a coin rejected only for a
    provider-reported unlocked LP never reaches the unified engine and the LP proposal could never
    be compared. When EVERY failed gate is one that rule would soften, research continues; the
    live decision is still REJECT (those gates stay hard vetoes in the live engine).
    """
    try:
        if not getattr(s, "shadow_test_enabled", True) or not getattr(s, "shadow_test_deep_research", True) \
                or not getattr(s, "unified_enabled", True):
            return False
        if not any("lp_unverified_warn" in p for p in parse_policies(getattr(s, "shadow_test_rules", DEFAULT_RULES))):
            return False
        failed = [g for g in gates if isinstance(g, dict) and g.get("status") == "FAIL"]
        if not failed or not _lp_scope(report):
            return False
        return all(g.get("name") == "lp_lock" or (g.get("name") == "rugcheck_danger" and _lp_only_gate(g))
                   for g in failed)
    except Exception:  # noqa: BLE001 - on any doubt, keep the live behaviour
        return False


def _apply_rules(led, report: dict, rules: list[str]) -> list[str]:
    """Change the graded hazards the proposal disagrees with. Returns the rules that fired."""
    from .analysis import unified as U

    fired: list[str] = []
    if "lp_unverified_warn" in rules:
        if _lp_scope(report):
            for v in led.vetoes:
                if v.severity != U.CONFIRMED_CRITICAL:
                    continue
                if v.key == "haz:lp" or (v.key == "gate:rugcheck_danger" and _lp_only_danger(report)):
                    v.severity, v.blocking = U.STRONG_WARNING, False
                    v.text = f"{v.text} (provider-reported, not verified on-chain)"
                    if "lp_unverified_warn" not in fired:
                        fired.append("lp_unverified_warn")
    if "holder_gate_warn" in rules:
        for v in led.vetoes:
            if v.key == "haz:concentration" and v.blocking and v.severity == U.STRONG_WARNING:
                v.blocking = False
                fired.append("holder_gate_warn")
    return fired


def propose(report: dict, s: Any, rules: list[str], overrides: dict) -> Optional[dict]:
    """The proposed unified result for one decided report, or None without a unified result.

    Re-derived from the live result's own ledger (checks + graded hazards), brain tilt, AI read
    and source conflicts — exactly how the engine re-derives after the brain and the AI — so with
    no rules and no overrides it reproduces the live decision.
    """
    from .analysis import unified as U
    from .final import has_unified

    if not has_unified(report):
        return None
    u = report["unified"]
    led = U._reload(u)
    fired = _apply_rules(led, report, rules)
    model = u.get("model") if isinstance(u.get("model"), dict) and u["model"].get("probability") is not None \
        else None
    ps = _Overlay(s, overrides) if overrides else s
    pu = U._assemble(led, report, ps, model=model, ai=u.get("ai"), conflicts=u.get("conflicts"))
    fired += _apply_profile(pu, report, rules)
    pu["shadow_rules_fired"] = fired
    return pu


def profile_misses(report: dict, rules: list[str]) -> list[tuple[str, str]]:
    """Which call-profile rules this coin misses (whatever its decision). [] = it fits the profile."""
    from .analysis import unified as U

    misses: list[tuple[str, str]] = []
    liq = U._num((report.get("market") or {}).get("liquidity_usd"))
    age = U._num((report.get("security") or {}).get("pool_age_hours"))
    for rule in sorted(r for r in rules if r in PROFILE_LIQUIDITY):
        need = PROFILE_LIQUIDITY[rule]
        if liq is None or liq < need:       # unknown liquidity never counts as enough
            misses.append((rule, f"liquidity ${liq or 0:,.0f} is under the ${need:,.0f} a call needs in this profile"))
    for rule in sorted(r for r in rules if r in PROFILE_POOL_AGE_H):
        need = PROFILE_POOL_AGE_H[rule]
        if age is None or age < need:
            misses.append((rule, ("the pool is too new" if age is not None else "the pool age is unknown")
                           + (f" ({age * 60:.0f} min old, needs {need * 60:.0f})" if age is not None else "")))
    return misses


def _apply_profile(pu: dict, report: dict, rules: list[str]) -> list[str]:
    """Call-profile rules: a STRONG call that misses the profile is only a WATCH. The pass/miss is
    recorded for every decision, so the report can compare the coins a profile admits with the
    ones it excludes, not only the handful of calls."""
    from .analysis import unified as U

    if not any(r in PROFILE_RULES for r in rules):
        return []
    misses = profile_misses(report, rules)
    pu["shadow_profile"] = {"fits": not misses, "misses": [k for k, _ in misses]}
    if pu.get("decision") != U.STRONG or not misses:
        return []
    pu["decision"], pu["reason_class"] = U.WATCH, "CALL_PROFILE"
    pu["why"] = [f"call profile: {t}" for _, t in misses] + list(pu.get("why") or [])
    return [k for k, _ in misses]


def _components(u: dict) -> dict:
    cats = u.get("categories") or {}
    return {"points": {k: (c or {}).get("points") for k, c in cats.items()},
            "base": u.get("base_score"), "penalty": u.get("penalty"),
            "tilt": (u.get("model") or {}).get("tilt"), "completeness": u.get("completeness"),
            "score_ex_narrative": u.get("score_ex_narrative"),
            "profile": u.get("shadow_profile")}


# ------------------------------------------------------------------------------- recording
def record(db, out, s: Any, shadow_call_id: Optional[int] = None,
           now: Optional[float] = None) -> Optional[int]:
    """Record the live decision next to every configured proposal (one row per policy).

    Returns the first new row id, or None when nothing was recorded (disabled, no unified result,
    duplicates). Never raises: shadow testing must never break research.
    """
    if not getattr(s, "shadow_test_enabled", True):
        return None
    overrides = parse_overrides(getattr(s, "shadow_test_overrides", ""), s)
    first = None
    for rules in parse_policies(getattr(s, "shadow_test_rules", DEFAULT_RULES)):
        rid = _record_one(db, out, s, rules, overrides, shadow_call_id, now)
        first = first or rid
    return first


def _record_one(db, out, s: Any, rules: list[str], overrides: dict, shadow_call_id: Optional[int],
                now: Optional[float]) -> Optional[int]:
    try:
        report = out.report
        live = report.get("final") if isinstance(report.get("final"), dict) else {}
        if not live.get("decision"):
            return None
        policy = policy_name(rules, overrides)
        pu = propose(report, s, rules, overrides)
        if pu is None:
            return None
        from .final import final_view
        pv = final_view(pu, report.get("policy_blockers"), report.get("final_notes"))
        now = now or time.time()
        init_schema(db)
        if out.snapshot_id is not None and db.fetchone(
                "SELECT 1 FROM shadow_tests WHERE snapshot_id=? AND policy=?", (out.snapshot_id, policy)):
            return None
        window = float(getattr(s, "shadow_test_dedupe_minutes", 30.0) or 0.0) * 60
        last = db.fetchone("SELECT live_decision, prop_decision, live_action, prop_action FROM shadow_tests "
                           "WHERE mint=? AND policy=? AND ts>? ORDER BY ts DESC LIMIT 1",
                           (out.mint, policy, now - window)) if window > 0 else None
        live_action = out.action.value
        if last and (last["live_decision"], last["prop_decision"], last["live_action"], last["prop_action"]) \
                == (live["decision"], pv["decision"], live_action, pv["action"].value):
            return None
        pair = out.pair
        sql = (
            "INSERT OR IGNORE INTO shadow_tests(ts, mint, snapshot_id, shadow_call_id, policy, entry_price, "
            "entry_mcap, liquidity, live_decision, live_action, live_score, live_risk, live_confidence, "
            "live_class, live_reasons, prop_decision, prop_action, prop_score, prop_risk, prop_confidence, "
            "prop_class, prop_reasons, changed, rules_fired, components) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
        params = (now, out.mint, out.snapshot_id, shadow_call_id, policy,
                  getattr(pair, "price_usd", None), getattr(pair, "market_cap", None),
                  getattr(pair, "liquidity_usd", None),
                  live["decision"], live_action, out.score, live.get("risk_class"), live.get("confidence"),
                  live.get("reason_class"), json.dumps(list(out.reasons or [])[:8], default=str),
                  pv["decision"], pv["action"].value, pv["score"], pv["risk_class"], pv["confidence"].value,
                  pv["reason_class"], json.dumps(pv["reasons"][:8] + pv["blockers"][:4], default=str),
                  int(pv["decision"] != live["decision"] or pv["action"].value != live_action),
                  json.dumps(pu.get("shadow_rules_fired") or []),
                  json.dumps({"live": _components(report["unified"]), "proposed": _components(pu)}, default=str))
        with db._lock:
            cur = db._conn.execute(sql, params)
            return cur.lastrowid if cur.rowcount else None
    except Exception:  # noqa: BLE001 - shadow testing must never break research
        log.warning("shadow test record failed for %s", getattr(out, "mint", "?"), exc_info=True)
        return None


# ------------------------------------------------------------------------------- outcomes
def _from_path(db, row: dict) -> Optional[tuple]:
    if not row["shadow_call_id"]:
        return None
    sc = db.fetchone("SELECT ts, entry_price, evaluated, path FROM shadow_calls WHERE id=? AND mint=?",
                     (row["shadow_call_id"], row["mint"]))
    if not sc or sc["evaluated"] != 1 or not sc["path"]:
        return None
    if sc["ts"] < row["ts"] - 60:              # a path that began before this decision: never used
        return None
    try:
        path = json.loads(sc["path"])
    except (TypeError, ValueError):
        return None
    lab = B.label_path(path)
    if lab in (None, B.INCOMPLETE):
        return None
    return (lab, "path", max(p[1] for p in path), min(p[2] for p in path), after_2x_low(path))


def after_2x_low(path: list) -> Optional[float]:
    """Lowest low after the first candle that reached 2x (None if it never did). A runner whose
    price then fell to 0.3x of entry or lower is a pump-then-rug, not a clean run."""
    t2 = next((p[0] for p in path if p[1] >= 2.0), None)
    if t2 is None:
        return None
    after = [p[2] for p in path if p[0] > t2]
    return min(after) if after else None


def _from_tracking(db, row: dict) -> Optional[tuple]:
    if row["snapshot_id"] is None:
        return None
    tr = db.fetchone("SELECT * FROM decision_tracking WHERE snapshot_id=? AND mint=?",
                     (row["snapshot_id"], row["mint"]))
    if not tr or tr["decided_at"] < row["ts"] - 600:     # same research pass only
        return None
    lab = B.tracking_label(tr)
    if lab in (None, B.INCOMPLETE):
        return None
    e = tr["entry_price"]
    return (lab, "tracking", (tr["max_price"] / e) if e and tr["max_price"] else None,
            (tr["min_price"] / e) if e and tr["min_price"] else None, None)   # high/low only: order unknown


def evaluate_due(db, now: Optional[float] = None, limit: int = 500) -> int:
    """Attach outcomes to rows whose window has passed. Uses only stored data. Returns rows labelled."""
    now = now or time.time()
    init_schema(db)
    done = 0
    for row in db.fetchall("SELECT * FROM shadow_tests WHERE outcome_label IS NULL AND outcome_src IS NULL "
                           "AND ts < ? ORDER BY ts LIMIT ?", (now - EVAL_AFTER, limit)):
        res = _from_path(db, row) or _from_tracking(db, row)
        if res:
            lab, src, peak, trough, low_after = res
            db.execute("UPDATE shadow_tests SET outcome_label=?, outcome_src=?, peak_x=?, trough_x=?, "
                       "after_2x_low=?, outcome_checked_at=? WHERE id=?",
                       (lab, src, peak, trough, low_after, now, row["id"]))
            done += 1
        elif row["ts"] < now - GIVE_UP_AFTER:
            db.execute("UPDATE shadow_tests SET outcome_src='none', outcome_checked_at=? WHERE id=?",
                       (now, row["id"]))
        else:
            db.execute("UPDATE shadow_tests SET outcome_checked_at=? WHERE id=?", (now, row["id"]))
    return done


def prune(db, now: Optional[float] = None, keep_days: float = KEEP_DAYS) -> int:
    now = now or time.time()
    init_schema(db)
    with db._lock:
        return db._conn.execute("DELETE FROM shadow_tests WHERE ts < ?", (now - keep_days * 86400,)).rowcount


# ------------------------------------------------------------------------------- report
CALL = "AUTO_POST"


def _tally(rows: list[dict]) -> dict:
    """Outcome counts. A runner is split by what happened after its first 2x (paths only):
    clean, pump-then-rug (0.3x of entry or lower after the 2x), or order unknown (24 h tracking)."""
    t = {"n": len(rows), "labelled": 0, B.RUNNER: 0, B.RUG: 0, B.FLAT: 0, B.AMBIGUOUS: 0,
         "clean_runner": 0, "pump_then_rug": 0, "runner_order_unknown": 0}
    for r in rows:
        lab = r["outcome_label"]
        if lab in (B.RUNNER, B.RUG, B.FLAT, B.AMBIGUOUS):
            t[lab] += 1
            if lab != B.AMBIGUOUS:
                t["labelled"] += 1
        if lab == B.RUNNER:
            low = r.get("after_2x_low")
            if r.get("outcome_src") != "path":
                t["runner_order_unknown"] += 1
            elif low is not None and low <= 0.3:
                t["pump_then_rug"] += 1
            else:
                t["clean_runner"] += 1
    n = t["labelled"]
    pct = lambda k: round(100.0 * t[k] / n, 1) if n else None  # noqa: E731
    t["runner_pct"], t["rug_pct"] = pct(B.RUNNER), pct(B.RUG)
    t["clean_runner_pct"], t["pump_then_rug_pct"] = pct("clean_runner"), pct("pump_then_rug")
    return t


def summary(db, days: float = 7.0, now: Optional[float] = None, policy: Optional[str] = None) -> dict:
    """Live vs proposed, with outcomes. Each coin counts once per (live, proposed) pair it reached."""
    now = now or time.time()
    init_schema(db)
    policies = [r["policy"] for r in db.fetchall(
        "SELECT policy, COUNT(*) n FROM shadow_tests WHERE ts>? GROUP BY policy ORDER BY n DESC",
        (now - days * 86400,))]
    if policy is None:
        policy = policies[0] if policies else None
    out: dict = {"days": days, "policy": policy, "policies": policies}
    if policy is None:
        return {**out, "rows": 0}
    rows = db.fetchall("SELECT * FROM shadow_tests WHERE ts>? AND policy=? ORDER BY ts", (now - days * 86400, policy))
    # one row per coin and decision pair: repeated research of the same coin is not extra evidence
    seen, uniq = set(), []
    for r in rows:
        k = (r["mint"], r["live_action"], r["prop_action"])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    matrix: dict = {}
    for r in uniq:
        matrix.setdefault(r["live_decision"], {})
        matrix[r["live_decision"]][r["prop_decision"]] = matrix[r["live_decision"]].get(r["prop_decision"], 0) + 1
    fired: dict = {}
    for r in uniq:
        for rule in json.loads(r["rules_fired"] or "[]"):
            fired.setdefault(rule, []).append(r)
    out.update({
        "rows": len(rows), "coins": len({r["mint"] for r in rows}), "compared": len(uniq),
        "changed": sum(1 for r in uniq if r["changed"]),
        "with_outcome": sum(1 for r in uniq if r["outcome_label"]),
        "matrix": matrix,
        "live_calls": _tally([r for r in uniq if r["live_action"] == CALL]),
        "proposed_calls": _tally([r for r in uniq if r["prop_action"] == CALL]),
        "only_proposed_calls": _tally([r for r in uniq if r["prop_action"] == CALL and r["live_action"] != CALL]),
        "only_live_calls": _tally([r for r in uniq if r["live_action"] == CALL and r["prop_action"] != CALL]),
        "live_reject_proposed_watch_or_better": _tally(
            [r for r in uniq if r["live_action"] == "REJECT" and r["prop_action"] in ("WATCHLIST", CALL)]),
        "proposed_reject_live_watch_or_better": _tally(
            [r for r in uniq if r["prop_action"] == "REJECT" and r["live_action"] in ("WATCHLIST", CALL)]),
        "unchanged": _tally([r for r in uniq if not r["changed"]]),
        "rules": {k: {"text": RULES.get(k, ""), **_tally(v)} for k, v in fired.items()},
    })
    span_days = max(1e-9, min(days, (now - min(r["ts"] for r in rows)) / 86400)) if rows else days
    out["calls_per_day"] = {"live": round(out["live_calls"]["n"] / span_days, 2),
                            "proposed": round(out["proposed_calls"]["n"] / span_days, 2)}
    fits, misses = [], []
    for r in uniq:
        prof = (json.loads(r["components"] or "{}").get("proposed") or {}).get("profile")
        if isinstance(prof, dict):
            (fits if prof.get("fits") else misses).append(r)
    if fits or misses:
        # every analysed decision, not only calls: a far bigger sample of what the profile filters
        out["profile_fits"], out["profile_excludes"] = _tally(fits), _tally(misses)
    return out


def _line(name: str, t: dict) -> str:
    if not t["n"]:
        return f"  {name:<44} 0"
    rate = (f"{t['runner_pct']:.0f}% ran 2x ({t['clean_runner']} clean, {t['pump_then_rug']} pump-then-rug, "
            f"{t['runner_order_unknown']} order unknown), {t['rug_pct']:.0f}% rugged"
            if t["labelled"] else "no outcomes yet")
    return (f"  {name:<44} {t['n']:>4} coins · {t['labelled']} with outcome · "
            f"{t[B.RUNNER]} runner / {t[B.FLAT]} flat / {t[B.RUG]} rug ({rate})")


def format_summary(sm: dict) -> str:
    if not sm.get("rows"):
        return (f"Shadow test — last {sm['days']:g} days: nothing recorded yet. "
                "Rows appear as coins are researched (NORTHSTAR_SHADOW_TEST_ENABLED).")
    lines = [f"Shadow test — last {sm['days']:g} days · policy {sm['policy']}",
             "(live callouts are unchanged; the proposal is recorded next to them)",
             f"  {sm['rows']} records · {sm['coins']} coins · {sm['compared']} coin/decision pairs · "
             f"{sm['changed']} where the proposal decides differently · {sm['with_outcome']} with an outcome",
             "", "  live decision -> proposed decision"]
    for live, props in sorted(sm["matrix"].items()):
        for prop, n in sorted(props.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {live:<20} -> {prop:<20} {n}")
    lines += ["", "  outcomes (runner = 2x before 0.6x, rug = 0.3x before 1.3x):",
              _line("live calls", sm["live_calls"]),
              _line("proposed calls", sm["proposed_calls"]),
              _line("called ONLY by the proposal", sm["only_proposed_calls"]),
              _line("called ONLY live", sm["only_live_calls"]),
              _line("live REJECT -> proposed WATCH/call", sm["live_reject_proposed_watch_or_better"]),
              _line("proposed REJECT -> live WATCH/call", sm["proposed_reject_live_watch_or_better"]),
              _line("same decision", sm["unchanged"])]
    if sm.get("calls_per_day"):
        lines.append(f"  calls per day: live {sm['calls_per_day']['live']:g} · proposed {sm['calls_per_day']['proposed']:g}")
    if sm.get("profile_fits"):
        lines += ["", "  every analysed coin, by whether it fits this policy's call profile:",
                  _line("fits the profile", sm["profile_fits"]),
                  _line("excluded by the profile", sm["profile_excludes"])]
    if sm.get("rules"):
        lines += ["", "  rules that changed something:"]
        lines += [_line(k, v) for k, v in sm["rules"].items()]
    lines += ["", "  Small samples: a few coins either way is noise. Compare only after 1-2 weeks."]
    if len(sm.get("policies") or []) > 1:
        lines.append(f"  other policies in this window: {', '.join(p for p in sm['policies'] if p != sm['policy'])}")
    return "\n".join(lines)
