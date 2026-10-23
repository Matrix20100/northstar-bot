"""Where your tracked wallets' money is flowing, live (v3.5.0).

Before this, the 1,436 tracked wallets (data/tracked_wallets.json, an Axiom export) were only
checked while a coin was being researched: "do any of them hold it?". They could not FIND a coin.
Now every pump.fun / PumpSwap trade those wallets make is streamed from PumpPortal
(`subscribeAccountTrade`) and turned into flow:

  * every buy and sell by a tracked wallet is stored (`wallet_trades`, kept 3 days);
  * per coin, over the last `wallet_flow_window_min` minutes: how many DIFFERENT tracked wallets
    bought, how much SOL they put in, how much they took out;
  * when `wallet_flow_min_buyers` different tracked wallets buy the same coin inside the window, or
    one of them buys `wallet_flow_big_buy_sol`+ SOL in a single trade, the coin is researched at
    once (it skips the queue, like a radar surge). Every safety check still applies;
  * the flow counts in the attention score (attention.py) and is shown on the dashboard.

Cost and noise control (PumpPortal meters this stream: 0.01 SOL per 10,000 messages, and needs an
API key whose linked wallet holds at least 0.02 SOL):
  * a wallet that trades more than `wallet_bot_trades_per_hour` times in an hour trades like a bot:
    it is muted (unsubscribed) for the day. Bot wallets add cost and no signal;
  * after `wallet_watch_max_events_day` messages in a UTC day the stream is paused until midnight.

Nothing here buys or sells anything.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Optional

log = logging.getLogger("northstar.wallet_flow")

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    wallet TEXT NOT NULL,
    mint TEXT NOT NULL,
    side TEXT NOT NULL,
    sol REAL,
    mcap_sol REAL,
    pool TEXT,
    symbol TEXT
);
CREATE INDEX IF NOT EXISTS ix_wallet_trades_mint ON wallet_trades(mint, ts);
CREATE INDEX IF NOT EXISTS ix_wallet_trades_ts ON wallet_trades(ts);
"""


def init_schema(db) -> None:
    with db._lock:
        db._conn.executescript(SCHEMA)


