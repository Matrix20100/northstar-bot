"""Paper trading: a simulated account that follows every call the bot posts (v3.1.0).

No real trades, ever. Nothing here signs or sends a transaction.

How a paper trade works
  * Every call posted after the current run started opens one position: `position_pct` of the
    cash left, bought at the call's entry market cap, minus `fee_pct` (fees and slippage).
  * It then does exactly what the call's trade plan tells people to do, at the moment the bot
    posts it and at the market cap the bot tracked at that moment:
      TP1  -> sell the plan's TP1 share of the position
      TP2  -> sell the plan's TP2 share (capped at what is left)
      TRAIL / STOP / TIME / DANGER -> sell everything left
  * When the bot stops tracking the call (tracking window over, coin rugged to 0.3x, message
    deleted, untracked), whatever is left is sold at the last market cap the bot saw.
  * Sells also pay `fee_pct`.

Market cap is used as the price: a pump.fun coin's supply is fixed, so a 2x in market cap is
a 2x in price. The fills are the bot's own observations (one every refresh interval), so a
real fill can be better or worse. Every number here comes from tracked calls; nothing is made up.

The account (starting amount, position size, fee) is set on the dashboard's Paper tab. A new
amount starts a new run; earlier runs and their trades are kept.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from . import trade_plan as TP

log = logging.getLogger("northstar.paper")

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    start_usd REAL NOT NULL,
    position_pct REAL NOT NULL,
    fee_pct REAL NOT NULL,
    ended_at REAL
);
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    ref TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    kind TEXT,
    opened_at REAL NOT NULL,
    entry_mcap REAL NOT NULL,
    cost_usd REAL NOT NULL,
    units REAL NOT NULL,
    units_left REAL NOT NULL,
    proceeds_usd REAL NOT NULL DEFAULT 0,
    last_mcap REAL,
    peak_mcap REAL,
    plan TEXT,
    done TEXT NOT NULL DEFAULT '[]',
    fills TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'OPEN',
    exit_reason TEXT,
    closed_at REAL,
    UNIQUE(run_id, ref)
);
CREATE INDEX IF NOT EXISTS ix_paper_open ON paper_trades(run_id, status);
"""

SELL_KINDS = ("TP1", "TP2") + tuple(sorted(TP.EXIT_KINDS))
REASONS = {"TP1": "take profit 1", "TP2": "take profit 2", "TRAIL": "trailing stop", "STOP": "stop loss",
           "TIME": "time stop", "DANGER": "exit now (danger)", "END": "tracking ended"}
MIN_POSITION_USD = 1.0


def init_schema(db) -> None:
    with db._lock:
        db._conn.executescript(SCHEMA)


