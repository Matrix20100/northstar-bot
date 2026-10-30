"""Daily recap (v5.2.0). Operator: "make a recap system that posts in the free channel every day of all the
multipliers".

Once a day, at `daily_recap_hour_utc` (19:00 UTC = 21:00 South Africa), one post lists every call made in the last
24 hours with its peak multiple since the call: runner/early calls (`early_calls`) and standard calls (`alerts`;
rug scalps are left out, they have their own channel). Every call is listed, best first, not only the winners, so
the recap can be trusted. Peaks come from the tracked prices, which v5.1.3 checks before they move a peak.

v5.2.1: before a call is listed at 2x or more, its peak is checked against GeckoTerminal's 1-minute candles (the
highest price since the call / the price at the call), a source independent of the bot's own price reads. A stored
peak more than 25% above the chart's is corrected (here and in the database) and logged. A peak the chart could
not confirm is marked with *.

It goes to `discord_recap_channel_id`, or the free channel when that is not set. It is posted once per day (the day
is remembered in the database, so a restart does not post it twice). A day without calls posts nothing.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

log = logging.getLogger("northstar.recap")

META_KEY = "daily_recap_last_day"
DAY = 86400.0
MAX_DESC = 3900                       # Discord allows 4096 characters per embed description
COLOR = 0x9B59B6


def _money(v: Optional[float]) -> str:
    if not v:
        return "?"
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:,.0f}"


def calls_between(db, start: float, end: float) -> list[dict]:
    """Every call posted in [start, end): symbol, mint, entry, peak, peak multiple, kind."""
    out = []
    try:
        rows = db.fetchall("SELECT mint, created_at, entry_mcap, peak_mcap, current_mcap, report, status, phase FROM early_calls "
                           "WHERE created_at >= ? AND created_at < ? AND status IN ('SENT','DRY_RUN','CLOSED','UNTRACKED')",
                           (start, end))
    except Exception:  # noqa: BLE001 - no early_calls table yet
        rows = []
    for r in rows:
        r = dict(r)
        try:
            ident = (json.loads(r.get("report") or "{}") or {}).get("identity") or {}
        except (TypeError, ValueError):
            ident = {}
        out.append({"mint": r["mint"], "symbol": ident.get("symbol"), "at": r["created_at"], "entry": r["entry_mcap"],
                    "peak": max(r.get("peak_mcap") or 0, r.get("entry_mcap") or 0), "kind": "runner",
                    "phase": r.get("phase")})
    try:
        rows = db.fetchall("SELECT id, mint, symbol, created_at, entry_mcap, peak_mcap, max_multiple, call_type, pool_address "
                           "FROM alerts "
                           "WHERE created_at >= ? AND created_at < ? AND status IN ('SENT','DRY_RUN')", (start, end))
    except Exception:  # noqa: BLE001
        rows = []
    for r in rows:
        r = dict(r)
        if r.get("call_type") == "RUG_SCALP" or not r.get("entry_mcap"):
            continue
        peak = max(r.get("peak_mcap") or 0, (r.get("max_multiple") or 1) * r["entry_mcap"], r["entry_mcap"])
        out.append({"mint": r["mint"], "symbol": r.get("symbol"), "at": r["created_at"], "entry": r["entry_mcap"],
                    "peak": peak, "kind": "standard", "pool": r.get("pool_address"), "alert_id": r["id"]})
    for c in out:
        c["x"] = c["peak"] / c["entry"] if c.get("entry") else None
    return sorted([c for c in out if c["x"]], key=lambda c: (-c["x"], c["at"]))


def _chart_pool(c: dict) -> Optional[str]:
    """The pool whose candles show this call's whole run: a standard call's pool; a runner call still on its pump.fun
    curve, the curve (after migration the curve's candles stop, so a migrated runner is not chart-checked)."""
    if c.get("kind") == "standard":
        return c.get("pool")
    if c.get("phase") == "CURVE" and c["mint"].endswith("pump"):
        from .analysis.pda import pump_bonding_curve_address
        return pump_bonding_curve_address(c["mint"])
    return None


async def verify_peaks(db, calls: list[dict], cg) -> None:
    """Check every 2x+ peak against the chart; correct a peak the chart does not show (in place and in the db)."""
    for c in calls:
        if c["x"] < 2:
            continue
        pool = _chart_pool(c) if cg is not None else None
        candles = []
        if pool:
            try:
                candles = await cg.candles(pool, c["at"] - 60, token=c["mint"])
            except Exception:  # noqa: BLE001 - optional source: the call is marked unchecked
                candles = []
        after = [k for k in candles if k[0] >= c["at"] - 60]
        if len(after) < 2 or not after[0][1]:
            c["unchecked"] = True
            continue
        chart_x = max(k[2] for k in after) / after[0][1]           # highest high since the call / open at the call
        if c["x"] > chart_x * 1.25:
            log.warning("daily recap: %s ($%s) peak %.2fx is not on the chart (chart %.2fx); corrected", c["mint"],
                        c.get("symbol"), c["x"], chart_x)
            c["x"], c["peak"] = max(chart_x, 1.0), c["entry"] * max(chart_x, 1.0)
            try:
                if c["kind"] == "runner":
                    db.execute("UPDATE early_calls SET peak_mcap = ? WHERE mint = ?", (c["peak"], c["mint"]))
                elif c.get("alert_id"):
                    db.execute("UPDATE alerts SET peak_mcap = ?, max_multiple = ? WHERE id = ?",
                               (c["peak"], c["x"], c["alert_id"]))
            except Exception:  # noqa: BLE001
                log.debug("daily recap: could not store the corrected peak", exc_info=True)
    calls.sort(key=lambda c: (-c["x"], c["at"]))


def _icon(x: float) -> str:
    return "🚀" if x >= 5 else "🔥" if x >= 3 else "📈" if x >= 2 else "✅" if x >= 1.5 else "▫️"


def build(calls: list[dict], day_label: str) -> list[dict]:
    """The recap embeds (more than one only when the list does not fit in one)."""
    n = len(calls)
    two = sum(1 for c in calls if c["x"] >= 2)
    best = next((c for c in calls if not c.get("unchecked")), calls[0])
    head = (f"**{n} call{'s' if n != 1 else ''} in the last 24 hours** · {two} reached 2x or more · "
            f"best **${best.get('symbol') or '?'} {best['x']:.2f}x**\n\n")
    lines = [f"{_icon(c['x'])} **${c.get('symbol') or c['mint'][:6]}** — **{c['x']:.2f}x**{'*' if c.get('unchecked') else ''}"
             f" peak ({_money(c['entry'])} → {_money(c['peak'])})" for c in calls]
    if any(c.get("unchecked") for c in calls):
        lines.append("\n*peak not confirmed by chart data")
    pages, cur = [], head
    for ln in lines:
        if len(cur) + len(ln) + 1 > MAX_DESC:
            pages.append(cur)
            cur = ""
        cur += ln + "\n"
    pages.append(cur)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    embeds = []
    for i, desc in enumerate(pages):
        e = {"description": desc.rstrip(), "color": COLOR, "timestamp": ts,
             "footer": {"text": "Peak since each call, from tracked prices · observed, not guaranteed executable "
                                "profit · not financial advice"}}
        if i == 0:
            e["title"] = f"📊 Daily recap · {day_label}"
            e["author"] = {"name": "Northstar Callouts"}
        else:
            e["title"] = f"📊 Daily recap · {day_label} (continued)"
        embeds.append(e)
    return embeds


def channel(s) -> Optional[int]:
    return getattr(s, "discord_recap_channel_id", None) or getattr(s, "discord_free_channel_id", None)


def due(db, s, now: float) -> Optional[str]:
    """The day label to post now, or None (not the hour yet, already posted today, or switched off)."""
    if not getattr(s, "daily_recap_enabled", True) or not channel(s):
        return None
    t = time.gmtime(now)
    if t.tm_hour < int(getattr(s, "daily_recap_hour_utc", 19)):
        return None
    day = time.strftime("%Y-%m-%d", t)
    try:
        row = db.fetchone("SELECT value FROM meta WHERE key = ?", (META_KEY,))
    except Exception:  # noqa: BLE001
        row = None
    if row and row["value"] == day:
        return None
    return day


async def run_once(db, s, publisher, now: Optional[float] = None, cg=None) -> dict:
    now = now or time.time()
    day = due(db, s, now)
    if day is None:
        return {"posted": 0}
    calls = calls_between(db, now - DAY, now)
    await verify_peaks(db, calls, cg)

    def done() -> None:
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (META_KEY, day))
    if not calls:
        done()
        log.info("daily recap %s: no calls in the last 24 h, nothing posted", day)
        return {"posted": 0, "calls": 0}
    label = time.strftime("%d %b %Y", time.gmtime(now))
    posted = 0
    for e in build(calls, label):
        try:
            await publisher.publish(e, channel(s))
            posted += 1
        except Exception as exc:  # noqa: BLE001 - a failed post is logged; tomorrow's recap still runs
            log.warning("daily recap %s: post failed (tried again in 5 min): %s", day, exc)
            break
    if posted:
        done()                         # a first page out = the day is done (no repeat of it)
    log.info("daily recap %s: %d calls, %d reached 2x+, posted %d message(s)", day, len(calls),
             sum(1 for c in calls if c["x"] >= 2), posted)
    return {"posted": posted, "calls": len(calls)}