def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class WalletFlow:
    def __init__(self, s, db, wallets: Optional[list] = None):
        self.s, self.db = s, db
        self.names: dict[str, str] = {}
        self.set_wallets(wallets or [])
        self._recent: dict[str, deque] = {}          # wallet -> trade times in the last hour
        self.muted: set[str] = set()
        self._triggered: dict[str, float] = {}       # mint -> last trigger time
        self._day = time.strftime("%Y-%m-%d", time.gmtime())
        self.events_today = 0
        self.paused = False
        init_schema(db)

    def set_wallets(self, wallets: list) -> None:
        self.names = {w.address: (w.name or w.address[:6]) for w in wallets}

    def keys(self) -> list[str]:
        return [w for w in self.names if w not in self.muted]

    def enabled(self) -> bool:
        return bool(getattr(self.s, "wallet_watch_enabled", True) and self.names)

    # ------------------------------------------------------------------ budget
    def _roll_day(self, now: float) -> bool:
        """Start a new UTC day: reset the counters, unmute, unpause. True when the day changed."""
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if day == self._day:
            return False
        self._day, self.events_today, self.paused = day, 0, False
        self.muted.clear()
        return True

    # ------------------------------------------------------------------ events
    def observe(self, msg: dict, now: Optional[float] = None) -> Optional[dict]:
        """One trade event. Returns {"mint", "why", ...} when the coin should be researched now."""
        now = now or time.time()
        self._roll_day(now)
        wallet = str(msg.get("traderPublicKey") or "")
        if wallet not in self.names:
            return None
        self.events_today += 1
        cap = int(getattr(self.s, "wallet_watch_max_events_day", 30000) or 0)
        if cap and self.events_today >= cap and not self.paused:
            self.paused = True
            log.warning("wallet watch: %d messages today, the daily budget; paused until 00:00 UTC "
                        "(NORTHSTAR_WALLET_WATCH_MAX_EVENTS_DAY)", self.events_today)
        q = self._recent.setdefault(wallet, deque())
        q.append(now)
        while q and now - q[0] > 3600:
            q.popleft()
        limit = int(getattr(self.s, "wallet_bot_trades_per_hour", 60) or 0)
        if limit and len(q) > limit and wallet not in self.muted:
            self.muted.add(wallet)
            log.info("wallet watch: muted %s (%s) for today: %d trades in an hour trades like a bot",
                     self.names.get(wallet), wallet, len(q))
            return None
        if wallet in self.muted:
            return None
        side = str(msg.get("txType") or "").lower()
        if side not in ("buy", "sell"):
            return None
        mint = str(msg.get("mint") or "")
        sol = _f(msg.get("solAmount"))
        self.db.execute("INSERT INTO wallet_trades(ts, wallet, mint, side, sol, mcap_sol, pool, symbol) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (now, wallet, mint, side, sol, _f(msg.get("marketCapSol")), msg.get("pool"),
                         msg.get("symbol")))
        if side != "buy":
            return None
        f = self.flow(mint, now)
        big = float(getattr(self.s, "wallet_flow_big_buy_sol", 5.0) or 0)
        need = int(getattr(self.s, "wallet_flow_min_buyers", 2) or 2)
        why = None
        if f["buyers"] >= need:
            why = (f"{f['buyers']} tracked wallets bought it in {f['window_min']:.0f} min "
                   f"({f['buy_sol']:.1f} SOL in, {f['sell_sol']:.1f} out): " + ", ".join(f["names"][:4]))
        elif big and sol and sol >= big:
            why = f"tracked wallet {self.names.get(wallet)} bought {sol:.1f} SOL in one trade"
        if not why or now - self._triggered.get(mint, 0) < 600:
            return None
        self._triggered[mint] = now
        return {"mint": mint, "why": why, **f}

    # ------------------------------------------------------------------ reading
    def flow(self, mint: str, now: Optional[float] = None, window_min: Optional[float] = None) -> dict:
        now = now or time.time()
        window_min = float(window_min or getattr(self.s, "wallet_flow_window_min", 30.0))
        rows = self.db.fetchall("SELECT wallet, side, sol FROM wallet_trades WHERE mint = ? AND ts > ?",
                                (mint, now - window_min * 60))
        buyers = sorted({r["wallet"] for r in rows if r["side"] == "buy"})
        return {"buyers": len(buyers), "names": [self.names.get(w, w[:6]) for w in buyers],
                "buy_sol": round(sum(r["sol"] or 0 for r in rows if r["side"] == "buy"), 3),
                "sell_sol": round(sum(r["sol"] or 0 for r in rows if r["side"] == "sell"), 3),
                "window_min": window_min}

    def top(self, now: Optional[float] = None, window_min: float = 60.0, limit: int = 15) -> list[dict]:
        """Coins with the most tracked-wallet buying in the window (the dashboard's money-flow card)."""
        now = now or time.time()
        rows = self.db.fetchall(
            "SELECT mint, MAX(symbol) AS symbol, COUNT(DISTINCT CASE WHEN side='buy' THEN wallet END) AS buyers, "
            "COUNT(DISTINCT CASE WHEN side='sell' THEN wallet END) AS sellers, "
            "SUM(CASE WHEN side='buy' THEN COALESCE(sol,0) ELSE 0 END) AS buy_sol, "
            "SUM(CASE WHEN side='sell' THEN COALESCE(sol,0) ELSE 0 END) AS sell_sol, MAX(ts) AS last_ts, "
            "MAX(mcap_sol) AS mcap_sol FROM wallet_trades WHERE ts > ? GROUP BY mint "
            "ORDER BY buyers DESC, buy_sol DESC LIMIT ?", (now - window_min * 60, limit))
        return [{**dict(r), "buy_sol": round(r["buy_sol"] or 0, 3), "sell_sol": round(r["sell_sol"] or 0, 3),
                 "net_sol": round((r["buy_sol"] or 0) - (r["sell_sol"] or 0), 3)} for r in rows]

    def status(self) -> dict:
        return {"wallets": len(self.names), "watching": len(self.keys()) if not self.paused else 0,
                "muted": len(self.muted), "events_today": self.events_today, "paused": self.paused,
                "budget": int(getattr(self.s, "wallet_watch_max_events_day", 30000) or 0)}


def prune(db, now: Optional[float] = None, days: float = 3.0) -> int:
    now = now or time.time()
    try:
        init_schema(db)
        with db._lock:
            return db._conn.execute("DELETE FROM wallet_trades WHERE ts < ?", (now - days * 86400,)).rowcount
    except Exception:  # noqa: BLE001
        return 0


def view_top(db, now: float, window_min: float = 60.0, limit: int = 15) -> list[dict]:
    """Read-only version of WalletFlow.top for the dashboard (no wallet names needed)."""
    rows = db.fetchall(
        "SELECT mint, MAX(symbol) AS symbol, COUNT(DISTINCT CASE WHEN side='buy' THEN wallet END) AS buyers, "
        "COUNT(DISTINCT CASE WHEN side='sell' THEN wallet END) AS sellers, "
        "SUM(CASE WHEN side='buy' THEN COALESCE(sol,0) ELSE 0 END) AS buy_sol, "
        "SUM(CASE WHEN side='sell' THEN COALESCE(sol,0) ELSE 0 END) AS sell_sol, MAX(ts) AS last_ts "
        "FROM wallet_trades WHERE ts > ? GROUP BY mint ORDER BY buyers DESC, buy_sol DESC LIMIT ?",
        (now - window_min * 60, limit))
    return [{**dict(r), "buy_sol": round(r["buy_sol"] or 0, 3), "sell_sol": round(r["sell_sol"] or 0, 3),
             "net_sol": round((r["buy_sol"] or 0) - (r["sell_sol"] or 0), 3)} for r in rows]
