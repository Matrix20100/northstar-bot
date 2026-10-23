"""Paper trading: simulated accounts. No real trades, ever; nothing here signs or sends a transaction.

Two separate accounts, each with its own starting amount (dashboard Paper tab):

`calls` (v3.1.0) follows every call the bot posts
  * Every call posted after the current run started opens one position: `position_pct` of the
    cash left, bought at the call's entry market cap, minus `fee_pct` (fees and slippage).
  * It then does exactly what the call's trade plan tells people to do, at the moment the bot
    posts it and at the market cap the bot tracked then: TP1 / TP2 sell the plan's share;
    TRAIL / STOP / TIME / DANGER sell everything left.
  * When the bot stops tracking the call (window over, rugged, message deleted, untracked),
    whatever is left is sold at the last market cap the bot saw.

`brain` (v3.2.0) trades the learning brain's own picks, not the bot's calls
  * Every coin the bot researches with a DEX pool gets the runner brain's odds (it reaches
    `brain_win_x` before falling to `brain_loss_x`) and the rug brain's odds.
  * The brain buys when its runner odds are at least `paper_brain_min_p` and the rug odds are
    under `paper_brain_max_rug` (when known), at the pool's market cap at that moment. One
    position per coin per run, at most `paper_brain_max_open` open at once.
  * It is followed with the standard trade plan (the same rules a call gets), priced from
    DexScreener every cycle, and closed after `paper_brain_hold_hours` at the last price seen.
  * This shows whether the brain's picks make money BEFORE it is given any say in real calls.

Market cap is used as the price (supply is fixed, so a 2x in market cap is a 2x in price). Fills
are the bot's own observations, one every refresh interval, so a real fill can be better or
worse. Sells also pay `fee_pct`. Every number comes from tracked data; nothing is made up.
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
    ended_at REAL,
    account TEXT NOT NULL DEFAULT 'calls'
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
    note TEXT,
    UNIQUE(run_id, ref)
);
CREATE INDEX IF NOT EXISTS ix_paper_open ON paper_trades(run_id, status);
"""
ACCOUNTS = ("calls", "brain")
SELL_KINDS = ("TP1", "TP2") + tuple(sorted(TP.EXIT_KINDS))
REASONS = {"TP1": "take profit 1", "TP2": "take profit 2", "TRAIL": "trailing stop", "STOP": "stop loss",
           "TIME": "time stop", "DANGER": "exit now (danger)", "END": "tracking ended", "HOLD": "hold time over",
           "DEAD": "coin died ($5K or lower)"}
MIN_POSITION_USD = 1.0
# v3.3.2 sanity rails: a coin is only bought with a believable price, and a price that jumps more than
# MAX_TICK_JUMP x (or falls under 1 / MAX_TICK_JUMP) between two checks 15 s apart is a bad quote,
# not a move. The live brain account once "bought" for $1.35 billion after one such quote.
MIN_ENTRY_MCAP = 5_000.0
MIN_BRAIN_LIQUIDITY = 10_000.0
MAX_TICK_JUMP = 20.0
BRAIN_SKIP_CLASSES = ("PREFILTER", "SAFETY_FAIL")