def _clamp(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if v != v:          # NaN
        return default
    return max(lo, min(hi, v))


def active_run(db, s=None) -> dict:
    """The current run; the first one is created from the settings."""
    init_schema(db)
    row = db.fetchone("SELECT * FROM paper_runs WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1")
    if row:
        return dict(row)
    return start_run(db, getattr(s, "paper_start_usd", 1000.0), getattr(s, "paper_position_pct", 10.0),
                     getattr(s, "paper_fee_pct", 1.0))


def start_run(db, start_usd, position_pct, fee_pct, now: Optional[float] = None) -> dict:
    """End the current run (its open trades are kept as they are) and start a new one."""
    init_schema(db)
    now = now or time.time()
    start = _clamp(start_usd, 1.0, 1e9, 1000.0)
    pos = _clamp(position_pct, 0.1, 100.0, 10.0)
    fee = _clamp(fee_pct, 0.0, 20.0, 1.0)
    db.execute("UPDATE paper_runs SET ended_at = ? WHERE ended_at IS NULL", (now,))
    db.execute("INSERT INTO paper_runs(started_at, start_usd, position_pct, fee_pct) VALUES (?,?,?,?)",
               (now, start, pos, fee))
    return dict(db.fetchone("SELECT * FROM paper_runs ORDER BY id DESC LIMIT 1"))


def cash(db, run: dict) -> float:
    r = db.fetchone("SELECT COALESCE(SUM(cost_usd), 0) AS c, COALESCE(SUM(proceeds_usd), 0) AS p "
                    "FROM paper_trades WHERE run_id = ?", (run["id"],))
    return float(run["start_usd"]) - float(r["c"]) + float(r["p"])


# ----------------------------------------------------------------------------- the calls to follow
def _plan_from_alert(a: dict, s) -> Optional[dict]:
    if not getattr(s, "trade_plan_enabled", True) or not a.get("entry_mcap"):
        return None
    try:
        values = json.loads(a.get("plan_json") or "null")
    except (TypeError, ValueError):
        values = None
    profile = "scalp" if a.get("call_type") == "RUG_SCALP" else "standard"
    try:
        p = TP.build_plan(a["entry_mcap"], s, profile, values=values)
    except (KeyError, AttributeError, TypeError):
        p = None
    return p.__dict__ if p else None


def calls_since(db, since: float, s) -> list[dict]:
    """Every call posted since `since`, as one shape: ref, mint, symbol, kind, created_at, entry,
    current, plan (dict or None), events (plan kinds the bot has posted), open (still tracked)."""
    out = []
    for a in db.fetchall("SELECT * FROM alerts WHERE status IN ('SENT', 'DRY_RUN') AND created_at >= ?", (since,)):
        a = dict(a)
        try:
            warnings = json.loads(a.get("warnings") or "[]")
        except (TypeError, ValueError):
            warnings = []
        out.append({"ref": f"alert:{a['id']}", "mint": a["mint"], "symbol": a.get("symbol"),
                    "kind": (a.get("call_type") or "STANDARD").lower(), "created_at": a["created_at"],
                    "entry": a.get("entry_mcap"), "current": a.get("current_mcap"), "plan": _plan_from_alert(a, s),
                    "events": [w[5:] for w in warnings if isinstance(w, str) and w.startswith("PLAN:")],
                    "open": a.get("state") != "CLOSED"})
    try:
        rows = db.fetchall("SELECT * FROM early_calls WHERE status IN ('SENT', 'DRY_RUN', 'CLOSED', 'UNTRACKED') "
                           "AND created_at >= ?", (since,))
    except Exception:  # noqa: BLE001 - no early_calls table yet
        rows = []
    for r in rows:
        r = dict(r)
        try:
            report = json.loads(r.get("report") or "{}")
        except (TypeError, ValueError):
            report = {}
        try:
            plan = json.loads(r.get("plan") or "null")
            events = json.loads(r.get("plan_sent") or "[]")
        except (TypeError, ValueError):
            plan, events = None, []
        out.append({"ref": f"early:{r['mint']}", "mint": r["mint"],
                    "symbol": (report.get("identity") or {}).get("symbol"),
                    "kind": "runner" if report.get("runner") else "early", "created_at": r["created_at"],
                    "entry": r.get("entry_mcap"), "current": r.get("current_mcap"), "plan": plan,
                    "events": events, "open": r.get("status") in ("SENT", "DRY_RUN")})
    return sorted(out, key=lambda c: c["created_at"])


# ----------------------------------------------------------------------------- the trader
class PaperTrader:
    def __init__(self, s, db):
        self.s, self.db = s, db

    def _sell(self, t: dict, units: float, mcap: float, kind: str, run: dict, now: float) -> float:
        units = min(units, t["units_left"])
        if units <= 0 or not mcap or mcap <= 0:
            return 0.0
        gross = units * mcap
        net = gross * (1 - float(run["fee_pct"]) / 100)
        t["units_left"] -= units
        t["proceeds_usd"] += net
        t["fills"].append({"kind": kind, "at": now, "mcap": mcap, "multiple": round(mcap / t["entry_mcap"], 4),
                           "sold_pct": round(100 * units / t["units"], 1), "usd": round(net, 2)})
        return net

    def _open(self, c: dict, run: dict, now: float) -> Optional[str]:
        entry = c.get("entry")
        if not entry or entry <= 0:
            return "no entry market cap"
        size = cash(self.db, run) * float(run["position_pct"]) / 100
        if size < MIN_POSITION_USD:
            return f"cash too low for a position (${size:.2f})"
        units = size * (1 - float(run["fee_pct"]) / 100) / entry
        self.db.execute(
            "INSERT OR IGNORE INTO paper_trades(run_id, ref, mint, symbol, kind, opened_at, entry_mcap, cost_usd, units, "
            "units_left, last_mcap, peak_mcap, plan, fills) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run["id"], c["ref"], c["mint"], c.get("symbol"), c.get("kind"), now, entry, size, units, units, entry,
             entry, json.dumps(c.get("plan")) if c.get("plan") else None,
             json.dumps([{"kind": "BUY", "at": now, "mcap": entry, "multiple": 1.0, "usd": round(size, 2)}])))
        log.info("paper: bought $%s for $%.2f at $%.0f market cap (%s call)", c.get("symbol") or c["mint"][:6], size,
                 entry, c.get("kind"))
        return None

    def _follow(self, t: dict, c: Optional[dict], run: dict, now: float) -> None:
        t = {**t, "fills": json.loads(t["fills"] or "[]"), "done": set(json.loads(t["done"] or "[]"))}
        mcap = (c or {}).get("current") or t["last_mcap"] or t["entry_mcap"]
        t["last_mcap"] = mcap
        t["peak_mcap"] = max(t["peak_mcap"] or 0, mcap)
        plan = json.loads(t["plan"]) if t.get("plan") else None
        for kind in [k for k in SELL_KINDS if k in set((c or {}).get("events") or [])] if c else []:
            if kind in t["done"] or t["units_left"] <= 0:
                continue
            t["done"].add(kind)
            if kind == "TP1" and plan:
                self._sell(t, t["units"] * plan["tp1_sell_pct"] / 100, mcap, kind, run, now)
            elif kind == "TP2" and plan:
                share = 100 if TP.sells_everything(TP.Plan(**plan)) else plan["tp2_sell_pct"]
                self._sell(t, t["units"] * share / 100, mcap, kind, run, now)
            elif kind in TP.EXIT_KINDS:
                self._sell(t, t["units_left"], mcap, kind, run, now)
                t["exit_reason"] = kind
        if t["units_left"] > 0 and (c is None or not c.get("open")):
            self._sell(t, t["units_left"], mcap, "END", run, now)
            t["exit_reason"] = t.get("exit_reason") or "END"
        if t["units_left"] <= t["units"] * 1e-9:
            t["units_left"] = 0.0
            t["status"] = "CLOSED"
            t["closed_at"] = t.get("closed_at") or now
            t["exit_reason"] = t.get("exit_reason") or (t["fills"][-1]["kind"] if t["fills"] else "END")
        self.db.execute("UPDATE paper_trades SET units_left=?, proceeds_usd=?, last_mcap=?, peak_mcap=?, done=?, "
                        "fills=?, status=?, exit_reason=?, closed_at=? WHERE id=?",
                        (t["units_left"], t["proceeds_usd"], t["last_mcap"], t["peak_mcap"],
                         json.dumps(sorted(t["done"])), json.dumps(t["fills"][-40:]), t["status"], t.get("exit_reason"),
                         t.get("closed_at"), t["id"]))
        if t["status"] == "CLOSED":
            pnl = t["proceeds_usd"] - t["cost_usd"]
            log.info("paper: closed $%s (%s): %+.2f USD (%+.0f%%)", t.get("symbol") or t["mint"][:6],
                     REASONS.get(t["exit_reason"], t["exit_reason"]), pnl, 100 * pnl / max(t["cost_usd"], 1e-9))

    def run_once(self, now: Optional[float] = None) -> dict:
        counts = {"opened": 0, "followed": 0, "closed": 0, "skipped": 0}
        if not getattr(self.s, "paper_enabled", True):
            return counts
        now = now or time.time()
        run = active_run(self.db, self.s)
        calls = {c["ref"]: c for c in calls_since(self.db, run["started_at"], self.s)}
        have = {r["ref"] for r in self.db.fetchall("SELECT ref FROM paper_trades WHERE run_id = ?", (run["id"],))}
        for ref, c in calls.items():
            if ref in have:
                continue
            why = self._open(c, run, now)
            if why:
                counts["skipped"] += 1
                log.info("paper: skipped %s: %s", c.get("symbol") or c["mint"][:6], why)
                # remember it, so the skip is shown and it is not retried every cycle
                self.db.execute("INSERT OR IGNORE INTO paper_trades(run_id, ref, mint, symbol, kind, opened_at, "
                                "entry_mcap, cost_usd, units, units_left, status, exit_reason, closed_at) "
                                "VALUES (?,?,?,?,?,?,?,0,0,0,'SKIPPED',?,?)",
                                (run["id"], ref, c["mint"], c.get("symbol"), c.get("kind"), now,
                                 c.get("entry") or 0, why[:120], now))
            else:
                counts["opened"] += 1
        for t in self.db.fetchall("SELECT * FROM paper_trades WHERE run_id = ? AND status = 'OPEN'", (run["id"],)):
            t = dict(t)
            self._follow(t, calls.get(t["ref"]), run, now)
            counts["followed"] += 1
            if self.db.fetchone("SELECT status FROM paper_trades WHERE id = ?", (t["id"],))["status"] == "CLOSED":
                counts["closed"] += 1
        return counts


