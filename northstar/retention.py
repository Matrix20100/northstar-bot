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


# v2.5.5: old research reports that reached the security checks keep every number and verdict the
# learner, the brain, the audit and the re-check rule read, and lose the per-entity lists and
# repeated explanation text. Validated on the 22 Oct database: identical brain vectors, rug-check
# red flags, learning features and `python main.py audit` output (tests/test_db_retention.py).
UNIFIED_DROP = ("checks", "positives", "negatives", "exit_triggers", "invalidation")
# sections whose per-entity lists nothing reads after the decision (holder lists, trade samples...).
# `sellability` (exit checks) and `clusters` (the bubble map: linked groups, hub funding, two-hop
# links) are read by the rug verdict and the brain, so they stay whole.
LIST_TRIM_KEYS = ("holders", "pre_migration", "narrative", "trades", "creator", "security",
                  "market_structure", "liquidity_breakdown", "context")

DROP_KEYS = ("pre_migration_rpc_diagnostics",)


def _scalars_only(d: Any, depth: int = 0) -> Any:
    """Keep numbers, strings, booleans and nested dicts; drop lists of entities (holders, wallets,
    pools, trades). Short lists of scalars (e.g. failed check names) are kept."""
    if isinstance(d, dict):
        return {k: _scalars_only(v, depth + 1) for k, v in d.items()
                if not (isinstance(v, list) and (len(v) > 8 or any(isinstance(x, (dict, list)) for x in v)))}
    return d


def compact_analysis_report(r: dict) -> Optional[dict]:
    """An analysed (non-prefilter) report without its bulky detail, or None if already compact."""
    if r.get("compacted") or str((r.get("reasons") or [""])[0]).startswith("prefilter:"):
        return None
    out = {k: v for k, v in r.items() if k not in DROP_KEYS}
    u = out.get("unified")
    if isinstance(u, dict):
        u = {k: v for k, v in u.items() if k not in UNIFIED_DROP}
        cats = u.get("categories")
        if isinstance(cats, dict):
            u["categories"] = {k: ({kk: vv for kk, vv in c.items() if kk not in ("evidence", "weak", "missing")}
                                   if isinstance(c, dict) else c) for k, c in cats.items()}
        out["unified"] = u
    for k in LIST_TRIM_KEYS:
        if isinstance(out.get(k), dict):
            out[k] = _scalars_only(out[k])
    if isinstance(out.get("market"), dict):
        out["market"] = _slim_value(out["market"])
    if isinstance(out.get("pool"), dict):
        out["pool"] = {k: out["pool"][k] for k in ("address", "dex") if k in out["pool"]}
    out["compacted"] = 2
    return out


def compact_analysis_snapshots(db, older_than_hours: float, now: Optional[float] = None, batch: int = 500) -> int:
    """Compact analysed reports older than the cutoff (see compact_analysis_report).

    Never touches: a coin's latest snapshot, a snapshot an alert points to, or a snapshot whose
    price tracking still waits for the brain to copy its numbers (brain_vector not yet stored).
    """
    if not older_than_hours or older_than_hours <= 0:
        return 0
    now = now or time.time()
    rows = db.fetchall(
        "SELECT id, report FROM research_snapshots WHERE ts < ? AND report NOT LIKE '%\"compacted\": %' "
        "AND report NOT LIKE '%\"reasons\": [\"prefilter:%' "
        "AND id NOT IN (SELECT snapshot_id FROM alerts WHERE snapshot_id IS NOT NULL) "
        "AND id NOT IN (SELECT snapshot_id FROM decision_tracking WHERE brain_vector IS NULL) "
        "AND id NOT IN (SELECT MAX(id) FROM research_snapshots GROUP BY mint) LIMIT ?",
        (now - older_than_hours * 3600, batch))
    done = 0
    for row in rows:
        try:
            slim = compact_analysis_report(json.loads(row["report"]))
        except (TypeError, ValueError):
            continue
        if slim is None:
            continue
        db.execute("UPDATE research_snapshots SET report = ? WHERE id = ?", (json.dumps(slim, default=str), row["id"]))
        done += 1
    return done


