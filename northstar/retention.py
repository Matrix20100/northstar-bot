"""Database size: what uses the space, and the safe ways to reduce it (v2.5.4).

Measured on the live database copy of 21 Oct 2026 17:22 (394 MB, v2.4.1 era):
  discovery_events  145 MB + 16 MB index  — 74% of rows were exact repeats of an earlier event for
                                           the same mint and feed (DexScreener re-lists boosts and
                                           profiles every minute)
  research_snapshots 103 MB               — 12,561 of 18,618 rows were prefilter rejections whose
                                           report is mostly a provider-metadata market block
  curve_snapshots     55 MB               — kept 7 days, read only for the last 5 minutes

What is never touched here: alerts and their price history, early calls, decision reasons
(`reasons`, `final`, `decision`, gates, unified result), decision_tracking and outcomes (the
runner/rug labels), shadow_calls, shadow_tests and anything the learning brain reads.

Everything here is idempotent and safe to run while the bot is running, except VACUUM, which
needs the bot stopped and free disk space about the size of the database.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any, Optional

log = logging.getLogger("northstar.retention")

DEDUP_FLAG = "discovery_dedup_v1"
COMPACT_FLAG = '"compacted": 1'
# keys a compacted prefilter report keeps (everything any reader of an old prefilter row uses)
PREFILTER_KEEP = ("mint", "ts", "sources", "missing", "identity", "decision", "risk", "final_action",
                  "final", "reasons", "lifecycle", "funnel")


# --------------------------------------------------------------------------- measurement
def table_sizes(path: str) -> list[dict]:
    """Bytes per table/index (dbstat), largest first. Read-only."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC").fetchall()
    except sqlite3.Error:                    # SQLite built without dbstat
        rows = []
    out = []
    for name, size in rows:
        try:
            n = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        except sqlite3.Error:
            n = None                          # an index
        out.append({"name": name, "bytes": size, "rows": n})
    con.close()
    return out


def file_size(path: str) -> int:
    return sum(os.path.getsize(p) for p in (path, path + "-wal") if os.path.exists(p))


# --------------------------------------------------------------------------- one-time migration
def collapse_discovery_duplicates(db, force: bool = False, max_rows: int = 2000) -> int:
    """Merge exact repeats (same mint, feed and payload) into one row with seen/last_ts.

    One-time (meta flag set only after the last chunk). Readers take the first launch event
    (ORDER BY ts ASC) or the latest profile (ORDER BY COALESCE(last_ts, ts) DESC); both are
    unchanged by the merge.

    Works in short transactions of whole coins holding about `max_rows` rows each (one coin can
    exceed it): on the 21 Oct copy a single transaction held the database for 5.3 s and grew the
    WAL file by 146 MB. Each transaction merges complete coins, so a crash or restart mid-way
    double-counts nothing and the next run carries on. Returns rows removed.
    """
    if not force and db.fetchone("SELECT 1 FROM meta WHERE key=?", (DEDUP_FLAG,)):
        return 0
    sizes = db.fetchall("SELECT mint, COUNT(*) n FROM discovery_events WHERE mint IN (SELECT mint FROM "
                        "discovery_events GROUP BY mint, source, payload HAVING COUNT(*) > 1) GROUP BY mint")
    chunks, cur, rows = [], [], 0
    for r in sizes:
        if cur and rows + r["n"] > max_rows:
            chunks.append(cur)
            cur, rows = [], 0
        cur.append(r["mint"])
        rows += r["n"]
    if cur:
        chunks.append(cur)
    removed = 0
    for part in chunks:
        marks = ",".join("?" * len(part))

        def tx(c: sqlite3.Connection, part=part, marks=marks) -> int:
            c.execute("DROP TABLE IF EXISTS temp.disc_dup")
            c.execute(f"CREATE TEMP TABLE disc_dup AS SELECT MIN(id) keep, SUM(seen) n, "
                      f"MAX(COALESCE(last_ts, ts)) last FROM discovery_events WHERE mint IN ({marks}) "
                      f"GROUP BY mint, source, payload HAVING COUNT(*) > 1", part)
            c.execute("UPDATE discovery_events SET "
                      "seen = (SELECT n FROM disc_dup WHERE keep = discovery_events.id), "
                      "last_ts = (SELECT last FROM disc_dup WHERE keep = discovery_events.id) "
                      "WHERE id IN (SELECT keep FROM disc_dup)")
            n = c.execute(f"DELETE FROM discovery_events WHERE mint IN ({marks}) AND id NOT IN "
                          f"(SELECT MIN(id) FROM discovery_events WHERE mint IN ({marks}) "
                          f"GROUP BY mint, source, payload)", part + part).rowcount
            c.execute("DROP TABLE temp.disc_dup")
            return n

        removed += db._tx(tx)
        checkpoint(db, "TRUNCATE")            # keep the WAL file small between chunks
    db.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (DEDUP_FLAG, str(int(time.time()))))
    return removed