# ----------------------------------------------------------------------------- reporting
def summary(db, s=None, now: Optional[float] = None, trades_limit: int = 60, run: Optional[dict] = None) -> dict:
    """Account, open positions and trade history for the current run (and a list of past runs)."""
    now = now or time.time()
    run = run or active_run(db, s)
    rows = [dict(r) for r in db.fetchall("SELECT * FROM paper_trades WHERE run_id = ? ORDER BY opened_at DESC",
                                         (run["id"],))]
    cash_now = cash(db, run)
    trades, open_value, closed = [], 0.0, []
    for r in rows:
        value = (r["units_left"] or 0) * (r["last_mcap"] or 0) * (1 - float(run["fee_pct"]) / 100)
        if r["status"] == "OPEN":
            open_value += value
        pnl = None if r["status"] == "SKIPPED" else r["proceeds_usd"] + value - r["cost_usd"]
        if r["status"] == "CLOSED":
            closed.append(pnl)
        trades.append({"mint": r["mint"], "symbol": r["symbol"], "kind": r["kind"], "status": r["status"],
                       "opened_at": r["opened_at"], "closed_at": r["closed_at"], "entry_mcap": r["entry_mcap"],
                       "last_mcap": r["last_mcap"], "peak_x": (r["peak_mcap"] or 0) / r["entry_mcap"]
                       if r["entry_mcap"] else None, "cost": r["cost_usd"], "proceeds": r["proceeds_usd"],
                       "open_value": value if r["status"] == "OPEN" else 0.0, "pnl": pnl,
                       "pnl_pct": 100 * pnl / r["cost_usd"] if pnl is not None and r["cost_usd"] else None,
                       "exit": REASONS.get(r["exit_reason"] or "", r["exit_reason"]),
                       "fills": json.loads(r["fills"] or "[]")})
    equity = cash_now + open_value
    wins = [p for p in closed if p > 0]
    past = [dict(r) for r in db.fetchall("SELECT * FROM paper_runs WHERE id != ? ORDER BY id DESC LIMIT 10",
                                         (run["id"],))]
    for p in past:
        p["end_cash"] = round(cash(db, p), 2)
        p["trades"] = db.fetchone("SELECT COUNT(*) AS n FROM paper_trades WHERE run_id = ? AND status != 'SKIPPED'",
                                  (p["id"],))["n"]
    return {"run": run, "cash": round(cash_now, 2), "open_value": round(open_value, 2), "equity": round(equity, 2),
            "pnl": round(equity - run["start_usd"], 2),
            "pnl_pct": round(100 * (equity / run["start_usd"] - 1), 2) if run["start_usd"] else None,
            "open": sum(1 for t in trades if t["status"] == "OPEN"), "closed": len(closed), "wins": len(wins),
            "realized": round(sum(closed), 2) if closed else 0.0,
            "best": max(closed) if closed else None, "worst": min(closed) if closed else None,
            "skipped": sum(1 for t in trades if t["status"] == "SKIPPED"),
            "trades": trades[:trades_limit], "past_runs": past, "now": now}