def thin_repeated_snapshots(db, older_than_hours: float, now: Optional[float] = None) -> int:
    """Delete old snapshots that only repeat the coin's previous decision.

    A coin researched 20 times with the same outcome keeps the first snapshot of each run of
    identical decisions (same action and reason class), its latest snapshot, and every snapshot
    something points to (an alert, price tracking, an outcome horizon, a shadow test). Every
    change of decision stays in the history; the per-research counts stay in funnel_events.
    """
    if not older_than_hours or older_than_hours <= 0:
        return 0
    cutoff = (now or time.time()) - older_than_hours * 3600
    rows = db.fetchall(
        "SELECT id, mint, action, report FROM research_snapshots WHERE mint IN "
        "(SELECT mint FROM research_snapshots WHERE ts < ? GROUP BY mint HAVING COUNT(*) > 1) ORDER BY mint, ts, id",
        (cutoff,))
    protected = {r["sid"] for r in db.fetchall(
        "SELECT snapshot_id sid FROM alerts WHERE snapshot_id IS NOT NULL UNION "
        "SELECT snapshot_id FROM decision_tracking UNION SELECT snapshot_id FROM outcomes")}
    try:
        protected |= {r["sid"] for r in db.fetchall(
            "SELECT snapshot_id sid FROM shadow_tests WHERE snapshot_id IS NOT NULL")}
    except Exception:  # noqa: BLE001 - no shadow_tests table yet
        pass
    try:
        # the audit and the learner pair each learning shadow with the research snapshot taken at
        # that moment (the latest one at or before it); those snapshots stay
        protected |= {r["sid"] for r in db.fetchall(
            "SELECT (SELECT r.id FROM research_snapshots r WHERE r.mint = s.mint AND r.ts <= s.ts + 60 "
            "ORDER BY r.ts DESC LIMIT 1) sid FROM shadow_calls s") if r["sid"] is not None}
    except Exception:  # noqa: BLE001 - no shadow_calls table yet
        pass
    latest = {r["mid"] for r in db.fetchall("SELECT MAX(id) mid FROM research_snapshots GROUP BY mint")}
    ts_by_id = {r["id"]: r["ts"] for r in db.fetchall(
        "SELECT id, ts FROM research_snapshots WHERE mint IN "
        "(SELECT mint FROM research_snapshots WHERE ts < ? GROUP BY mint HAVING COUNT(*) > 1)", (cutoff,))}
    drop, prev = [], {}
    for r in rows:
        try:
            rep = json.loads(r["report"])
        except (TypeError, ValueError):
            continue
        key = (r["action"], (rep.get("final") or {}).get("reason_class")
               or str((rep.get("reasons") or [""])[0])[:60])
        same = prev.get(r["mint"]) == key
        prev[r["mint"]] = key
        if same and ts_by_id.get(r["id"], now or 0) < cutoff and r["id"] not in protected and r["id"] not in latest:
            drop.append(r["id"])
    removed = 0
    for i in range(0, len(drop), 2000):
        part = drop[i:i + 2000]
        removed += db._tx(lambda c, part=part: c.execute(
            f"DELETE FROM research_snapshots WHERE id IN ({','.join('?' * len(part))})", part).rowcount)
    return removed


def downsample_price_history(db, detail_hours: float, bucket_minutes: float = 5.0,
                             now: Optional[float] = None) -> int:
    """Keep every price update of a callout for `detail_hours`; after that keep, per alert and per
    `bucket_minutes`, only the highest and lowest price (plus the first and last update). Peaks,
    drawdowns and the order of 2x / 0.6x / 0.3x events stay readable at that resolution."""
    if not detail_hours or detail_hours <= 0:
        return 0
    cutoff = (now or time.time()) - detail_hours * 3600
    b = max(60.0, bucket_minutes * 60)
    keep = db.fetchall(
        "SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY alert_id, CAST(ts / ? AS INTEGER) "
        "ORDER BY price DESC, id) hi, ROW_NUMBER() OVER (PARTITION BY alert_id, CAST(ts / ? AS INTEGER) "
        "ORDER BY price ASC, id) lo FROM price_history WHERE ts < ? AND price IS NOT NULL) WHERE hi = 1 OR lo = 1 "
        "UNION SELECT MIN(id) FROM price_history GROUP BY alert_id "
        "UNION SELECT MAX(id) FROM price_history GROUP BY alert_id", (b, b, cutoff))
    keep_ids = {r["id"] for r in keep}
    old = [r["id"] for r in db.fetchall("SELECT id FROM price_history WHERE ts < ?", (cutoff,))
           if r["id"] not in keep_ids]
    removed = 0
    for i in range(0, len(old), 5000):
        part = old[i:i + 5000]
        removed += db._tx(lambda c, part=part: c.execute(
            f"DELETE FROM price_history WHERE id IN ({','.join('?' * len(part))})", part).rowcount)
    return removed


def prune_market_snapshots(db, hours: float, now: Optional[float] = None) -> int:
    """Market snapshots are read only for the last 24 h (liquidity-removal check)."""
    if not hours or hours <= 0:
        return 0
    cutoff = (now or time.time()) - max(hours, 25.0) * 3600
    removed = 0
    while True:
        n = db._tx(lambda c: c.execute("DELETE FROM market_snapshots WHERE id IN "
                                       "(SELECT id FROM market_snapshots WHERE ts < ? LIMIT 5000)", (cutoff,)).rowcount)
        removed += n
        if n < 5000:
            return removed


