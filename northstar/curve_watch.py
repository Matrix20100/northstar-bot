"""What actually happens to pump.fun coins in the early band (v2.6.2).

The bot never measured outcomes for coins still on the bonding curve: decision tracking needs a
DEX pool, so every curve-stage rejection went unscored. That is the stage where call bots such as
"Nexas Alerts" post (age under 30 min, market cap $14K-$60K, real 5-minute volume), and it was
the stage where Northstar's own rules contradicted each other. The curve radar looks at
$24K-$50K, while the "already +400% since launch" rule rejects nearly every coin above about $25K.

The curve radar already fetches every recent pump.fun launch from DexScreener every couple of
minutes. This module records, at no extra API cost:
  * the moment a coin first enters the band (`band_lo`-`band_hi` market cap, still on its curve),
    with the numbers a rule can use then: age, market cap, liquidity, 5-minute volume, buys,
    sells and price change;
  * its highest and lowest market cap over the next `watch_hours` (curve or migrated pool).

`evaluate()` then scores any entry rule on real outcomes: how many coins it would have called,
and how many reached 1.5x / 2x / 3x, or fell to 0.3x. One compact row per coin.
"""
from __future__ import annotations

import logging
import statistics
import time
from typing import Callable, Optional

log = logging.getLogger("northstar.curve_watch")

PUMP_DEXES = ("pumpfun", "pump.fun", "pump")

SCHEMA = """
CREATE TABLE IF NOT EXISTS curve_watch (
    mint TEXT PRIMARY KEY,
    symbol TEXT,
    entered_at REAL NOT NULL,
    age_min REAL,
    entry_mc REAL NOT NULL,
    entry_liq REAL,
    vol_m5 REAL,
    buys_m5 INTEGER,
    sells_m5 INTEGER,
    chg_m5 REAL,
    chg_h1 REAL,
    peak_mc REAL,
    peak_at REAL,
    low_mc REAL,
    low_before_peak REAL,
    last_mc REAL,
    last_ts REAL,
    migrated INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_curve_watch_open ON curve_watch(done, entered_at);
"""


def init_schema(db) -> None:
    with db._lock:
        db._conn.executescript(SCHEMA)


def open_mints(db, now: float, watch_hours: float) -> list[str]:
    """Coins still inside their outcome window (they stay in the radar's batch until then)."""
    init_schema(db)
    return [r["mint"] for r in db.fetchall(
        "SELECT mint FROM curve_watch WHERE done = 0 AND entered_at > ? ORDER BY entered_at DESC LIMIT 1500",
        (now - watch_hours * 3600,))]


def observe(db, pairs_by_mint: dict, now: float, s) -> dict:
    """Fold one radar pass into the table. `pairs_by_mint`: mint -> list of PairInfo. Never raises."""
    lo = float(getattr(s, "curve_watch_band_lo", 10_000.0))
    hi = float(getattr(s, "curve_watch_band_hi", 80_000.0))
    hours = float(getattr(s, "curve_watch_hours", 4.0))
    out = {"entered": 0, "updated": 0, "closed": 0}
    try:
        init_schema(db)
        rows = {r["mint"]: r for r in db.fetchall(
            "SELECT mint, entered_at, peak_mc, low_mc, low_before_peak FROM curve_watch WHERE done = 0")}
        for mint, pairs in pairs_by_mint.items():
            caps = [(p.market_cap or p.fdv) for p in pairs if (p.market_cap or p.fdv)]
            if not caps:
                continue
            curve = [p for p in pairs if (p.dex_id or "").lower() in PUMP_DEXES]
            migrated = bool(pairs) and not curve
            # the coin's market cap now: the curve while it trades there, else its deepest pool
            main = curve[0] if curve else max(pairs, key=lambda p: p.liquidity_usd or 0)
            mc = main.market_cap or main.fdv or max(caps)
            row = rows.get(mint)
            if row is None:
                if not curve or not (lo <= mc <= hi):
                    continue
                if db.fetchone("SELECT 1 AS x FROM curve_watch WHERE mint = ?", (mint,)):
                    continue                                  # already scored once
                t5 = (main.txns or {}).get("m5") or {}
                age = (now - main.pair_created_at) / 60 if main.pair_created_at else None
                db.execute(
                    "INSERT INTO curve_watch(mint, symbol, entered_at, age_min, entry_mc, entry_liq, vol_m5, buys_m5, "
                    "sells_m5, chg_m5, chg_h1, peak_mc, peak_at, low_mc, low_before_peak, last_mc, last_ts) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mint, main.base_symbol, now, round(age, 1) if age is not None else None, mc, main.liquidity_usd,
                     (main.vol or {}).get("m5"), t5.get("buys"), t5.get("sells"),
                     (main.price_change or {}).get("m5"), (main.price_change or {}).get("h1"),
                     mc, now, mc, mc, mc, now))
                out["entered"] += 1
                continue
            peak, low = row["peak_mc"] or mc, row["low_mc"] or mc
            new_peak = max(peak, mc)
            low_before = row["low_before_peak"] or mc
            if mc > peak:
                low_before = min(low_before, low)            # the worst drawdown suffered before this new high
            db.execute("UPDATE curve_watch SET peak_mc = ?, peak_at = CASE WHEN ? > COALESCE(peak_mc, 0) THEN ? "
                       "ELSE peak_at END, low_mc = ?, low_before_peak = ?, last_mc = ?, last_ts = ?, "
                       "migrated = MAX(migrated, ?) WHERE mint = ?",
                       (new_peak, mc, now, min(low, mc), low_before, mc, now, 1 if migrated else 0, mint))
            out["updated"] += 1
        with db._lock:
            out["closed"] = db._conn.execute("UPDATE curve_watch SET done = 1 WHERE done = 0 AND entered_at < ?",
                                             (now - hours * 3600,)).rowcount
    except Exception:  # noqa: BLE001 - measurement must never break discovery
        log.warning("curve watch update failed", exc_info=True)
    return out