def checkpoint(db, mode: str = "PASSIVE") -> None:
    """Copy the WAL back into the database file; TRUNCATE also gives the WAL's disk space back.
    Never raises: a busy reader just means the next checkpoint does it."""
    try:
        db.execute(f"PRAGMA wal_checkpoint({mode})")
    except sqlite3.Error:
        pass


def prune_curve(db, hours: float, now: Optional[float] = None, chunk: int = 5000) -> int:
    """Delete bonding-curve events older than `hours`, in short transactions."""
    if not hours or hours <= 0:
        return 0
    cutoff = (now or time.time()) - hours * 3600
    removed = 0
    while True:
        n = db._tx(lambda c: c.execute(
            "DELETE FROM curve_snapshots WHERE id IN (SELECT id FROM curve_snapshots WHERE ts < ? LIMIT ?)",
            (cutoff, chunk)).rowcount)
        removed += n
        if n < chunk:
            return removed
        checkpoint(db, "TRUNCATE")


# --------------------------------------------------------------------------- compaction
def _slim_value(v: Any) -> Any:
    """{"value": x, "status": ..., "source": ..., "note": ...} -> {"value": x}"""
    if isinstance(v, dict) and "value" in v:
        return {"value": v["value"]}
    if isinstance(v, dict):
        return {k: _slim_value(x) for k, x in v.items()}
    return v


def compact_prefilter_report(r: dict) -> Optional[dict]:
    """A prefilter rejection's report without provider metadata, or None if it is not one."""
    reasons = r.get("reasons") or []
    if not reasons or not str(reasons[0]).startswith("prefilter:") or r.get("compacted"):
        return None
    out = {k: r[k] for k in PREFILTER_KEEP if k in r}
    # every market key stays (brain.vector reads e.g. fetched_at and boosts_active); only the
    # provider metadata around each value (status/source/note) is dropped
    out["market"] = _slim_value(r.get("market") or {})
    pool = r.get("pool") or {}
    out["pool"] = {k: pool[k] for k in ("address", "dex") if k in pool}
    out["compacted"] = 1
    return out


def compact_snapshots(db, older_than_hours: float, now: Optional[float] = None, batch: int = 2000) -> int:
    """Compact prefilter-rejection reports older than the cutoff. Keeps the reasons and decision.

    Never touches the latest snapshot of a coin, a snapshot an alert points to, or anything that
    reached a security check (those reports hold the evidence the audit and the brain read).
    """
    if not older_than_hours or older_than_hours <= 0:
        return 0
    now = now or time.time()
    rows = db.fetchall(
        "SELECT id, report FROM research_snapshots WHERE ts < ? AND action = 'REJECT' "
        "AND score IS NULL AND report LIKE '%\"reasons\": [\"prefilter:%' AND report NOT LIKE ? "
        "AND id NOT IN (SELECT snapshot_id FROM alerts WHERE snapshot_id IS NOT NULL) "
        "AND id NOT IN (SELECT MAX(id) FROM research_snapshots GROUP BY mint) LIMIT ?",
        (now - older_than_hours * 3600, f"%{COMPACT_FLAG}%", batch))
    done = 0
    for row in rows:
        try:
            slim = compact_prefilter_report(json.loads(row["report"]))
        except (TypeError, ValueError):
            continue
        if slim is None:
            continue
        db.execute("UPDATE research_snapshots SET report = ? WHERE id = ?",
                   (json.dumps(slim, default=str), row["id"]))
        done += 1
    return done