def incremental_vacuum(db, max_pages: int = 20000, chunk: int = 2000) -> int:
    """Give free pages back to the disk without a full VACUUM (no temporary copy, short locks).

    Works when the database uses auto_vacuum=INCREMENTAL, which a VACUUM run by this module (or a
    database created by v2.5.5+) switches on. Returns pages released."""
    try:
        if db.fetchone("PRAGMA auto_vacuum")["auto_vacuum"] != 2:
            return 0
        freed = 0
        while freed < max_pages:
            before = db.fetchone("PRAGMA freelist_count")["freelist_count"]
            if not before:
                break
            n = min(chunk, before, max_pages - freed)
            db._tx(lambda c, n=n: c.execute(f"PRAGMA incremental_vacuum({int(n)})").fetchall())
            freed += before - db.fetchone("PRAGMA freelist_count")["freelist_count"]
        return freed
    except Exception as e:  # noqa: BLE001
        log.warning("incremental vacuum skipped: %s", e)
        return 0


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
def _training_compact(db, s: Any, now: float) -> dict:
    """Harvest finished examples into the compact training store, then bound it and the raw
    tables it replaces (training_store.py). The harvest runs first so nothing is lost."""
    from . import training_store as TS
    TS.harvest(db, now)
    return TS.compact(db, s, now)


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
        ("candidates_forgotten", lambda: db.prune_candidates(getattr(s, "candidate_retention_days", 7.0), now)),
        ("launches_expired", lambda: db.expire_unscheduled_launches(
            getattr(s, "discovered_launch_max_age_minutes", 30.0), now)),
        ("creators_forgotten", lambda: db.prune_creator_stats(now=now) if hasattr(db, "prune_creator_stats") else 0),
        ("training_memory", lambda: _training_compact(db, s, now)),
        ("snapshots_compacted", lambda: sum(
            compact_snapshots(db, getattr(s, "snapshot_compact_hours", 24.0), now) for _ in range(5))),
        ("repeated_snapshots_removed", lambda: thin_repeated_snapshots(
            db, getattr(s, "snapshot_compact_hours", 24.0), now)),
        ("analysis_reports_compacted", lambda: sum(
            compact_analysis_snapshots(db, getattr(s, "snapshot_compact_hours", 24.0), now) for _ in range(6))),
        ("price_updates_thinned", lambda: downsample_price_history(
            db, getattr(s, "price_history_detail_hours", 24.0), getattr(s, "price_history_bucket_minutes", 5.0), now)),
        ("market_snapshots_removed", lambda: prune_market_snapshots(
            db, getattr(s, "market_snapshot_retention_hours", 48.0), now)),
    ):
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001 - housekeeping must never break the bot
            log.warning("retention step %s failed: %s", name, e, exc_info=True)
            out[name] = f"failed: {type(e).__name__}"
    if any(v for k, v in out.items() if isinstance(v, int)):
        checkpoint(db, "TRUNCATE")            # give the WAL's disk space back after a big pass
    if getattr(s, "db_auto_shrink", True):
        pages = incremental_vacuum(db)
        if pages:
            out["pages_returned_to_disk"] = pages
            checkpoint(db, "TRUNCATE")
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


def free_share(path: str) -> tuple[float, int]:
    """(share of the file that is free pages, free bytes). Read-only."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        page = con.execute("PRAGMA page_size").fetchone()[0]
        total = con.execute("PRAGMA page_count").fetchone()[0] or 1
        free = con.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        con.close()
    return free / total, free * page


def vacuum_on_start(path: str, min_share: float = 0.2, min_bytes: int = 50_000_000) -> Optional[dict]:
    """Shrink the file at startup when the retention pass has freed a lot of space.

    Called by `python main.py run` after it holds the instance lock and BEFORE the database is
    opened for the bot, so nothing else is using it. Skipped (None) when there is little to gain,
    the file does not exist yet, or there is not enough free disk; never raises.
    """
    try:
        if not os.path.exists(path):
            return None
        share, free = free_share(path)
        if share < min_share or free < min_bytes:
            return None
        return _vacuum(path)
    except Exception as e:  # noqa: BLE001 - a failed shrink must never stop the bot starting
        log.warning("startup VACUUM skipped: %s", e)
        return None


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
        # from now on the bot can give freed space back to the disk itself (incremental_vacuum)
        con.execute("PRAGMA auto_vacuum = INCREMENTAL")
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
