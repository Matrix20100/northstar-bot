"""Northstar control panel: a password-protected web dashboard served by the bot itself.

Tabs: Overview · Watching · Calls · Learning · Settings · Self-test. Stdlib-only HTTP server so
it runs on Wispbyte without extra packages. The page is northstar/dashboard.html.

Security
  * Locked until NORTHSTAR_DASHBOARD_PASSWORD is set (the server sits on a public IP and shows
    strategy, settings and a test button). Login gives an HttpOnly, SameSite=Strict session
    cookie; 5 wrong passwords from one address lock it out for 10 minutes.
  * Keys and tokens are never sent to the browser: secret settings only show "set" / "not set".
  * The database is opened read-only. The only action is the self-test, which never posts to
    Discord and never changes settings.
"""
from __future__ import annotations

import asyncio
import hmac
import concurrent.futures
import json
import os
import secrets as _secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from . import selftest as ST

PAGE = Path(__file__).with_name("dashboard.html")
ROOT = Path(__file__).resolve().parents[1]
SESSION_SECONDS = 7 * 86400
MAX_FAILS, LOCKOUT = 5, 600

# The live bot registers itself here (cli._run) so the panel can show live state and settings.
LIVE: dict[str, Any] = {"engine": None, "settings": None, "loop": None}


def attach(engine, loop) -> None:
    LIVE.update(engine=engine, settings=engine.s, loop=loop)


def _version() -> str:
    try:
        v = (ROOT / "VERSION").read_text().strip()
    except OSError:
        v = ""
    commit = _commit()
    return f"v{v} ({commit})" if v else commit


