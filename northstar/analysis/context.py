"""Context signals: what is happening AROUND a coin, not inside it.

Found by testing ideas against 731 real tracked coins (scripts/feature_lab.py):

  * ticker crowding  - how many tokens with the same ticker launched in the last 6 hours. Copied
                       tickers are a hot meta: coins whose ticker had been copied ran (2x+) 17% of
                       the time, coins with a unique ticker 1%. Biggest single new signal.
  * community takeover - DexScreener lists a CTO for the token (community revived it).
  * market heat      - median 1-hour price change of the coins Northstar researched in the last
                       30 minutes (is the whole memecoin market running or bleeding?).

Everything is computed from data the bot already stores, using only what was known at that moment,
so the same numbers can be rebuilt for past decisions (the brain learns from history on day one).
"""
from __future__ import annotations

import json
import statistics
import time
from collections import deque
from typing import Optional

CROWD_WINDOW = 6 * 3600


def ticker_crowding(db, symbol: Optional[str], ts: float, mint: Optional[str] = None) -> Optional[int]:
    """Other tokens with the same ticker first seen in the 6 hours before ``ts``."""
    if not symbol:
        return None
    try:
        row = db.fetchone("SELECT COUNT(*) n FROM candidates WHERE symbol = ? COLLATE NOCASE AND discovered_at BETWEEN ? AND ? "
                          "AND mint != ?", (symbol.strip(), ts - CROWD_WINDOW, ts, mint or ""))
    except Exception:  # noqa: BLE001 - optional signal
        return None
    return int((row or {}).get("n") or 0)


def has_cto(sources) -> bool:
    if isinstance(sources, str):
        try:
            sources = json.loads(sources)
        except ValueError:
            sources = [sources]
    return "dexscreener_cto" in (sources or [])


class Heat:
    """Rolling median of 1-hour price change across recently researched coins."""

    def __init__(self, window: float = 1800, maxlen: int = 400):
        self.window = window
        self.items: deque = deque(maxlen=maxlen)

    def add(self, report: dict, ts: Optional[float] = None) -> None:
        pc = ((report.get("market") or {}).get("price_change") or {}).get("h1")
        try:
            v = float(pc)
        except (TypeError, ValueError):
            return
        self.items.append((ts or time.time(), max(-90.0, min(200.0, v))))

    def value(self, now: Optional[float] = None) -> Optional[float]:
        now = now or time.time()
        vals = [v for t, v in self.items if now - self.window <= t <= now]
        return round(statistics.median(vals), 2) if len(vals) >= 5 else None


def build(db, mint: str, report: dict, cand: Optional[dict], ts: Optional[float] = None,
          heat: Optional[Heat] = None) -> dict:
    ts = ts or time.time()
    ident = report.get("identity") or {}
    symbol = ident.get("symbol") or (cand or {}).get("symbol")
    crowd = ticker_crowding(db, symbol, ts, mint)
    out = {"ticker_crowding_6h": crowd, "cto": has_cto((cand or {}).get("sources")),
           "market_heat_h1": heat.value(ts) if heat else None}
    if crowd:
        out["line"] = (f"🔥 Hot ticker: {crowd} other ${symbol} launch{'es' if crowd != 1 else ''} in the last 6 h"
                       if crowd >= 2 else None)
    return out