# --------------------------------------------------------------------------- one call for the engine
def maintain(db, s: Any, now: Optional[float] = None) -> dict:
    """Retention pass: migration (once), age-based pruning, compaction. Never raises."""
    now = now or time.time()
    out: dict = {}
    for name, fn in (
        ("discovery_duplicates_merged", lambda: collapse_discovery_duplicates(db)),
        ("curve_events_removed", lambda: prune_curve(db, getattr(s, "curve_retention_hours", 6.0), now)),
        ("pruned", lambda: db.prune_old_data(getattr(s, "discovery_retention_days", 4.0),
                                             getattr(s, "snapshot_retention_days", 7.0), now,
                                             curve_retention_hours=getattr(s, "curve_retention_hours", 6.0))),
        ("discovered_expired", lambda: db.expire_discovered(getattr(s, "discovered_max_age_hours", 6.0), now)),
        ("snapshots_compacted", lambda: sum(
            compact_snapshots(db, getattr(s, "snapshot_compact_hours", 24.0), now) for _ in range(5))),
    ):
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001 - housekeeping must never break the bot
            log.warning("retention step %s failed: %s", name, e, exc_info=True)
            out[name] = f"failed: {type(e).__name__}"
    if any(v for k, v in out.items() if isinstance(v, int)):
        checkpoint(db, "TRUNCATE")            # give the WAL's disk space back after a big pass
    return out


def estimate(db, s: Any, now: Optional[float] = None) -> dict:
    """What the retention pass would remove or compact right now. Read-only."""
    now = now or time.time()
    out = {}
    if not db.fetchone("SELECT 1 FROM meta WHERE key=?", (DEDUP_FLAG,)):
        out["discovery_duplicate_rows"] = db.fetchone(
            "SELECT COUNT(*) - (SELECT COUNT(*) FROM (SELECT 1 FROM discovery_events GROUP BY mint, source, payload)) n "
            "FROM discovery_events")["n"]
    d = getattr(s, "discovery_retention_days", 4.0)
    if d and d > 0:
        out["discovery_older_than_retention"] = db.fetchone(
            "SELECT COUNT(*) n FROM discovery_events WHERE COALESCE(last_ts, ts) < ? AND mint NOT IN "
            "(SELECT mint FROM early_signals) AND mint NOT IN (SELECT mint FROM alerts)", (now - d * 86400,))["n"]
    h = getattr(s, "curve_retention_hours", 6.0)
    if h and h > 0:
        out["curve_events_older_than_retention"] = db.fetchone(
            "SELECT COUNT(*) n FROM curve_snapshots WHERE ts < ?", (now - h * 3600,))["n"]
    ch = getattr(s, "snapshot_compact_hours", 24.0)
    if ch and ch > 0:
        out["prefilter_reports_to_compact"] = db.fetchone(
            "SELECT COUNT(*) n FROM research_snapshots WHERE ts < ? AND action='REJECT' AND score IS NULL "
            "AND report LIKE '%\"reasons\": [\"prefilter:%' AND report NOT LIKE ? "
            "AND id NOT IN (SELECT snapshot_id FROM alerts WHERE snapshot_id IS NOT NULL) "
            "AND id NOT IN (SELECT MAX(id) FROM research_snapshots GROUP BY mint)",
            (now - ch * 3600, f"%{COMPACT_FLAG}%"))["n"]
    return out


def vacuum(path: str, lock_path: Optional[str] = None) -> dict:
    """Rebuild the file to return freed pages to the disk. Refuses while the bot is running.

    VACUUM needs an exclusive lock for its whole run (the bot's writes would fail after their
    30 s busy timeout) and free disk space of about the database size (temporary copy). If it
    fails part-way SQLite leaves the original file intact.
    """
    if lock_path:
        from .engine import InstanceLock
        lk = InstanceLock(__import__("pathlib").Path(lock_path))
        try:
            lk.acquire()
        except RuntimeError as e:
            raise RuntimeError(f"stop the bot first: {e}") from None
    try:
        return _vacuum(path)
    finally:
        if lock_path:
            lk.release()


def _vacuum(path: str) -> dict:
    import shutil
    before = file_size(path)
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(path)) or ".").free
    if free < before * 1.1:
        raise RuntimeError(f"not enough free disk for VACUUM: {free / 1e6:,.0f} MB free, "
                           f"needs about {before * 1.1 / 1e6:,.0f} MB")
    con = sqlite3.connect(path, isolation_level=None, timeout=30)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("VACUUM")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    return {"before_bytes": before, "after_bytes": file_size(path)}


def format_sizes(rows: list[dict], total: int, top: int = 14) -> str:
    lines = [f"database {total / 1e6:,.1f} MB on disk (file + WAL)"]
    pages = sum(r["bytes"] for r in rows) or 1
    for r in rows[:top]:
        n = "index" if r["rows"] is None else f"{r['rows']:,} rows"
        lines.append(f"  {r['name']:<34} {r['bytes'] / 1e6:8.1f} MB {100 * r['bytes'] / pages:5.1f}%  {n}")
    return "\n".join(lines)