def _commit() -> str:
    try:
        head = (ROOT / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = ROOT / ".git" / head.split(" ", 1)[1]
            return ref.read_text().strip()[:7] if ref.exists() else "unknown"
        return head[:7]
    except OSError:
        return "zip upload"


class _Ro:
    """Tiny read-only adapter with the Database methods learning/brain use."""

    def __init__(self, conn: sqlite3.Connection):
        self.c = conn

    def fetchall(self, sql, params=()):
        return [dict(r) for r in self.c.execute(sql, tuple(params)).fetchall()]

    def fetchone(self, sql, params=()):
        r = self.c.execute(sql, tuple(params)).fetchone()
        return dict(r) if r else None


def _table_ok(c, name: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


# ----------------------------------------------------------------------------- data views
def view_status(c, s, now: float) -> dict:
    eng = LIVE.get("engine")
    counts = {r["state"]: r["n"] for r in c.execute("SELECT state, COUNT(*) n FROM candidates GROUP BY state")}
    open_calls = c.execute("SELECT COUNT(*) FROM alerts WHERE state NOT IN ('CLOSED') AND status='SENT'").fetchone()[0] \
        if _table_ok(c, "alerts") else 0
    calls_24h = c.execute("SELECT COUNT(*) FROM alerts WHERE status='SENT' AND sent_at > ?", (now - 86400,)).fetchone()[0]
    last_research = c.execute("SELECT MAX(ts) FROM research_snapshots").fetchone()[0]
    researched_1h = c.execute("SELECT COUNT(*) FROM research_snapshots WHERE ts > ?", (now - 3600,)).fetchone()[0]
    surges_6h = c.execute("SELECT COUNT(*) FROM radar_obs WHERE fired=1 AND ts > ?", (now - 6 * 3600,)).fetchone()[0] \
        if _table_ok(c, "radar_obs") else 0
    providers = [dict(r) for r in c.execute(
        "SELECT provider, status, ok_count, err_count, last_ok, last_err, last_error, budget_used FROM provider_health "
        "ORDER BY provider")]
    brain = None
    if _table_ok(c, "brain_models"):
        m = c.execute("SELECT ts, n, positives, auc, recent_auc, trusted FROM brain_models ORDER BY ts DESC LIMIT 1").fetchone()
        brain = dict(m) if m else None
    actions = {r["action"]: r["n"] for r in c.execute(
        "SELECT action, COUNT(*) n FROM research_snapshots WHERE ts > ? GROUP BY action", (now - 86400,))}
    return {
        "now": now, "version": _version(), "running": eng is not None,
        "uptime_s": now - eng.stats.get("started_at", now) if eng else None,
        "stats": dict(eng.stats) if eng else None,
        "queue": eng.queue.qsize() if eng is not None and getattr(eng, "queue", None) is not None else None,
        "mode": ("DRY RUN" if s.dry_run else ("LIVE" if s.discord_enabled else "DISCORD OFF")) if s else "unknown",
        "candidates": counts, "open_calls": open_calls, "calls_24h": calls_24h,
        "last_research": last_research, "researched_1h": researched_1h, "surges_6h": surges_6h,
        "actions_24h": actions, "providers": providers, "brain": brain,
    }


def view_watching(c, now: float) -> dict:
    queue = [dict(r) for r in c.execute(
        "SELECT mint, symbol, name, state, priority, sources, last_score, last_action, next_research_at, "
        "last_research_at FROM candidates WHERE state IN ('WATCHLIST','QUALIFIED') "
        "OR (state = 'DISCOVERED' AND next_research_at IS NOT NULL) "
        "ORDER BY (state = 'DISCOVERED'), priority DESC, COALESCE(next_research_at, 0) LIMIT 80")]
    for q in queue:
        try:
            q["sources"] = json.loads(q.get("sources") or "[]")
        except ValueError:
            q["sources"] = []
    surges = []
    if _table_ok(c, "radar_obs"):
        for r in c.execute("SELECT o.mint, o.ts, o.fired, o.metrics, c.symbol, c.last_action FROM radar_obs o "
                           "LEFT JOIN candidates c ON c.mint = o.mint WHERE o.ts > ? ORDER BY o.ts DESC LIMIT 60",
                           (now - 6 * 3600,)):
            d = dict(r)
            try:
                d["metrics"] = json.loads(d["metrics"])
            except (TypeError, ValueError):
                d["metrics"] = {}
            surges.append(d)
    early = [dict(r) for r in c.execute(
        "SELECT mint, created_at, updated_at, peak_multiple, min_multiple, score, status, entry_market_cap_sol, "
        "current_market_cap_sol FROM early_signals ORDER BY created_at DESC LIMIT 30")]
    eng = LIVE.get("engine")
    trending = list((getattr(getattr(eng, "radar", None), "trending", None) or {}).values())[:30] if eng else []
    # v2.5.9: only coins Northstar actually follows count as "watched". Launches the feed reported
    # but nobody scheduled are counted apart (they wait for the curve radar and expire quickly).
    row = c.execute(
        "SELECT SUM(state IN ('WATCHLIST','QUALIFIED')) AS watched, "
        "SUM(state = 'DISCOVERED' AND next_research_at IS NOT NULL) AS queued, "
        "SUM(state = 'DISCOVERED' AND next_research_at IS NULL) AS waiting "
        "FROM candidates WHERE state IN ('DISCOVERED','WATCHLIST','QUALIFIED')").fetchone()
    watched, queued, waiting = (int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)) if row else (0, 0, 0)
    return {"queue": queue, "queue_total": watched + queued, "watched": watched, "queued": queued,
            "waiting_launches": waiting, "surges": surges, "early": early, "trending": trending}


PULSE_FINAL_STRETCH_USD = 40_000.0   # a curve coin past this market cap is close to migrating


def _pulse_card(row: dict) -> Optional[dict]:
    try:
        r = json.loads(row["report"] or "{}")
    except (TypeError, ValueError):
        return None
    ident = r.get("identity") or {}
    market = r.get("market") or {}
    pm = r.get("pre_migration") or {}
    u = r.get("unified") if isinstance(r.get("unified"), dict) else {}

    def val(x):
        return x.get("value") if isinstance(x, dict) else x

    curve = r.get("lifecycle") == "PRE_MIGRATION_BONDING_CURVE"
    mc = pm.get("market_cap_usd_current") if curve else val(market.get("market_cap"))
    if curve:
        prog = pm.get("curve_progress_pct")
        stage = ("final" if (prog is not None and prog >= 70) or (prog is None and (mc or 0) >= PULSE_FINAL_STRETCH_USD)
                 else "new")
    else:
        stage = "migrated"
    gates = [g for g in r.get("gates") or [] if isinstance(g, dict)]
    relaxed = {t.split(":")[0] for t in (u.get("suspicions") or []) if "(not blocking:" in t}
    filters = []
    for g in gates:
        name = str(g.get("name") or "")
        label = name.replace("_", " ")
        st = g.get("status") or "UNKNOWN"
        if st == "FAIL" and label in relaxed:
            st = "WARN"
        filters.append({"name": label, "status": st, "reason": str(g.get("reason") or "")[:160]})
    rug = u.get("rug_odds")
    if rug is not None:
        filters.append({"name": "learned rug odds", "status": "FAIL" if rug >= 0.6 else ("WARN" if rug > 0.3 else "PASS"),
                        "reason": f"{rug * 100:.0f}%"})
    action = row.get("action") or r.get("final_action")
    reasons = [str(x) for x in r.get("reasons") or []]
    prefilter = bool(reasons) and all(x.startswith("prefilter:") for x in reasons)
    if action == "REJECT" and not any(f["status"] == "FAIL" for f in filters) and reasons:
        # rejected by a rule that is not a gate (prefilter, timing, curve thresholds): show why
        why = reasons[0]
        name = ("already pumped" if "since launch" in why else
                "curve activity" if why.startswith("pre-migration activity") else
                "learned rug odds" if "rug odds" in why else
                str(u.get("reason_class") or "").replace("_", " ").lower() or why.split(":")[0][:28])
        filters.append({"name": name, "status": "FAIL", "reason": why[:160]})
    return {
        "prefilter": prefilter,
        "mint": row["mint"], "symbol": ident.get("symbol"), "name": ident.get("name"),
        "image": market.get("image_url") or ident.get("image_url"), "stage": stage, "ts": row["ts"],
        "market_cap": mc, "liquidity": None if curve else val(market.get("liquidity_usd")),
        "pool_age_h": (r.get("security") or {}).get("pool_age_hours"),
        "action": action, "decision": u.get("decision"), "score": u.get("score", row.get("score")),
        "opportunity": u.get("opportunity_score"), "rug_odds": rug,
        "passed": action in ("AUTO_POST", "WATCHLIST"),
        "failed": [f for f in filters if f["status"] == "FAIL"][:4],
        "warned": [f for f in filters if f["status"] == "WARN"][:3],
        "passed_count": sum(1 for f in filters if f["status"] == "PASS"), "filter_count": len(filters),
        "why": (r.get("reasons") or [""])[0][:200] if r.get("reasons") else "",
        "tracked": ((r.get("tracked_wallets") or {}).get("holding_count") or 0),
    }


def view_pulse(c, now: float, hours: float = 3.0) -> dict:
    """v2.6.0: Axiom-Pulse-style view of what the bot researched recently. Three stages (new on the
    curve, final stretch before migration, migrated) and the coins that passed every filter."""
    rows = [dict(r) for r in c.execute(
        "SELECT s.mint, s.ts, s.action, s.score, s.report FROM research_snapshots s "
        "JOIN (SELECT mint, MAX(id) AS id FROM research_snapshots WHERE ts > ? GROUP BY mint) m ON s.id = m.id "
        "WHERE s.mint NOT IN (SELECT mint FROM candidates WHERE state = 'IGNORED') "
        "ORDER BY s.ts DESC LIMIT 300", (now - hours * 3600,))]
    every = [x for x in (_pulse_card(r) for r in rows) if x]
    cards = [x for x in every if not x["prefilter"]]   # dust (no liquidity, no trades) is counted, not shown
    cols = {"new": [], "final": [], "migrated": []}
    for x in cards:
        cols[x["stage"]].append(x)
    passed = [x for x in cards if x["passed"]]
    funnel = {"researched": len(every), "prefiltered": len(every) - len(cards), "passed": len(passed),
              "failed_on": {}}
    for x in cards:
        for f in x["failed"][:1]:
            funnel["failed_on"][f["name"]] = funnel["failed_on"].get(f["name"], 0) + 1
    funnel["failed_on"] = dict(sorted(funnel["failed_on"].items(), key=lambda kv: -kv[1])[:8])
    return {"hours": hours, "columns": {k: v[:60] for k, v in cols.items()}, "passed": passed[:40], "funnel": funnel}


def view_paper(c, s, now: float, account: str = "calls") -> dict:
    """A paper account (read-only connection: shows the defaults until the bot starts a run)."""
    from . import paper as PT
    account = account if account in PT.ACCOUNTS else "calls"
    has_account = _table_ok(c, "paper_runs") and any(
        r[1] == "account" for r in c.execute("PRAGMA table_info(paper_runs)").fetchall())
    brain = {"min_p": getattr(s, "paper_brain_min_p", 0.15), "max_rug": getattr(s, "paper_brain_max_rug", 0.3),
             "max_open": getattr(s, "paper_brain_max_open", 10), "hold_hours": getattr(s, "paper_brain_hold_hours", 24)}
    if not has_account:
        return {"ready": False, "account": account, "brain": brain, "defaults": {"start_usd": getattr(s, "paper_start_usd", 1000.0),
                                             "position_pct": getattr(s, "paper_position_pct", 10.0),
                                             "fee_pct": getattr(s, "paper_fee_pct", 1.0)}}
    db = _Ro(c)
    run = db.fetchone("SELECT * FROM paper_runs WHERE ended_at IS NULL AND account = ? ORDER BY id DESC LIMIT 1",
                      (account,))
    if not run or not _table_ok(c, "paper_trades"):
        return {"ready": False, "account": account, "brain": brain,
                "defaults": {"start_usd": getattr(s, "paper_start_usd", 1000.0),
                             "position_pct": getattr(s, "paper_position_pct", 10.0),
                             "fee_pct": getattr(s, "paper_fee_pct", 1.0)}}
    return {"ready": True, "brain": brain, **PT.summary(db, s, now, run=run, account=account)}


def view_calls(c) -> dict:
    cols = {r[1] for r in c.execute("PRAGMA table_info(alerts)")}
    extra = ", ".join(x for x in ("peak_mcap", "peak_at", "call_type", "plan_json") if x in cols)
    rows = [dict(r) for r in c.execute(
        "SELECT id, mint, status, state, created_at, sent_at, symbol, name, entry_mcap, current_mcap, max_multiple, "
        f"min_multiple, score, risk, closed_at, last_update_at{', ' + extra if extra else ''} FROM alerts "
        "ORDER BY created_at DESC LIMIT 60")]
    events: dict[int, list] = {}
    if rows and _table_ok(c, "alert_events"):
        ids = tuple(r["id"] for r in rows)
        q = f"SELECT alert_id, ts, kind FROM alert_events WHERE alert_id IN ({','.join('?' * len(ids))}) ORDER BY ts"
        for e in c.execute(q, ids):
            events.setdefault(e["alert_id"], []).append({"ts": e["ts"], "kind": e["kind"]})
    for r in rows:
        r["events"] = [e for e in events.get(r["id"], []) if e["kind"] not in ("published",)][-8:]
        r.pop("plan_json", None)
    return {"calls": rows}


def view_decisions(c) -> dict:
    rows = [dict(r) for r in c.execute(
        "SELECT mint, symbol, name, state, last_action, last_score, last_risk, last_confidence, reasons, last_research_at "
        "FROM candidates WHERE last_research_at IS NOT NULL ORDER BY last_research_at DESC LIMIT 80")]
    for r in rows:
        try:
            r["reasons"] = json.loads(r.get("reasons") or "[]")[:4]
        except ValueError:
            r["reasons"] = []
    return {"decisions": rows}


def view_token(c, mint: str) -> Optional[dict]:
    row = c.execute("SELECT mint, state, symbol, name, last_score, last_risk, last_confidence, reasons, sources, "
                    "updated_at FROM candidates WHERE mint=?", (mint,)).fetchone()
    if not row:
        return None
    out = dict(row)
    for k in ("reasons", "sources"):
        try:
            out[k] = json.loads(out.get(k) or "[]")
        except ValueError:
            out[k] = []
    snap = c.execute("SELECT report, action, score, risk, confidence, ts FROM research_snapshots WHERE mint=? "
                     "ORDER BY ts DESC LIMIT 1", (mint,)).fetchone()
    out["report"] = {}
    if snap:
        try:
            out["report"] = json.loads(snap["report"] or "{}")
        except ValueError:
            pass
        out["snapshot"] = {k: snap[k] for k in ("action", "score", "risk", "confidence", "ts")}
    return out


def view_learning(c, s, now: float) -> dict:
    from . import brain as B
    from . import learning as L
    db = _Ro(c)
    if not _table_ok(c, "shadow_calls"):
        return {"ready": False}
    info = L.summary(db, s, s.learning_days, now) if s else {}
    m = B.latest(db)
    learned = {r["key"]: r for r in db.fetchall("SELECT key, value, updated_at FROM learned_settings")}
    eng = LIVE.get("engine")
    defaults = getattr(eng, "_tunable_defaults", {}) if eng else {}
    def live_value(t):
        if eng is None and t.key in learned:          # panel running without the bot: show what the bot uses
            return learned[t.key]["value"]
        return getattr(s, t.key) if s else None
    tunables = [{"key": t.key, "label": t.label, "kind": t.kind, "grid": list(t.grid), "value": live_value(t),
                 "default": defaults.get(t.key, getattr(s, t.key) if s else None),
                 "learned": t.key in learned, "updated_at": (learned.get(t.key) or {}).get("updated_at")}
                for t in L.TUNABLES]
    stats = db.fetchone("SELECT COUNT(*) total, SUM(evaluated=1) evaluated, SUM(evaluated=0) pending, "
                        "SUM(called) called FROM shadow_calls") or {}
    runners = db.fetchone("SELECT COUNT(*) n FROM shadow_calls WHERE evaluated=1 AND peak_x >= ?",
                          (getattr(s, "brain_win_x", 2.0) if s else 2.0,)) or {}
    log_rows = db.fetchall("SELECT ts, key, old, new, detail FROM learning_log ORDER BY ts DESC LIMIT 40")
    for r in log_rows:
        try:
            r["detail"] = json.loads(r["detail"] or "{}")
        except (TypeError, ValueError):
            r["detail"] = {}
    history = db.fetchall("SELECT ts, n, positives, auc, recent_auc, trusted FROM brain_models ORDER BY ts DESC LIMIT 20")
    radar = db.fetchone("SELECT COUNT(*) total, SUM(fired) fired, SUM(evaluated=1) evaluated FROM radar_obs") or {}
    brain = None
    if m is not None:
        brain = {"trusted": m.trusted, "n": m.n, "positives": m.positives, "auc": m.auc, "recent_auc": m.recent_auc,
                 "ts": m.ts, "describe": B.describe(m),
                 "signals": [{"name": B.LABELS.get(f, f), "weight": round(w, 3)}
                             for f, w in sorted(m.coef.items(), key=lambda kv: -abs(kv[1]))[:16]]}
    flags = None
    if _table_ok(c, "flag_outcomes"):
        stats = {r["key"]: r for r in db.fetchall("SELECT key, n, crash, run2x FROM flag_outcomes")}
        base = stats.pop("_all", None)
        if base:
            rows = [{"key": k, "label": FLAG_LABELS.get(k, k.replace("_", " ")), **v} for k, v in stats.items() if v["n"] >= 5]
            rows.sort(key=lambda r: -(r["crash"] / r["n"]))
            flags = {"base": base, "rows": rows[:25]}
    from . import rugmodel as RM
    from . import training_store as TS
    rm = RM.latest(db) if _table_ok(c, "rug_models") else None
    rug_brain = None if rm is None else {
        "trusted": rm.trusted, "auc": rm.auc, "recent_auc": rm.recent_auc, "n": rm.n, "positives": rm.positives,
        "ts": rm.ts, "signals": [{"name": n, "weight": round(w, 3)} for n, w in rm.importance(14)]}
    memory = TS.stats(db) if _table_ok(c, "training_examples") else None
    return {"ready": True, "brain": brain, "rug_brain": rug_brain, "memory": memory, "flags": flags, "brain_history": history, "tunables": tunables, "log": log_rows,
            "shadows": stats, "runners": runners.get("n", 0), "radar": radar,
            "win_x": getattr(s, "brain_win_x", 2.0) if s else 2.0, "loss_x": getattr(s, "brain_loss_x", 0.6) if s else 0.6,
            "summary": {k: info.get(k) for k in ("evaluated", "pending", "standard_trades", "standard_avg",
                                                 "scalp_trades", "scalp_avg", "median_peak")}}


FLAG_LABELS = {
    "whale_holder": "one wallet holds 8%+", "buying_concentrated": "a few wallets do most of the buying",
    "possible_wash_trading": "possible wash trading", "volume_liquidity_implausible": "volume implausible for liquidity",
    "possible_copycat": "copycat name/ticker", "fresh_wallets": "most checked wallets brand new",
    "fresh_supply": "fresh wallets hold 5%+", "fresh_supply_big": "fresh wallets hold 15%+",
    "dev_holds": "dev holds 3%+", "dev_holds_big": "dev holds 10%+", "dev_sold": "dev sold after launch",
    "creator_dumping": "dev is selling", "dev_prior_rugs": "dev rugged before", "creator_prior_rugs": "dev rugged before",
    "cluster": "connected wallets hold 8%+", "cluster_big": "connected wallets hold 20%+",
    "dev_linked_wallets": "dev-linked wallets hold supply", "two_hop": "hidden 2-hop funding",
    "bundle": "launch bundle 8%+", "bundle_big": "launch bundle 20%+", "top10": "top 10 own 28%+",
    "top10_big": "top 10 own 45%+", "liquidity_removal": "liquidity dropped from its high",
    "lp_partial": "LP only partly burned", "lp_not_locked": "LP not burned/locked", "rugcheck_danger": "RugCheck danger",
    "mint_authority": "mint authority active", "freeze_authority": "freeze authority active",
    "recycled_socials": "recycled socials", "serial_launcher": "serial launcher",
}

GROUPS = [
    ("Discord & posting", ("discord_", "dry_run", "free_", "vip_", "early_watch", "max_alerts", "alert_", "embed_")),
    ("Who gets called", ("min_", "max_allowed", "watchlist_", "block_", "require_", "reject_", "max_")),
    ("Trade plan (VIP calls)", ("plan_", "trade_plan")),
    ("Rug scalps", ("scalp_",)),
    ("Surge radar", ("radar_",)),
    ("Self-learning & brain", ("learning_", "brain_")),
    ("Score weights", ("w_",)),
    ("Data sources & keys", ("enable_", "helius", "pumpportal", "coingecko", "geckoterminal", "solana_rpc", "rugcheck",
                             "dexscreener", "rpc_", "http_", "social")),
    ("Timing & storage", ("", )),
]
SECRETS = {"discord_token", "helius_api_key", "pumpportal_api_key", "coingecko_api_key", "dashboard_password"}


def view_settings(c, s) -> dict:
    if s is None:
        return {"groups": []}
    learned = {}
    if _table_ok(c, "learned_settings"):
        learned = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM learned_settings")}
    from_env = set(getattr(s, "model_fields_set", set()))
    groups: dict[str, list] = {g: [] for g, _ in GROUPS}
    for name in type(s).model_fields:
        raw = getattr(s, name, None)
        if name in SECRETS or "secret" in name or name.endswith("_api_key") or name.endswith("_token"):
            value = "set" if raw is not None and (raw.get_secret_value() if hasattr(raw, "get_secret_value") else raw) else "not set"
            secret = True
        else:
            value, secret = raw, False
            if isinstance(value, (set, tuple)):
                value = list(value)
            elif not isinstance(value, (int, float, str, bool, list, dict, type(None))):
                value = str(value)
        source = "learned" if name in learned else (".env" if name in from_env else "default")
        group = next((g for g, prefixes in GROUPS if any(name.startswith(p) for p in prefixes) and prefixes != ("",)),
                     "Timing & storage")
        groups[group].append({"name": name, "env": "NORTHSTAR_" + name.upper(), "value": value, "secret": secret,
                              "source": source})
    return {"groups": [{"name": g, "settings": v} for g, v in groups.items() if v],
            "problems": s.config_problems()}


# ----------------------------------------------------------------------------- server
class DashboardServer:
    def __init__(self, db_path: str | Path, host: str = "0.0.0.0", port: int = 8080, settings=None,
                 password: Optional[str] = None):
        self.db_path, self.host, self.port = str(db_path), host, int(port)
        self.settings = settings
        self.password = password if password is not None else os.getenv("NORTHSTAR_DASHBOARD_PASSWORD", "")
        self.sessions: dict[str, float] = {}
        self.fails: dict[str, list[float]] = {}
        self.runner = ST.Runner()
        self.server: Optional[ThreadingHTTPServer] = None
        self._lock = threading.Lock()

    # -- auth
    def login(self, ip: str, password: str, now: Optional[float] = None) -> Optional[str]:
        now = now or time.time()
        with self._lock:
            recent = [t for t in self.fails.get(ip, []) if now - t < LOCKOUT]
            if len(recent) >= MAX_FAILS:
                self.fails[ip] = recent
                return None
            if self.password and hmac.compare_digest(password.encode(), self.password.encode()):
                self.fails.pop(ip, None)
                tok = _secrets.token_urlsafe(32)
                self.sessions = {k: v for k, v in self.sessions.items() if v > now}
                self.sessions[tok] = now + SESSION_SECONDS
                return tok
            self.fails[ip] = recent + [now]
            return None

    def locked_out(self, ip: str, now: Optional[float] = None) -> bool:
        now = now or time.time()
        return len([t for t in self.fails.get(ip, []) if now - t < LOCKOUT]) >= MAX_FAILS

    def valid(self, token: Optional[str], now: Optional[float] = None) -> bool:
        if not token or not self.password:
            return False
        exp = self.sessions.get(token)
        return exp is not None and exp > (now or time.time())

    # -- self-test
    def start_selftest(self, include_code: bool) -> bool:
        s = LIVE.get("settings") or self.settings
        eng, loop = LIVE.get("engine"), LIVE.get("loop")

        def run():
            if eng is not None and loop is not None and loop.is_running():
                fut = asyncio.run_coroutine_threadsafe(ST.run_async(s, self.db_path, eng.p, eng, False), loop)
                res = fut.result(timeout=150)
            else:
                res = asyncio.run(ST.run_async(s, self.db_path, None, None, False))
            if include_code:
                res += ST.check_code()
            return res
        return self.runner.start(run)

    # -- http
    def handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "Northstar"

            def log_message(self, fmt, *args):
                return

            def _ip(self) -> str:
                return self.client_address[0] if self.client_address else "?"

            def _cookie(self) -> Optional[str]:
                for part in (self.headers.get("Cookie") or "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "ns_session":
                        return v
                return None

            def _send(self, status: int, body, typ: str = "application/json", headers: Optional[dict] = None):
                data = body.encode() if isinstance(body, str) else json.dumps(body, default=str).encode()
                self.send_response(status)
                self.send_header("Content-Type", typ)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _db(self):
                c = sqlite3.connect(f"file:{owner.db_path}?mode=ro", uri=True, timeout=3)
                c.row_factory = sqlite3.Row
                return c

            def _same_origin(self) -> bool:
                origin = self.headers.get("Origin")
                if not origin:
                    return True
                host = self.headers.get("Host") or ""
                return urlparse(origin).netloc == host

            def _body(self) -> dict:
                n = min(int(self.headers.get("Content-Length") or 0), 10_000)
                raw = self.rfile.read(n) if n else b""
                try:
                    return json.loads(raw or b"{}")
                except ValueError:
                    return {}

            def do_GET(self):
                u = urlparse(self.path)
                if u.path == "/":
                    try:
                        page = PAGE.read_text(encoding="utf-8")
                    except OSError:
                        page = "<h1>Northstar</h1><p>dashboard.html missing</p>"
                    return self._send(200, page, "text/html; charset=utf-8")
                if u.path == "/healthz":
                    return self._send(200, {"ok": True, "service": "northstar-dashboard"})
                if u.path == "/api/session":
                    return self._send(200, {"configured": bool(owner.password), "authed": owner.valid(self._cookie()),
                                            "locked_out": owner.locked_out(self._ip())})
                if not u.path.startswith("/api/"):
                    return self._send(404, {"error": "not found"})
                if not owner.password:
                    return self._send(401, {"error": "locked", "why": "set NORTHSTAR_DASHBOARD_PASSWORD"})
                if not owner.valid(self._cookie()):
                    return self._send(401, {"error": "login required"})
                if u.path == "/api/selftest":
                    return self._send(200, owner.runner.state())
                s = LIVE.get("settings") or owner.settings
                now = time.time()
                c = None
                try:
                    c = self._db()
                    if u.path == "/api/status":
                        return self._send(200, view_status(c, s, now))
                    if u.path == "/api/watching":
                        return self._send(200, view_watching(c, now))
                    if u.path == "/api/pulse":
                        hours = float((parse_qs(u.query).get("hours") or ["3"])[0] or 3)
                        return self._send(200, view_pulse(c, now, max(0.5, min(hours, 24.0))))
                    if u.path == "/api/curve":
                        from . import curve_watch as CW
                        if not _table_ok(c, "curve_watch"):
                            return self._send(200, {"scored": 0, "pending": 0, "rules": {}, "days": 7})
                        return self._send(200, CW.evaluate(_Ro(c), 7.0, now))
                    if u.path == "/api/calls":
                        return self._send(200, view_calls(c))
                    if u.path == "/api/paper":
                        acct = (parse_qs(u.query).get("account") or ["calls"])[0]
                        return self._send(200, view_paper(c, s, now, acct))
                    if u.path == "/api/decisions":
                        return self._send(200, view_decisions(c))
                    if u.path == "/api/funnel":
                        from . import telemetry as T
                        if not _table_ok(c, "funnel_events"):
                            return self._send(200, {"error": "funnel table not created yet — restart the bot once"})
                        hours = float((parse_qs(u.query).get("hours") or ["24"])[0] or 24)
                        return self._send(200, T.summary(_Ro(c), max(1.0, min(hours, 24 * 14)), now))
                    if u.path == "/api/learning":
                        return self._send(200, view_learning(c, s, now))
                    if u.path == "/api/settings":
                        return self._send(200, view_settings(c, s))
                    if u.path == "/api/token":
                        mint = (parse_qs(u.query).get("mint") or [""])[0]
                        if not mint or len(mint) > 64:
                            return self._send(400, {"error": "mint required"})
                        tok = view_token(c, mint)
                        return self._send(200, tok) if tok else self._send(404, {"error": "not found"})
                    return self._send(404, {"error": "not found"})
                except sqlite3.Error as e:
                    return self._send(503, {"error": "database temporarily unavailable", "detail": type(e).__name__})
                except Exception as e:  # noqa: BLE001 - one bad view must not kill the panel
                    return self._send(500, {"error": "view failed", "detail": f"{type(e).__name__}: {e}"[:200]})
                finally:
                    if c is not None:
                        c.close()

            def do_POST(self):
                u = urlparse(self.path)
                if not self._same_origin():
                    return self._send(403, {"error": "cross-site request refused"})
                if u.path == "/api/login":
                    if not owner.password:
                        return self._send(401, {"error": "locked", "why": "set NORTHSTAR_DASHBOARD_PASSWORD"})
                    if owner.locked_out(self._ip()):
                        return self._send(429, {"error": "too many attempts — wait 10 minutes"})
                    tok = owner.login(self._ip(), str(self._body().get("password") or ""))
                    if not tok:
                        return self._send(401, {"error": "wrong password"})
                    return self._send(200, {"ok": True}, headers={
                        "Set-Cookie": f"ns_session={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}"})
                if u.path == "/api/logout":
                    owner.sessions.pop(self._cookie() or "", None)
                    return self._send(200, {"ok": True}, headers={
                        "Set-Cookie": "ns_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
                if not owner.valid(self._cookie()):
                    return self._send(401, {"error": "login required"})
                if u.path == "/api/selftest":
                    started = owner.start_selftest(bool(self._body().get("code", True)))
                    return self._send(202 if started else 409, {"started": started, **owner.runner.state()})
                if u.path in ("/api/research", "/api/call"):
                    return self._manual(u.path, self._body())
                if u.path in ("/api/untrack", "/api/retrack"):
                    return self._untrack(u.path, self._body())
                if u.path == "/api/paper/start":
                    return self._paper_start(self._body())
                return self._send(404, {"error": "not found"})

            def _paper_start(self, body: dict):
                """Start a new paper run with the amount typed on the Paper tab."""
                from . import paper as PT
                from .db import Database
                try:
                    start = float(body.get("start_usd"))
                    pos = float(body.get("position_pct", 10))
                    fee = float(body.get("fee_pct", 1))
                except (TypeError, ValueError):
                    return self._send(422, {"ok": False, "error": "Enter numbers for the amount, size and fee."})
                if not (1 <= start <= 1e9) or not (0.1 <= pos <= 100) or not (0 <= fee <= 20):
                    return self._send(422, {"ok": False, "error": "Amount $1-$1,000,000,000, size 0.1-100%, "
                                                                  "fee 0-20%."})
                db = Database(owner.db_path)
                db.open()
                try:
                    run = PT.start_run(db, start, pos, fee, account=str(body.get("account") or "calls"))
                except Exception as e:  # noqa: BLE001
                    return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"})
                finally:
                    db.close()
                return self._send(200, {"ok": True, "run": run})

            def _untrack(self, path: str, body: dict):
                """Untrack / retrack a coin: through the running bot, or straight in the database."""
                mint = str(body.get("mint") or "").strip()[:64]
                reason = str(body.get("reason") or "")[:200]
                eng, loop = LIVE.get("engine"), LIVE.get("loop")
                try:
                    if eng is not None and loop is not None:
                        coro = eng.untrack(mint, reason, "dashboard") if path == "/api/untrack" else eng.retrack(mint)
                        res = asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=20)
                    else:
                        from .analysis.identity import is_valid_mint_format
                        if not is_valid_mint_format(mint):
                            return self._send(422, {"ok": False, "error": "That is not a Solana mint address."})
                        from .db import Database
                        db = Database(owner.db_path)
                        db.open()
                        try:
                            if path == "/api/untrack":
                                res = {"ok": True, **db.untrack(mint, reason, "dashboard")}
                            else:
                                ok = db.retrack(mint)
                                res = {"ok": ok, "error": None if ok else "That coin was not untracked."}
                        finally:
                            db.close()
                except Exception as e:  # noqa: BLE001
                    return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"})
                return self._send(200 if res.get("ok") else 422, res)

            def _manual(self, path: str, body: dict):
                """Hand a manual research or call to the running bot and wait for its answer."""
                eng, loop = LIVE.get("engine"), LIVE.get("loop")
                if eng is None or loop is None:
                    return self._send(503, {"ok": False, "error": "The bot is not running, so it cannot "
                                                                  "research or post right now."})
                mint = str(body.get("mint") or "").strip()[:64]
                if path == "/api/research":
                    coro, wait = eng.manual_research(mint), 150
                else:
                    coro, wait = eng.manual_call(mint, note=str(body.get("note") or "")[:500],
                                                 free_copy=bool(body.get("free_copy", False)),
                                                 override=bool(body.get("override"))), 90
                try:
                    res = asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=wait)
                except concurrent.futures.TimeoutError:
                    return self._send(504, {"ok": False, "error": "Still working after "
                                            f"{wait} seconds. Check Latest decisions in a minute."})
                except Exception as e:  # noqa: BLE001 - surface it, never crash the panel
                    return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"})
                return self._send(200 if res.get("ok") else 422, res)

        return Handler

    def start_background(self):
        if self.server:
            return
        self.server = ThreadingHTTPServer((self.host, self.port), self.handler())
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, name="northstar-dashboard", daemon=True).start()

    def serve_forever(self):
        self.start_background()
        while True:
            time.sleep(3600)


def pick_port(settings=None) -> int:
    """Wispbyte/Pterodactyl give the server's public port as SERVER_PORT (some hosts use PORT)."""
    for var in ("SERVER_PORT", "PORT"):
        v = os.getenv(var, "").strip()
        if v.isdigit():
            return int(v)
    return int(getattr(settings, "dashboard_port", 0) or os.getenv("NORTHSTAR_DASHBOARD_PORT") or 8080)


def start_dashboard(db_path: str | Path, port: int | None = None, settings=None) -> DashboardServer | None:
    if settings is not None:
        enabled, host = settings.dashboard_enabled, settings.dashboard_host
        pw = settings.dashboard_password.get_secret_value() if settings.dashboard_password else ""
    else:
        enabled = os.getenv("NORTHSTAR_DASHBOARD_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
        host = os.getenv("NORTHSTAR_DASHBOARD_HOST", "0.0.0.0")
        pw = os.getenv("NORTHSTAR_DASHBOARD_PASSWORD", "")
    if not enabled:
        return None
    chosen = port or pick_port(settings)
    try:
        server = DashboardServer(db_path, host, chosen, settings=settings, password=pw)
        server.start_background()
        state = "password protected" if pw else "LOCKED until NORTHSTAR_DASHBOARD_PASSWORD is set"
        print(f"Northstar dashboard listening on {host}:{chosen} ({state})", flush=True)
        return server
    except OSError as e:
        print(f"Northstar dashboard not started on port {chosen}: {e}", flush=True)
        return None