def init_schema(db) -> None:
    with db._lock:
        db._conn.executescript(SCHEMA)
        for table, col, decl in (("paper_runs", "account", "TEXT NOT NULL DEFAULT 'calls'"),
                                 ("paper_trades", "note", "TEXT"), ("paper_trades", "pool", "TEXT")):
            cols = {r[1] for r in db._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if col not in cols:
                db._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _clamp(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if v != v:          # NaN
        return default
    return max(lo, min(hi, v))


def _account(account) -> str:
    return account if account in ACCOUNTS else "calls"


def active_run(db, s=None, account: str = "calls") -> dict:
    """The account's current run; the first one is created from the settings."""
    init_schema(db)
    account = _account(account)
    row = db.fetchone("SELECT * FROM paper_runs WHERE ended_at IS NULL AND account = ? ORDER BY id DESC LIMIT 1",
                      (account,))
    if row:
        return dict(row)
    return start_run(db, getattr(s, "paper_start_usd", 1000.0), getattr(s, "paper_position_pct", 10.0),
                     getattr(s, "paper_fee_pct", 1.0), account=account)


def start_run(db, start_usd, position_pct, fee_pct, now: Optional[float] = None, account: str = "calls") -> dict:
    """End the account's current run (its trades are kept as they are) and start a new one."""
    init_schema(db)
    now = now or time.time()
    account = _account(account)
    start = _clamp(start_usd, 1.0, 1e9, 1000.0)
    pos = _clamp(position_pct, 0.1, 100.0, 10.0)
    fee = _clamp(fee_pct, 0.0, 20.0, 1.0)
    db.execute("UPDATE paper_runs SET ended_at = ? WHERE ended_at IS NULL AND account = ?", (now, account))
    db.execute("INSERT INTO paper_runs(started_at, start_usd, position_pct, fee_pct, account) VALUES (?,?,?,?,?)",
               (now, start, pos, fee, account))
    return dict(db.fetchone("SELECT * FROM paper_runs ORDER BY id DESC LIMIT 1"))


def cash(db, run: dict) -> float:
    r = db.fetchone("SELECT COALESCE(SUM(cost_usd), 0) AS c, COALESCE(SUM(proceeds_usd), 0) AS p "
                    "FROM paper_trades WHERE run_id = ?", (run["id"],))
    return float(run["start_usd"]) - float(r["c"]) + float(r["p"])


# ----------------------------------------------------------------------------- shared mechanics
def _buy(db, run: dict, ref: str, mint: str, symbol, kind: str, entry: float, plan: Optional[dict], now: float,
         note: Optional[str] = None, pool: Optional[str] = None) -> Optional[str]:
    """Open a position. Returns why it could not, or None."""
    if not entry or entry <= 0:
        return "no entry market cap"
    if entry < MIN_ENTRY_MCAP:
        return f"entry market cap ${entry:,.0f} is not believable (under ${MIN_ENTRY_MCAP:,.0f})"
    size = cash(db, run) * float(run["position_pct"]) / 100
    if size < MIN_POSITION_USD:
        return f"cash too low for a position (${size:.2f})"
    units = size * (1 - float(run["fee_pct"]) / 100) / entry
    db.execute(
        "INSERT OR IGNORE INTO paper_trades(run_id, ref, mint, symbol, kind, opened_at, entry_mcap, cost_usd, units, "
        "units_left, last_mcap, peak_mcap, plan, fills, note, pool) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run["id"], ref, mint, symbol, kind, now, entry, size, units, units, entry, entry,
         json.dumps(plan) if plan else None,
         json.dumps([{"kind": "BUY", "at": now, "mcap": entry, "multiple": 1.0, "usd": round(size, 2)}]), note, pool))
    log.info("paper[%s]: bought $%s for $%.2f at $%.0f market cap", run.get("account", "calls"),
             symbol or mint[:6], size, entry)
    return None


def _skip(db, run: dict, ref: str, mint: str, symbol, kind: str, entry, why: str, now: float) -> None:
    """Remember a skipped call, so the skip is shown and it is not retried every cycle."""
    db.execute("INSERT OR IGNORE INTO paper_trades(run_id, ref, mint, symbol, kind, opened_at, entry_mcap, cost_usd, "
               "units, units_left, status, exit_reason, closed_at) VALUES (?,?,?,?,?,?,?,0,0,0,'SKIPPED',?,?)",
               (run["id"], ref, mint, symbol, kind, now, entry or 0, why[:120], now))


def _execute(db, row: dict, run: dict, mcap: Optional[float], kinds: list[str], end: Optional[str], now: float) -> bool:
    """Apply sell events to an open position and store it. Returns True when it closed."""
    t = {**row, "fills": json.loads(row["fills"] or "[]"), "done": set(json.loads(row["done"] or "[]"))}
    ref_mc = t["last_mcap"] or t["entry_mcap"]
    if mcap and ref_mc and not (ref_mc / MAX_TICK_JUMP <= mcap <= ref_mc * MAX_TICK_JUMP):
        log.info("paper[%s]: ignored a bad quote for %s ($%.0f after $%.0f)", run.get("account", "calls"),
                 t.get("symbol") or t["mint"][:6], mcap, ref_mc)
        mcap, kinds = None, []
    mcap = mcap or t["last_mcap"] or t["entry_mcap"]
    t["last_mcap"] = mcap
    t["peak_mcap"] = max(t["peak_mcap"] or 0, mcap)
    plan = json.loads(t["plan"]) if t.get("plan") else None
    fee = float(run["fee_pct"]) / 100

    def sell(units: float, kind: str) -> None:
        units = min(units, t["units_left"])
        if units <= 0:
            return
        net = units * mcap * (1 - fee)
        t["units_left"] -= units
        t["proceeds_usd"] += net
        t["fills"].append({"kind": kind, "at": now, "mcap": mcap, "multiple": round(mcap / t["entry_mcap"], 4),
                           "sold_pct": round(100 * units / t["units"], 1), "usd": round(net, 2)})

    for kind in [k for k in SELL_KINDS if k in set(kinds)]:
        if kind in t["done"] or t["units_left"] <= 0:
            continue
        t["done"].add(kind)
        if kind == "TP1" and plan:
            sell(t["units"] * plan["tp1_sell_pct"] / 100, kind)
        elif kind == "TP2" and plan:
            share = 100 if TP.sells_everything(TP.Plan(**plan)) else plan["tp2_sell_pct"]
            sell(t["units"] * share / 100, kind)
        elif kind in TP.EXIT_KINDS:
            sell(t["units_left"], kind)
            t["exit_reason"] = kind
    if end and t["units_left"] > 0:
        sell(t["units_left"], end)
        t["exit_reason"] = t.get("exit_reason") or end
    closed = t["units_left"] <= t["units"] * 1e-9
    if closed:
        t["units_left"] = 0.0
        t["status"] = "CLOSED"
        t["closed_at"] = t.get("closed_at") or now
        t["exit_reason"] = t.get("exit_reason") or (t["fills"][-1]["kind"] if t["fills"] else "END")
    db.execute("UPDATE paper_trades SET units_left=?, proceeds_usd=?, last_mcap=?, peak_mcap=?, done=?, fills=?, "
               "status=?, exit_reason=?, closed_at=? WHERE id=?",
               (t["units_left"], t["proceeds_usd"], t["last_mcap"], t["peak_mcap"], json.dumps(sorted(t["done"])),
                json.dumps(t["fills"][-40:]), t["status"], t.get("exit_reason"), t.get("closed_at"), t["id"]))
    if closed:
        pnl = t["proceeds_usd"] - t["cost_usd"]
        log.info("paper[%s]: closed $%s (%s): %+.2f USD (%+.0f%%)", run.get("account", "calls"),
                 t.get("symbol") or t["mint"][:6], REASONS.get(t["exit_reason"], t["exit_reason"]), pnl,
                 100 * pnl / max(t["cost_usd"], 1e-9))
    return closed


# ----------------------------------------------------------------------------- the calls account
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


class PaperTrader:
    """The `calls` account: follows every posted call."""

    def __init__(self, s, db):
        self.s, self.db = s, db

    def run_once(self, now: Optional[float] = None) -> dict:
        counts = {"opened": 0, "followed": 0, "closed": 0, "skipped": 0}
        if not getattr(self.s, "paper_enabled", True):
            return counts
        now = now or time.time()
        run = active_run(self.db, self.s, "calls")
        calls = {c["ref"]: c for c in calls_since(self.db, run["started_at"], self.s)}
        have = {r["ref"] for r in self.db.fetchall("SELECT ref FROM paper_trades WHERE run_id = ?", (run["id"],))}
        for ref, c in calls.items():
            if ref in have:
                continue
            why = _buy(self.db, run, ref, c["mint"], c.get("symbol"), c.get("kind"), c.get("entry"), c.get("plan"), now)
            if why:
                counts["skipped"] += 1
                log.info("paper[calls]: skipped %s: %s", c.get("symbol") or c["mint"][:6], why)
                _skip(self.db, run, ref, c["mint"], c.get("symbol"), c.get("kind"), c.get("entry"), why, now)
            else:
                counts["opened"] += 1
        for t in self.db.fetchall("SELECT * FROM paper_trades WHERE run_id = ? AND status = 'OPEN'", (run["id"],)):
            c = calls.get(t["ref"])
            counts["followed"] += 1
            ev = (c or {}).get("events") or []
            end = "DEAD" if "DEAD" in ev else (None if (c and c.get("open")) else "END")
            if _execute(self.db, dict(t), run, (c or {}).get("current"), ev, end, now):
                counts["closed"] += 1
        return counts


# ----------------------------------------------------------------------------- the brain account
def _money(v: float) -> str:
    return f"${v:,.0f}"


class BrainTrader:
    """The `brain` account: buys the learning brain's own picks and follows them with the standard plan."""

    def __init__(self, s, db, dex=None):
        self.s, self.db, self.dex = s, db, dex

    def enabled(self) -> bool:
        return bool(getattr(self.s, "paper_enabled", True) and getattr(self.s, "paper_brain_enabled", True))

    def consider(self, out, now: Optional[float] = None) -> Optional[str]:
        """Called once per finished research. Returns "bought" or why not (None when the brain gave no odds)."""
        if not self.enabled():
            return None
        r = out.report or {}
        p = (r.get("brain") or {}).get("p")
        pair = getattr(out, "pair", None)
        if p is None or pair is None:
            return None
        need = float(getattr(self.s, "paper_brain_min_p", 0.15))
        if p < need:
            return f"runner odds {p:.0%} under {need:.0%}"
        rug = (r.get("unified") or {}).get("rug_odds") if isinstance(r.get("unified"), dict) else None
        if rug is None:
            rug = (r.get("rug_model") or {}).get("p")
        max_rug = float(getattr(self.s, "paper_brain_max_rug", 0.3))
        if rug is not None and rug >= max_rug:
            return f"rug odds {rug:.0%} at or over {max_rug:.0%}"
        rc = str(((r.get("final") or {}).get("reason_class")) or ((r.get("unified") or {}).get("reason_class") if
                                                                  isinstance(r.get("unified"), dict) else "") or "")
        if rc in BRAIN_SKIP_CLASSES:
            return f"failed {rc.lower().replace('_', ' ')}"
        if (r.get("verdict") or {}).get("level") == "RUG LIKELY":
            return "rug check: RUG LIKELY"
        if (pair.liquidity_usd or 0) < MIN_BRAIN_LIQUIDITY:
            return f"liquidity ${pair.liquidity_usd or 0:,.0f} under ${MIN_BRAIN_LIQUIDITY:,.0f}"
        mc = pair.market_cap or pair.fdv
        if not mc:
            return "no market cap"
        now = now or time.time()
        run = active_run(self.db, self.s, "brain")
        ref = f"brain:{out.mint}"
        if self.db.fetchone("SELECT 1 AS x FROM paper_trades WHERE run_id = ? AND ref = ?", (run["id"], ref)):
            return "already traded this run"
        n_open = self.db.fetchone("SELECT COUNT(*) AS n FROM paper_trades WHERE run_id = ? AND status = 'OPEN'",
                                  (run["id"],))["n"]
        cap = int(getattr(self.s, "paper_brain_max_open", 10))
        if n_open >= cap:
            return f"{cap} positions already open"
        try:
            plan = TP.build_plan(mc, self.s, "standard")
        except (AttributeError, KeyError, TypeError):
            plan = None
        note = f"runner odds {p:.0%}" + (f" · rug odds {rug:.0%}" if rug is not None else "")
        why = _buy(self.db, run, ref, out.mint, pair.base_symbol, "brain", mc, plan.__dict__ if plan else None, now,
                   note=note, pool=pair.pair_address)
        return why or "bought"

    async def _caps(self, mints: list[str]) -> tuple[dict[str, float], dict[str, float]]:
        """(market cap per pool address, market cap of the deepest pool per mint)."""
        if not mints or self.dex is None:
            return {}, {}
        try:
            pairs = await self.dex.tokens_batch(mints)
        except Exception:  # noqa: BLE001 - prices are retried next cycle
            log.info("paper[brain]: price refresh failed", exc_info=True)
            return {}, {}
        best: dict = {}
        by_pool: dict[str, float] = {}
        for p in pairs:
            if not (p.market_cap or p.fdv):
                continue
            if p.pair_address:
                by_pool[p.pair_address] = p.market_cap or p.fdv
            if p.base_mint not in best or (p.liquidity_usd or 0) > (best[p.base_mint].liquidity_usd or 0):
                best[p.base_mint] = p
        return by_pool, {m: (p.market_cap or p.fdv) for m, p in best.items()}

    async def run_once(self, now: Optional[float] = None) -> dict:
        counts = {"followed": 0, "closed": 0, "priced": 0}
        if not self.enabled():
            return counts
        now = now or time.time()
        run = active_run(self.db, self.s, "brain")
        rows = [dict(t) for t in self.db.fetchall("SELECT * FROM paper_trades WHERE run_id = ? AND status = 'OPEN'",
                                                  (run["id"],))]
        if not rows:
            return counts
        by_pool, caps = await self._caps(sorted({t["mint"] for t in rows}))
        hold = float(getattr(self.s, "paper_brain_hold_hours", 24.0)) * 3600
        for t in rows:
            # the pool it was bought in, so a stray pool with a broken price is never used
            mc = by_pool.get(t.get("pool") or "") or caps.get(t["mint"])
            counts["followed"] += 1
            kinds: list[str] = []
            ref_mc = t["last_mcap"] or t["entry_mcap"]
            if mc and ref_mc and not (ref_mc / MAX_TICK_JUMP <= mc <= ref_mc * MAX_TICK_JUMP):
                mc = None                                  # a bad quote: _execute logs and ignores it
                counts["bad_quotes"] = counts.get("bad_quotes", 0) + 1
            if mc:
                counts["priced"] += 1
                plan = json.loads(t["plan"]) if t.get("plan") else None
                if plan:
                    peak = max(t["peak_mcap"] or 0, mc)
                    done = set(json.loads(t["done"] or "[]"))
                    kinds = [k for k, _ in TP.evaluate(TP.Plan(**plan), mc / t["entry_mcap"], peak / t["entry_mcap"],
                                                       now - t["opened_at"], done, _money)]
            end = "HOLD" if now - t["opened_at"] >= hold else None
            from . import brain as B
            if mc and B.is_dead(mc, t["entry_mcap"]):
                end = "DEAD"                               # v4.3.0: the coin died, sell what is left
            if _execute(self.db, t, run, mc, kinds, end, now):
                counts["closed"] += 1
        return counts


def repair(db, s=None, now: Optional[float] = None) -> list[str]:
    """v3.3.2: a run whose numbers cannot be real (a balance or a trade over 1,000x its start, from a
    bad quote before the sanity rails) is closed and a fresh run starts with the same settings.
    The broken run stays in history. Returns the accounts repaired."""
    now = now or time.time()
    fixed = []
    init_schema(db)
    for acct in ACCOUNTS:
        run = db.fetchone("SELECT * FROM paper_runs WHERE ended_at IS NULL AND account = ? ORDER BY id DESC LIMIT 1",
                          (acct,))
        if not run:
            continue
        run = dict(run)
        bad = db.fetchone("SELECT COUNT(*) AS n FROM paper_trades WHERE run_id = ? AND (cost_usd > ? OR "
                          "proceeds_usd > cost_usd * 1000 + 1)", (run["id"], run["start_usd"] * 1.0001))["n"]
        if bad or cash(db, run) > run["start_usd"] * 1000:
            start_run(db, run["start_usd"], run["position_pct"], run["fee_pct"], now=now, account=acct)
            log.warning("paper[%s]: the run had impossible numbers (bad price quote); started a fresh run with the "
                        "same settings. The old run is kept in history.", acct)
            fixed.append(acct)
    return fixed


# ----------------------------------------------------------------------------- reporting
def summary(db, s=None, now: Optional[float] = None, trades_limit: int = 60, run: Optional[dict] = None,
            account: str = "calls") -> dict:
    """Account, open positions and trade history for the current run (and a list of past runs)."""
    now = now or time.time()
    run = run or active_run(db, s, account)
    account = run.get("account") or account
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
                       "exit": REASONS.get(r["exit_reason"] or "", r["exit_reason"]), "note": r.get("note"),
                       "fills": json.loads(r["fills"] or "[]")})
    equity = cash_now + open_value
    wins = [p for p in closed if p > 0]
    past = [dict(r) for r in db.fetchall("SELECT * FROM paper_runs WHERE id != ? AND COALESCE(account, 'calls') = ? "
                                         "ORDER BY id DESC LIMIT 10", (run["id"], account))]
    for p in past:
        p["end_cash"] = round(cash(db, p), 2)
        p["trades"] = db.fetchone("SELECT COUNT(*) AS n FROM paper_trades WHERE run_id = ? AND status != 'SKIPPED'",
                                  (p["id"],))["n"]
    return {"account": account, "run": run, "cash": round(cash_now, 2), "open_value": round(open_value, 2),
            "equity": round(equity, 2), "pnl": round(equity - run["start_usd"], 2),
            "pnl_pct": round(100 * (equity / run["start_usd"] - 1), 2) if run["start_usd"] else None,
            "open": sum(1 for t in trades if t["status"] == "OPEN"), "closed": len(closed), "wins": len(wins),
            "realized": round(sum(closed), 2) if closed else 0.0,
            "best": max(closed) if closed else None, "worst": min(closed) if closed else None,
            "skipped": sum(1 for t in trades if t["status"] == "SKIPPED"),
            "trades": trades[:trades_limit], "past_runs": past, "now": now}