def prune(db, now: Optional[float] = None, days: float = 21.0) -> int:
    now = now or time.time()
    try:
        init_schema(db)
        with db._lock:
            return db._conn.execute("DELETE FROM curve_watch WHERE done = 1 AND entered_at < ?",
                                    (now - days * 86400,)).rowcount
    except Exception:  # noqa: BLE001
        return 0


# ----------------------------------------------------------------------------- rules
def _n(x, default=0.0):
    return default if x is None else x


RULES: dict[str, tuple[str, Callable[[dict], bool]]] = {
    "every coin in the band": ("every pump.fun coin entering $10K-$80K on its curve", lambda r: True),
    "nexas clone": ("age 30 min or less, $14K-$60K, 5-min volume $7.5K+ (what Nexas Alerts posts)",
                    lambda r: _n(r["age_min"], 999) <= 30 and 14_000 <= r["entry_mc"] <= 60_000
                    and _n(r["vol_m5"]) >= 7_500),
    "nexas + more buys than sells": ("nexas clone, and more buys than sells in 5 min",
                                     lambda r: _n(r["age_min"], 999) <= 30 and 14_000 <= r["entry_mc"] <= 60_000
                                     and _n(r["vol_m5"]) >= 7_500 and _n(r["buys_m5"]) > _n(r["sells_m5"])),
    "final stretch $40K+": ("$40K-$80K on the curve (close to migrating)", lambda r: r["entry_mc"] >= 40_000),
    "northstar curve radar": ("$24K-$50K, 12+ buys and more buys than sells in 5 min (today's radar)",
                              lambda r: 24_000 <= r["entry_mc"] <= 50_000 and _n(r["buys_m5"]) >= 12
                              and _n(r["buys_m5"]) > _n(r["sells_m5"])),
    "heavy volume $20K+ in 5 min": ("5-min volume of $20K or more", lambda r: _n(r["vol_m5"]) >= 20_000),
}


def _rates(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    peaks = [r["peak_mc"] / r["entry_mc"] for r in rows]
    lows = [r["low_mc"] / r["entry_mc"] for r in rows]
    first_lows = [(r["low_before_peak"] or r["entry_mc"]) / r["entry_mc"] for r in rows]

    def pct(xs):
        return round(100 * sum(xs) / n, 1)

    return {"n": n,
            "hit_1_5x": pct([p >= 1.5 for p in peaks]),
            "hit_2x": pct([p >= 2 for p in peaks]),
            "hit_3x": pct([p >= 3 for p in peaks]),
            # a 2x reached without first dropping to 0.6x: something a holder could actually take
            "clean_2x": pct([p >= 2 and fl > 0.6 for p, fl in zip(peaks, first_lows)]),
            "fell_to_0_3x": pct([lo <= 0.3 for lo in lows]),
            "migrated": pct([bool(r["migrated"]) for r in rows]),
            "median_peak": round(statistics.median(peaks), 2)}


def evaluate(db, days: float = 7.0, now: Optional[float] = None) -> dict:
    now = now or time.time()
    rows = db.fetchall("SELECT * FROM curve_watch WHERE done = 1 AND entered_at > ?", (now - days * 86400,))
    pending = db.fetchone("SELECT COUNT(*) AS n FROM curve_watch WHERE done = 0")["n"]
    out = {"days": days, "scored": len(rows), "pending": pending, "rules": {}}
    for name, (desc, rule) in RULES.items():
        out["rules"][name] = {"rule": desc, **_rates([r for r in rows if rule(r)])}
    return out


def format_report(ev: dict) -> str:
    lines = [f"Curve band outcomes, last {ev['days']:g} days: {ev['scored']} coins scored, {ev['pending']} still "
             "inside their window", "",
             f"{'rule':34} {'coins':>6} {'1.5x':>6} {'2x':>6} {'3x':>6} {'clean2x':>8} {'0.3x':>6} {'migr':>6}"]
    for name, r in ev["rules"].items():
        if not r.get("n"):
            lines.append(f"{name:34} {0:>6}")
            continue
        lines.append(f"{name:34} {r['n']:>6} {r['hit_1_5x']:>5}% {r['hit_2x']:>5}% {r['hit_3x']:>5}% "
                     f"{r['clean_2x']:>7}% {r['fell_to_0_3x']:>5}% {r['migrated']:>5}%")
    lines += ["", "1.5x/2x/3x: the market cap reached that multiple of the entry within the window (a peak, not a "
              "sell price).", "clean2x: reached 2x without first falling to 0.6x. 0.3x: fell to 30% of the entry."]
    return "\n".join(lines)
