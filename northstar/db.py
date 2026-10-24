"""
SQLite persistence.

All access goes through a single connection guarded by a lock.
WAL mode and explicit transactions keep writes atomic.

A corrupt database file is moved aside rather than deleted.
Existing tables and tracking history are preserved.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("northstar.db")

SCHEMA_VERSION = 2
MAX_PRIORITY = 20.0  # research priorities are small numbers; anything above this was inflated

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    mint TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    priority REAL NOT NULL DEFAULT 0,
    discovered_at REAL NOT NULL,
    sources TEXT NOT NULL DEFAULT '[]',
    name TEXT,
    symbol TEXT,
    pool_address TEXT,
    last_research_at REAL,
    next_research_at REAL,
    research_count INTEGER NOT NULL DEFAULT 0,
    last_data_hash TEXT,
    last_action TEXT,
    last_score REAL,
    last_risk TEXT,
    last_confidence TEXT,
    reasons TEXT NOT NULL DEFAULT '[]',
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_cand_next
ON candidates(state, next_research_at);

CREATE INDEX IF NOT EXISTS ix_cand_symbol
ON candidates(symbol COLLATE NOCASE, discovered_at);

CREATE TABLE IF NOT EXISTS discovery_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    source TEXT NOT NULL,
    ts REAL NOT NULL,
    payload TEXT
);

CREATE INDEX IF NOT EXISTS ix_disc_mint
ON discovery_events(mint);

CREATE TABLE IF NOT EXISTS curve_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    ts REAL NOT NULL,
    tx_type TEXT,
    wallet TEXT,
    signature TEXT,
    sol_amount REAL,
    token_amount REAL,
    market_cap_sol REAL,
    v_sol_curve REAL,
    v_tokens_curve REAL,
    pool TEXT,
    bonding_curve_key TEXT,
    payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_curve_mint_ts
ON curve_snapshots(mint, ts);

CREATE TABLE IF NOT EXISTS early_signals (
    mint TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    entry_market_cap_sol REAL,
    current_market_cap_sol REAL,
    peak_multiple REAL NOT NULL DEFAULT 1,
    min_multiple REAL NOT NULL DEFAULT 1,
    score REAL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    channel_id INTEGER,
    message_id INTEGER,
    report TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    ts REAL NOT NULL,
    action TEXT NOT NULL,
    score REAL,
    risk TEXT,
    confidence TEXT,
    pool_address TEXT,
    price_usd REAL,
    liquidity_usd REAL,
    market_cap REAL,
    fdv REAL,
    report TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_snap_mint
ON research_snapshots(mint, ts);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    pair_address TEXT,
    ts REAL NOT NULL,
    price_usd REAL,
    liquidity_usd REAL,
    market_cap REAL,
    fdv REAL,
    vol_m5 REAL,
    vol_h1 REAL,
    buys_m5 INTEGER,
    sells_m5 INTEGER,
    buys_h1 INTEGER,
    sells_h1 INTEGER,
    unique_buyers INTEGER,
    unique_sellers INTEGER,
    top10_adj_pct REAL
);

CREATE INDEX IF NOT EXISTS ix_msnap_mint
ON market_snapshots(mint, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    status TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    sent_at REAL,
    channel_id INTEGER,
    message_id INTEGER,
    snapshot_id INTEGER,
    pool_address TEXT,
    symbol TEXT,
    name TEXT,
    entry_price REAL,
    entry_mcap REAL,
    entry_fdv REAL,
    entry_liq REAL,
    score REAL,
    risk TEXT,
    confidence TEXT,
    current_price REAL,
    current_mcap REAL,
    current_liq REAL,
    price_status TEXT,
    last_valid_price_at REAL,
    max_multiple REAL,
    min_multiple REAL,
    max_drawdown_pct REAL,
    entry_top10_adj_pct REAL,
    current_top10_adj_pct REAL,
    milestones_hit TEXT NOT NULL DEFAULT '[]',
    warnings TEXT NOT NULL DEFAULT '[]',
    last_update_at REAL,
    last_embed_edit_at REAL,
    publish_attempts INTEGER NOT NULL DEFAULT 0,
    closed_at REAL
);

CREATE INDEX IF NOT EXISTS ix_alert_mint
ON alerts(mint, created_at);

CREATE TABLE IF NOT EXISTS free_channel_posts (
    alert_id INTEGER PRIMARY KEY,
    due_at REAL NOT NULL,
    channel_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    message_id INTEGER,
    sent_at REAL,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS ix_free_posts_due
ON free_channel_posts(status, due_at);

CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    price REAL,
    liquidity REAL,
    market_cap REAL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    snapshot_id INTEGER NOT NULL,
    horizon TEXT NOT NULL,
    mint TEXT NOT NULL,
    action TEXT NOT NULL,
    score REAL,
    decided_at REAL NOT NULL,
    due_at REAL NOT NULL,
    entry_price REAL,
    entry_liq REAL,
    observed_at REAL,
    price REAL,
    liquidity REAL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    PRIMARY KEY (snapshot_id, horizon)
);

CREATE INDEX IF NOT EXISTS ix_out_due
ON outcomes(status, due_at);

CREATE TABLE IF NOT EXISTS decision_tracking (
    snapshot_id INTEGER PRIMARY KEY,
    mint TEXT NOT NULL,
    action TEXT NOT NULL,
    score REAL,
    decided_at REAL NOT NULL,
    entry_price REAL,
    entry_liq REAL,
    max_price REAL,
    min_price REAL,
    last_price REAL,
    min_liq REAL,
    samples INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS provider_health (
    provider TEXT PRIMARY KEY,
    ok_count INTEGER NOT NULL DEFAULT 0,
    err_count INTEGER NOT NULL DEFAULT 0,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    last_ok REAL,
    last_err REAL,
    last_error TEXT,
    budget_day TEXT,
    budget_used INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'UNKNOWN'
);

-- v2.5.9: every pump.fun launch and graduation seen on the PumpPortal feed, per creator wallet
CREATE TABLE IF NOT EXISTS creator_stats (
    creator TEXT PRIMARY KEY,
    launches INTEGER NOT NULL DEFAULT 0,
    migrated INTEGER NOT NULL DEFAULT 0,
    first_ts REAL,
    last_ts REAL
);

CREATE TABLE IF NOT EXISTS creator_cache (
    creator TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS social_links (
    mint TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    first_seen REAL NOT NULL,
    PRIMARY KEY (mint, kind, key)
);
CREATE INDEX IF NOT EXISTS idx_social_links_key ON social_links(kind, key, first_seen);

-- v2.5 decision funnel: one row per stage outcome, so "why was nothing called?" has a count
-- and an exact reason at every stage (see northstar/telemetry.py). Added with IF NOT EXISTS:
-- no migration step, older databases simply gain the table.
CREATE TABLE IF NOT EXISTS funnel_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mint TEXT,
    stage TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason_class TEXT,
    reason TEXT,
    score REAL
);
CREATE INDEX IF NOT EXISTS idx_funnel_ts ON funnel_events(ts);
"""


class Database:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self.recovered_from_corruption: Optional[str] = None

    # -------------------------------------------------------------- lifecycle

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self._connect()
            result = self._conn.execute("PRAGMA quick_check").fetchone()[0]

            if result != "ok":
                raise sqlite3.DatabaseError(f"quick_check: {result}")

        except sqlite3.DatabaseError as exc:
            self._quarantine(str(exc))
            self._connect()

        self._conn.executescript(SCHEMA)
        self._add_missing_columns()
        # Undo priority inflation from older versions (repeated feed events added +0.5 each time).
        self._conn.execute("UPDATE candidates SET priority = ? WHERE priority > ?", (MAX_PRIORITY, MAX_PRIORITY))

        self._conn.execute(
            """
            INSERT INTO meta(key, value)
            VALUES('schema_version', ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
            """,
            (str(SCHEMA_VERSION),),
        )

        self._conn.commit()

    # Columns added after the first release. SQLite cannot "ADD COLUMN IF NOT EXISTS", so check first.
    EXTRA_COLUMNS = {
        "alerts": [("peak_mcap", "REAL"), ("peak_at", "REAL"), ("call_type", "TEXT NOT NULL DEFAULT 'STANDARD'"),
                   ("plan_json", "TEXT")],
        # v2.5.4 retention: an identical repeat of the last event for the same mint+source is
        # counted (seen, last_ts) instead of stored again; tracking is polled round-robin
        "discovery_events": [("seen", "INTEGER NOT NULL DEFAULT 1"), ("last_ts", "REAL")],
        "decision_tracking": [("last_polled_at", "REAL")],
        # v4.5.0: highest market cap / liquidity seen (confirmed-rug check) and live rug flags (Watching)
        "candidates": [("peak_mcap", "REAL"), ("peak_liq", "REAL"), ("rug_flags", "TEXT"), ("watch_since", "REAL")],
    }

    def _add_missing_columns(self) -> None:
        for table, cols in self.EXTRA_COLUMNS.items():
            have = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _connect(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass

        self._conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
            timeout=30,
        )

        self._conn.row_factory = sqlite3.Row
        if not self._conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
            # a brand-new database: let the retention pass give freed space back to the disk
            # (auto_vacuum can only be chosen before the first table, or by a full VACUUM)
            self._conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def _quarantine(self, reason: str) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass

            self._conn = None

        stamp = time.strftime("%Y%m%d-%H%M%S")
        destination = self.path.with_name(
            f"{self.path.name}.corrupt-{stamp}"
        )

        for suffix in ("", "-wal", "-shm"):
            source = Path(str(self.path) + suffix)

            if source.exists():
                shutil.move(
                    str(source),
                    str(destination) + suffix,
                )

        self.recovered_from_corruption = (
            f"{reason}; moved to {destination.name}"
        )

        log.critical(
            "Database corrupt; quarantined and recreated: %s",
            self.recovered_from_corruption,
        )

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    # -------------------------------------------------------------- primitives

    def _exec(
        self,
        sql: str,
        params: Iterable[Any] = (),
    ) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def _tx(self, fn):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")

            try:
                result = fn(self._conn)
                self._conn.execute("COMMIT")
                return result

            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def fetchall(
        self,
        sql: str,
        params: Iterable[Any] = (),
    ) -> list[dict]:
        with self._lock:
            return [
                dict(row)
                for row in self._conn.execute(
                    sql,
                    tuple(params),
                ).fetchall()
            ]

    def fetchone(
        self,
        sql: str,
        params: Iterable[Any] = (),
    ) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                sql,
                tuple(params),
            ).fetchone()

            return dict(row) if row else None

    def execute(
        self,
        sql: str,
        params: Iterable[Any] = (),
    ) -> int:
        with self._lock:
            cursor = self._conn.execute(sql, tuple(params))
            return cursor.lastrowid

    async def run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    # -------------------------------------------------------------- candidates

    def upsert_discovery(
        self,
        mint: str,
        source: str,
        priority: float,
        name: Optional[str] = None,
        symbol: Optional[str] = None,
        payload: Optional[dict] = None,
    ) -> bool:
        """Record a discovery. Returns True if the mint is new."""
        now = time.time()

        def tx(c: sqlite3.Connection) -> bool:
            row = c.execute(
                """
                SELECT sources, priority, state
                FROM candidates
                WHERE mint = ?
                """,
                (mint,),
            ).fetchone()

            body = json.dumps(payload or {}, default=str)[:4000]
            # Feeds re-list the same coin every minute with the same payload (live DB 21 Oct:
            # 74% of all discovery rows were exact repeats). Count the repeat on the last row
            # instead of storing it again; a changed payload is still stored as a new event.
            last = c.execute(
                "SELECT id, payload FROM discovery_events WHERE mint = ? AND source = ? "
                "ORDER BY id DESC LIMIT 1",
                (mint, source),
            ).fetchone()
            if last is not None and last["payload"] == body:
                c.execute("UPDATE discovery_events SET seen = seen + 1, last_ts = ? WHERE id = ?",
                          (now, last["id"]))
            else:
                c.execute(
                    """
                    INSERT INTO discovery_events(mint, source, ts, payload)
                    VALUES (?, ?, ?, ?)
                    """,
                    (mint, source, now, body),
                )

            if row is not None and row["state"] == "STALE" and not (last is not None and last["payload"] == body):
                # a coin that dropped off the list comes back on NEW information (a surge, a
                # migration, a new profile or boost); a repeat of an old event does not revive it
                c.execute("UPDATE candidates SET state = 'DISCOVERED', next_research_at = ? WHERE mint = ?",
                          (now, mint))

            if row is None:
                c.execute(
                    """
                    INSERT INTO candidates(
                        mint, state, priority, discovered_at, sources,
                        name, symbol, next_research_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mint,
                        "DISCOVERED",
                        priority,
                        now,
                        json.dumps([source]),
                        name,
                        symbol,
                        now,
                        now,
                    ),
                )

                return True

            sources = json.loads(row["sources"])

            if source not in sources:
                sources.append(source)

            # A small bonus for each NEW independent source. Repeated events from the same feed
            # (e.g. a profile re-listed every minute) used to add 0.5 each time, pushing spammed
            # coins to priority 500+ and starving the queue.
            is_new_source = source not in json.loads(row["sources"])
            new_priority = max(
                row["priority"],
                priority,
            ) + (0.5 if is_new_source and len(sources) > 1 else 0)
            new_priority = min(new_priority, MAX_PRIORITY)

            c.execute(
                """
                UPDATE candidates
                SET sources = ?,
                    priority = ?,
                    name = COALESCE(name, ?),
                    symbol = COALESCE(symbol, ?),
                    updated_at = ?
                WHERE mint = ?
                """,
                (
                    json.dumps(sources),
                    new_priority,
                    name,
                    symbol,
                    now,
                    mint,
                ),
            )

            return False

        return self._tx(tx)

    def get_candidate(self, mint: str) -> Optional[dict]:
        return self.fetchone(
            "SELECT * FROM candidates WHERE mint = ?",
            (mint,),
        )

    def due_candidates(
        self,
        limit: int,
        now: Optional[float] = None,
    ) -> list[dict]:
        now = now or time.time()

        return self.fetchall(
            """
            SELECT *
            FROM candidates
            WHERE state IN (
                'DISCOVERED',
                'WATCHLIST',
                'INSUFFICIENT_DATA',
                'REJECTED'
            )
            AND next_research_at IS NOT NULL
            AND next_research_at <= ?
            ORDER BY priority DESC, next_research_at ASC
            LIMIT ?
            """,
            (now, limit),
        )

    def set_state(
        self,
        mint: str,
        state: str,
        **fields: Any,
    ) -> None:
        fields["state"] = state
        fields["updated_at"] = time.time()

        if "reasons" in fields and not isinstance(fields["reasons"], str):
            fields["reasons"] = json.dumps(fields["reasons"])

        columns = ", ".join(f"{key} = ?" for key in fields)
        # v4.5.0: when a coin went on the watch list, so it can be taken off after `watchlist_max_hours`
        if state == "WATCHLIST" and "watch_since" not in fields:
            columns += ", watch_since = COALESCE(watch_since, ?)"
            fields["_ws"] = fields["updated_at"]
        elif state not in ("WATCHLIST", "RESEARCHING") and "watch_since" not in fields:
            columns += ", watch_since = NULL"

        self.execute(
            f"UPDATE candidates SET {columns} WHERE mint = ?",
            [*fields.values(), mint],
        )

    def expire_watchlist(self, max_hours: float, now: Optional[float] = None) -> int:
        """v4.5.0: take coins off the watch list after `max_hours` (operator: a coin with score 100 sat on
        the list for hours at a $5K market cap). A new surge, migration or listing brings one back."""
        if not max_hours or max_hours <= 0:
            return 0
        now = now or time.time()
        why = json.dumps([f"left the watch list: watched for more than {max_hours:g}h without becoming a call"])
        with self._lock:
            return self._conn.execute(
                "UPDATE candidates SET state = 'STALE', next_research_at = NULL, watch_since = NULL, reasons = ?, "
                "updated_at = ? WHERE state = 'WATCHLIST' AND COALESCE(watch_since, discovered_at) < ?",
                (why, now, now - max_hours * 3600)).rowcount

    def unschedule_stale_launches(self) -> int:
        """Drop queued pump.fun launches that have never been researched.

        Changing the discovery rule only affects launches recorded from then on; the ones already
        sitting in the queue keep their slot and go on producing INSUFFICIENT DATA for hours. This
        clears exactly those: still DISCOVERED, never researched, and known only from the launch
        feed. Anything the curve radar promoted, anything that graduated, and anything a
        DexScreener feed also saw keeps its place.
        """
        where = ("state = 'DISCOVERED' AND next_research_at IS NOT NULL "
                 "AND last_research_at IS NULL AND research_count = 0 "
                 "AND sources LIKE '%pumpportal_new%' "
                 "AND sources NOT LIKE '%curve_radar%' "
                 "AND sources NOT LIKE '%migration%' "
                 "AND sources NOT LIKE '%dexscreener%'")
        row = self.fetchone(f"SELECT COUNT(*) AS n FROM candidates WHERE {where}")
        n = int((row or {}).get("n") or 0)
        if n:
            self.execute(f"UPDATE candidates SET next_research_at = NULL WHERE {where}")
        return n

    def expire_discovered(self, max_age_hours: float, now: Optional[float] = None) -> int:
        """Never-researched coins older than `max_age_hours` leave the watch list (state STALE).

        v2.5.4 and earlier kept every launch the feeds ever reported as DISCOVERED (the 21 Oct
        backup: 50,846 coins on the watch list, nearly all never researched). New information
        about a coin brings it back (upsert_discovery).
        """
        if not max_age_hours or max_age_hours <= 0:
            return 0
        cutoff = (now or time.time()) - max_age_hours * 3600
        with self._lock:
            return self._conn.execute(
                "UPDATE candidates SET state = 'STALE', next_research_at = NULL, updated_at = ? "
                "WHERE state = 'DISCOVERED' AND research_count = 0 AND discovered_at < ?",
                (time.time(), cutoff)).rowcount

    # ------------------------------------------------------------- manual untrack (v2.6.1)

    def untrack(self, mint: str, reason: str = "", by: str = "operator") -> dict:
        """Stop following a coin by hand: it leaves the watch list and the research queue, its open
        call and early-watch posts stop updating, and no feed, radar or surge brings it back
        (state IGNORED is never revived). Its learning data (24 h price tracking) is kept so the
        brains can still learn how it ended. Undo with `retrack`."""
        now = time.time()
        out = {"mint": mint, "candidate": False, "calls_closed": 0, "early_watches_ended": 0, "early_calls_closed": 0}
        note = f"untracked by {by}" + (f": {reason}" if reason else "")
        with self._lock:
            c = self._conn
            row = c.execute("SELECT reasons FROM candidates WHERE mint = ?", (mint,)).fetchone()
            if row is not None:
                try:
                    reasons = json.loads(row["reasons"] or "[]")
                except (TypeError, ValueError):
                    reasons = []
                c.execute("UPDATE candidates SET state = 'IGNORED', next_research_at = NULL, updated_at = ?, "
                          "reasons = ? WHERE mint = ?", (now, json.dumps([note] + list(reasons)[:19]), mint))
                out["candidate"] = True
            out["calls_closed"] = c.execute(
                "UPDATE alerts SET state = 'CLOSED', closed_at = ? WHERE mint = ? AND state != 'CLOSED'",
                (now, mint)).rowcount
            out["early_watches_ended"] = c.execute(
                "UPDATE early_signals SET status = 'UNTRACKED', updated_at = ? WHERE mint = ? "
                "AND status IN ('SENT', 'DRY_RUN', 'SILENT')", (now, mint)).rowcount
            try:
                out["early_calls_closed"] = c.execute(
                    "UPDATE early_calls SET status = 'CLOSED', closed_at = ? WHERE mint = ? AND status != 'CLOSED'",
                    (now, mint)).rowcount
            except sqlite3.OperationalError:
                pass                                   # early-call table not created yet
        return out

    def retrack(self, mint: str) -> bool:
        """Undo `untrack`: the coin is researched again on the next pass."""
        now = time.time()
        with self._lock:
            return self._conn.execute("UPDATE candidates SET state = 'DISCOVERED', next_research_at = ?, updated_at = ? "
                                      "WHERE mint = ? AND state IN ('IGNORED', 'DROPPED')", (now, now, mint)).rowcount > 0

    def expire_unscheduled_launches(self, max_age_minutes: float, now: Optional[float] = None) -> int:
        """Launch-feed-only coins with no research scheduled leave the list after `max_age_minutes`
        (v2.5.9). They were never "watched": they wait for the curve radar, which still scans STALE
        launches and brings one back the moment it shows real buying (a new curve_radar event
        revives it). The dashboard counted ~4,300 of them as "coins being watched"."""
        if not max_age_minutes or max_age_minutes <= 0:
            return 0
        cutoff = (now or time.time()) - max_age_minutes * 60
        with self._lock:
            return self._conn.execute(
                "UPDATE candidates SET state = 'STALE', updated_at = ? "
                "WHERE state = 'DISCOVERED' AND research_count = 0 AND next_research_at IS NULL "
                "AND discovered_at < ? AND sources = '[\"pumpportal_new\"]'",
                (time.time(), cutoff)).rowcount

    def prune_candidates(self, days: float, now: Optional[float] = None) -> int:
        """Forget finished coins nobody follows any more (v2.5.5).

        Only STALE coins and REJECTED / INSUFFICIENT_DATA coins with no re-check scheduled, last
        touched more than `days` ago, with no callout, early signal or open price tracking. If such
        a coin shows up again it is simply discovered as new.
        """
        if not days or days <= 0:
            return 0
        cutoff = (now or time.time()) - days * 86400
        # v4.5.0: confirmed rugs are remembered at least 30 days, so a feed re-listing a dead coin
        # finds it blocked instead of researching it as new
        rug_cutoff = (now or time.time()) - max(days, 30.0) * 86400
        with self._lock:
            n = self._conn.execute(
                "DELETE FROM candidates WHERE state IN ('STALE', 'REJECTED', 'INSUFFICIENT_DATA') "
                "AND next_research_at IS NULL AND updated_at < ? "
                "AND mint NOT IN (SELECT mint FROM alerts) AND mint NOT IN (SELECT mint FROM early_signals) "
                "AND mint NOT IN (SELECT mint FROM decision_tracking WHERE done = 0)", (cutoff,)).rowcount
            n += self._conn.execute("DELETE FROM candidates WHERE state = 'RUGGED' AND updated_at < ? "
                                    "AND mint NOT IN (SELECT mint FROM alerts)", (rug_cutoff,)).rowcount
            return n

    def recover_after_restart(self) -> int:
        """Re-queue candidates left RESEARCHING after a crash."""
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE candidates
                SET state = 'DISCOVERED',
                    next_research_at = ?
                WHERE state = 'RESEARCHING'
                """,
                (time.time(),),
            )

            return cursor.rowcount

    # -------------------------------------------------------------- snapshots

    def save_research(
        self,
        mint: str,
        action: str,
        score: Optional[float],
        risk: str,
        confidence: str,
        pool: Optional[str],
        price: Optional[float],
        liq: Optional[float],
        mcap: Optional[float],
        fdv: Optional[float],
        report: dict,
    ) -> int:
        return self.execute(
            """
            INSERT INTO research_snapshots(
                mint, ts, action, score, risk, confidence,
                pool_address, price_usd, liquidity_usd,
                market_cap, fdv, report
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mint,
                time.time(),
                action,
                score,
                risk,
                confidence,
                pool,
                price,
                liq,
                mcap,
                fdv,
                json.dumps(report, default=str),
            ),
        )

    def update_research_final(self, snapshot_id: int, action: str, score: Optional[float],
                              risk: str, confidence: str, report: dict) -> None:
        """Rewrite a snapshot with the FINAL decision (after live context, brain and AI).

        The snapshot is first written inside research(); the engine can still move the decision
        afterwards. v2.4 kept the pre-move row, so the history showed a different decision and
        score from the one the bot acted on.
        """
        self.execute(
            "UPDATE research_snapshots SET action=?, score=?, risk=?, confidence=?, report=? WHERE id=?",
            (action, score, risk, confidence, json.dumps(report, default=str), snapshot_id),
        )
        self.execute("UPDATE outcomes SET action=?, score=? WHERE snapshot_id=? AND status='PENDING'",
                     (action, score, snapshot_id))

    # -------------------------------------------------------------- funnel telemetry

    def record_funnel(self, rows: list[tuple]) -> None:
        """rows: (ts, mint, stage, outcome, reason_class, reason, score)"""
        if not rows:
            return

        def tx(c: sqlite3.Connection) -> None:
            c.executemany(
                "INSERT INTO funnel_events(ts, mint, stage, outcome, reason_class, reason, score) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)

        self._tx(tx)

    def save_market_snapshot(
        self,
        mint: str,
        pair: Optional[str],
        **values: Any,
    ) -> None:
        columns = ["mint", "pair_address", "ts", *values.keys()]

        self.execute(
            f"""
            INSERT INTO market_snapshots({','.join(columns)})
            VALUES ({','.join('?' * len(columns))})
            """,
            [mint, pair, time.time(), *values.values()],
        )

    def market_history(
        self,
        mint: str,
        since: float,
    ) -> list[dict]:
        return self.fetchall(
            """
            SELECT *
            FROM market_snapshots
            WHERE mint = ? AND ts >= ?
            ORDER BY ts
            """,
            (mint, since),
        )

    # ------------------------------------------------ bonding-curve observations

    def record_curve_snapshot(
        self,
        mint: str,
        payload: dict,
        ts: Optional[float] = None,
    ) -> None:
        """
        Store PumpPortal observations.

        Duplicate transaction signatures for the same mint are ignored.
        Observations without signatures are retained because they cannot be
        safely deduplicated.
        """
        ts = time.time() if ts is None else ts

        def number(*keys):
            for key in keys:
                try:
                    value = payload.get(key)

                    if value is None:
                        continue

                    value = float(value)

                    if math.isfinite(value) and value >= 0:
                        return value

                except (TypeError, ValueError, OverflowError):
                    continue

            return None

        tx_type = str(
            payload.get("txType") or "launch"
        ).lower()

        signature = payload.get("signature")

        if signature:
            signature = str(signature).strip() or None

        def tx(c: sqlite3.Connection) -> None:
            if signature:
                existing = c.execute(
                    """
                    SELECT 1
                    FROM curve_snapshots
                    WHERE mint = ? AND signature = ?
                    LIMIT 1
                    """,
                    (mint, signature),
                ).fetchone()

                if existing:
                    return

            c.execute(
                """
                INSERT INTO curve_snapshots(
                    mint, ts, tx_type, wallet, signature,
                    sol_amount, token_amount, market_cap_sol,
                    v_sol_curve, v_tokens_curve, pool,
                    bonding_curve_key, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mint,
                    ts,
                    tx_type,
                    payload.get("traderPublicKey") or payload.get("wallet"),
                    signature,
                    number("solAmount"),
                    number("tokenAmount"),
                    number("marketCapSol"),
                    number("vSolInBondingCurve"),
                    number("vTokensInBondingCurve"),
                    payload.get("pool"),
                    payload.get("bondingCurveKey"),
                    json.dumps(payload, default=str)[:4000],
                ),
            )

            c.execute(
                """
                DELETE FROM curve_snapshots
                WHERE mint = ?
                  AND id NOT IN (
                      SELECT id
                      FROM curve_snapshots
                      WHERE mint = ?
                      ORDER BY ts DESC, id DESC
                      LIMIT 600
                  )
                """,
                (mint, mint),
            )

            c.execute(
                "DELETE FROM curve_snapshots WHERE ts < ?",
                (ts - 7 * 86400,),
            )

        self._tx(tx)

    def curve_history(
        self,
        mint: str,
        since: float,
    ) -> list[dict]:
        rows = self.fetchall(
            """
            SELECT *
            FROM curve_snapshots
            WHERE mint = ? AND ts >= ?
            ORDER BY ts, id
            """,
            (mint, since),
        )

        for row in rows:
            try:
                row["payload"] = json.loads(
                    row.get("payload") or "{}"
                )
            except (TypeError, ValueError):
                row["payload"] = {}

        return rows

    def create_or_update_early_signal(
        self,
        mint: str,
        market_cap_sol: Optional[float],
        score: float,
        report: dict,
    ) -> tuple[dict, bool]:
        """
        Create/update an early-watch tracking record.

        If the original market cap was unavailable, the first valid
        observation becomes a delayed baseline. That delayed baseline
        must never be represented as the original callout valuation.
        """
        now = time.time()

        try:
            if market_cap_sol is not None:
                market_cap_sol = float(market_cap_sol)

                if (
                    not math.isfinite(market_cap_sol)
                    or market_cap_sol <= 0
                ):
                    market_cap_sol = None

        except (TypeError, ValueError, OverflowError):
            market_cap_sol = None

        def tx(c: sqlite3.Connection) -> tuple[dict, bool]:
            row = c.execute(
                """
                SELECT *
                FROM early_signals
                WHERE mint = ?
                """,
                (mint,),
            ).fetchone()

            updated_report = dict(report or {})
            tracking = dict(
                updated_report.get("early_tracking") or {}
            )
            if row is not None:
                # Keep values fixed at call time (e.g. the SOL/USD rate of the baseline) when a
                # fresh research report without tracking metadata replaces the stored one.
                try:
                    stored = json.loads(row["report"] or "{}").get("early_tracking") or {}
                except (TypeError, ValueError):
                    stored = {}
                tracking = {**stored, **tracking}
            report_sol_usd = (updated_report.get("pre_migration") or {}).get("sol_usd")

            if row is None:
                if market_cap_sol is not None and report_sol_usd:
                    tracking["entry_sol_usd"] = report_sol_usd
                baseline_mode = (
                    "callout"
                    if market_cap_sol is not None
                    else "pending"
                )

                baseline_note = (
                    "Baseline recorded when the early-watch signal "
                    "was created."
                    if market_cap_sol is not None
                    else
                    "Initial market cap unavailable. Waiting for the "
                    "first valid observation."
                )

                tracking.update({
                    "baseline_mode": baseline_mode,
                    "baseline_note": baseline_note,
                })

                updated_report["early_tracking"] = tracking

                c.execute(
                    """
                    INSERT INTO early_signals(
                        mint, created_at, updated_at,
                        entry_market_cap_sol, current_market_cap_sol,
                        peak_multiple, min_multiple, score,
                        status, report
                    )
                    VALUES (?, ?, ?, ?, ?, 1, 1, ?, 'PENDING', ?)
                    """,
                    (
                        mint,
                        now,
                        now,
                        market_cap_sol,
                        market_cap_sol,
                        score,
                        json.dumps(updated_report, default=str),
                    ),
                )

                saved = dict(
                    c.execute(
                        """
                        SELECT *
                        FROM early_signals
                        WHERE mint = ?
                        """,
                        (mint,),
                    ).fetchone()
                )

                saved["baseline_mode"] = baseline_mode
                saved["entry_sol_usd"] = tracking.get("entry_sol_usd")
                saved["baseline_note"] = baseline_note

                return saved, True

            entry = row["entry_market_cap_sol"]
            current = row["current_market_cap_sol"]

            peak = float(row["peak_multiple"] or 1.0)
            low = float(row["min_multiple"] or 1.0)

            # Existing records may predate the early_tracking metadata.
            baseline_mode = tracking.get("baseline_mode")
            baseline_note = tracking.get("baseline_note")

            if entry is None and market_cap_sol is not None:
                # Delayed baseline: earlier performance cannot be recovered.
                entry = market_cap_sol
                current = market_cap_sol
                peak = 1.0
                low = 1.0

                baseline_mode = "delayed"
                if report_sol_usd:
                    tracking["entry_sol_usd"] = report_sol_usd
                baseline_note = (
                    "Baseline established after the original signal "
                    "because its market cap was unavailable. Performance "
                    "before this observation cannot be measured."
                )

            elif entry is not None and market_cap_sol is not None:
                multiple = market_cap_sol / entry

                if math.isfinite(multiple) and multiple > 0:
                    peak = max(peak, multiple)
                    low = min(low, multiple)
                    current = market_cap_sol

                if not baseline_mode:
                    baseline_mode = "callout"

                if not baseline_note:
                    baseline_note = (
                        "Baseline recorded when the early-watch signal "
                        "was created."
                    )

            else:
                if not baseline_mode:
                    baseline_mode = (
                        "pending" if entry is None else "callout"
                    )

                if not baseline_note:
                    baseline_note = (
                        "Waiting for the first valid market-cap observation."
                        if entry is None
                        else
                        "Baseline recorded when the early-watch signal "
                        "was created."
                    )

            tracking.update({
                "baseline_mode": baseline_mode,
                "baseline_note": baseline_note,
            })

            updated_report["early_tracking"] = tracking

            c.execute(
                """
                UPDATE early_signals
                SET updated_at = ?,
                    entry_market_cap_sol = ?,
                    current_market_cap_sol = ?,
                    peak_multiple = ?,
                    min_multiple = ?,
                    score = ?,
                    report = ?
                WHERE mint = ?
                """,
                (
                    now,
                    entry,
                    current,
                    peak,
                    low,
                    score,
                    json.dumps(updated_report, default=str),
                    mint,
                ),
            )

            saved = dict(
                c.execute(
                    """
                    SELECT *
                    FROM early_signals
                    WHERE mint = ?
                    """,
                    (mint,),
                ).fetchone()
            )

            # Convenience values for the Discord embed builder.
            saved["baseline_mode"] = baseline_mode
            saved["baseline_note"] = baseline_note
            saved["entry_sol_usd"] = tracking.get("entry_sol_usd")

            return saved, False

        return self._tx(tx)

    def add_early_plan_events(self, mint: str, kinds: list[str]) -> None:
        """Remember which trade-plan events were posted for an early watch (kept across re-research)."""
        def tx(c: sqlite3.Connection) -> None:
            row = c.execute("SELECT report FROM early_signals WHERE mint = ?", (mint,)).fetchone()
            if row is None:
                return
            try:
                report = json.loads(row["report"] or "{}")
            except (TypeError, ValueError):
                report = {}
            tracking = dict(report.get("early_tracking") or {})
            tracking["plan_sent"] = sorted(set(tracking.get("plan_sent") or []) | set(kinds))
            report["early_tracking"] = tracking
            c.execute("UPDATE early_signals SET report = ? WHERE mint = ?", (json.dumps(report, default=str), mint))
        self._tx(tx)

    def update_early_signal_status(
        self,
        mint: str,
        status: str,
        channel_id: Optional[int] = None,
        message_id: Optional[int] = None,
    ) -> None:
        self.execute(
            """
            UPDATE early_signals
            SET status = ?,
                channel_id = COALESCE(?, channel_id),
                message_id = COALESCE(?, message_id),
                updated_at = ?
            WHERE mint = ?
            """,
            (
                status,
                channel_id,
                message_id,
                time.time(),
                mint,
            ),
        )

    # -------------------------------------------------------------- alerts

    def recent_alert(
        self,
        mint: str,
        since: float,
    ) -> Optional[dict]:
        return self.fetchone(
            """
            SELECT *
            FROM alerts
            WHERE mint = ?
              AND created_at >= ?
              AND status IN ('PENDING', 'SENT', 'UNCERTAIN', 'DRY_RUN')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (mint, since),
        )

    def alerts_since(self, since: float) -> int:
        row = self.fetchone(
            """
            SELECT COUNT(*) AS n
            FROM alerts
            WHERE created_at >= ?
              AND status IN ('PENDING', 'SENT', 'UNCERTAIN', 'DRY_RUN')
            """,
            (since,),
        )

        return row["n"] if row else 0

    def create_alert_intent(
        self,
        mint: str,
        cooldown_since: float,
        **fields: Any,
    ) -> Optional[int]:
        """Atomically insert an alert intent while preventing duplicates."""
        now = time.time()

        def tx(c: sqlite3.Connection) -> Optional[int]:
            duplicate = c.execute(
                """
                SELECT id
                FROM alerts
                WHERE mint = ?
                  AND created_at >= ?
                  AND status IN ('PENDING', 'SENT', 'UNCERTAIN', 'DRY_RUN')
                """,
                (mint, cooldown_since),
            ).fetchone()

            if duplicate:
                return None

            columns = [
                "mint",
                "status",
                "state",
                "created_at",
                *fields.keys(),
            ]

            cursor = c.execute(
                f"""
                INSERT INTO alerts({','.join(columns)})
                VALUES ({','.join('?' * len(columns))})
                """,
                [
                    mint,
                    "PENDING",
                    "PUBLISHED",
                    now,
                    *fields.values(),
                ],
            )

            return cursor.lastrowid

        return self._tx(tx)

    def update_alert(self, alert_id: int, **fields: Any) -> None:
        for key in ("milestones_hit", "warnings"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key])

        if not fields:
            return

        columns = ", ".join(
            f"{key} = ?" for key in fields
        )

        self.execute(
            f"UPDATE alerts SET {columns} WHERE id = ?",
            [*fields.values(), alert_id],
        )

    def get_alert(self, alert_id: int) -> Optional[dict]:
        return self.fetchone(
            "SELECT * FROM alerts WHERE id = ?",
            (alert_id,),
        )

    def open_alerts(self) -> list[dict]:
        return self.fetchall(
            """
            SELECT *
            FROM alerts
            WHERE status IN ('SENT', 'DRY_RUN')
              AND state != 'CLOSED'
            ORDER BY created_at
            """
        )

    def pending_alerts(self) -> list[dict]:
        return self.fetchall(
            """
            SELECT *
            FROM alerts
            WHERE status IN ('PENDING', 'UNCERTAIN')
            ORDER BY created_at
            """
        )

    def add_alert_event(
        self,
        alert_id: int,
        kind: str,
        detail: Any = None,
    ) -> None:
        self.execute(
            """
            INSERT INTO alert_events(alert_id, ts, kind, detail)
            VALUES (?, ?, ?, ?)
            """,
            (
                alert_id,
                time.time(),
                kind,
                json.dumps(detail, default=str),
            ),
        )

    def add_price_point(
        self,
        alert_id: int,
        price: Optional[float],
        liq: Optional[float],
        mcap: Optional[float],
        status: str,
    ) -> None:
        self.execute(
            """
            INSERT INTO price_history(
                alert_id, ts, price, liquidity, market_cap, status
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (alert_id, time.time(), price, liq, mcap, status),
        )

    # ------------------------------------------------ delayed free-channel callouts

    def schedule_free_channel_post(
        self,
        alert_id: int,
        due_at: float,
        channel_id: int,
    ) -> None:
        self.execute(
            """
            INSERT INTO free_channel_posts(
                alert_id, due_at, channel_id, status
            )
            VALUES (?, ?, ?, 'PENDING')
            ON CONFLICT(alert_id) DO NOTHING
            """,
            (alert_id, due_at, channel_id),
        )

    def due_free_channel_posts(
        self,
        now: Optional[float] = None,
        limit: int = 20,
    ) -> list[dict]:
        return self.fetchall(
            """
            SELECT
                f.*,
                a.mint,
                a.status AS alert_status,
                a.state AS alert_state
            FROM free_channel_posts f
            JOIN alerts a ON a.id = f.alert_id
            WHERE f.status = 'PENDING'
              AND f.due_at <= ?
            ORDER BY f.due_at
            LIMIT ?
            """,
            (now or time.time(), limit),
        )

    def update_free_channel_post(
        self,
        alert_id: int,
        **fields: Any,
    ) -> None:
        if not fields:
            return

        columns = ", ".join(
            f"{key} = ?" for key in fields
        )

        self.execute(
            f"""
            UPDATE free_channel_posts
            SET {columns}
            WHERE alert_id = ?
            """,
            [*fields.values(), alert_id],
        )

    # -------------------------------------------------------------- outcomes

    HORIZONS = {
        "5m": 300,
        "15m": 900,
        "1h": 3600,
        "6h": 21600,
        "24h": 86400,
    }

    def schedule_outcomes(
        self,
        snapshot_id: int,
        mint: str,
        action: str,
        score: Optional[float],
        entry_price: Optional[float],
        entry_liq: Optional[float],
    ) -> None:
        now = time.time()

        def tx(c: sqlite3.Connection) -> None:
            # Avoid repeatedly evaluating the same mint/action within six hours.
            recent = c.execute(
                """
                SELECT 1
                FROM decision_tracking
                WHERE mint = ?
                  AND action = ?
                  AND decided_at > ?
                """,
                (mint, action, now - 6 * 3600),
            ).fetchone()

            if recent:
                return

            for horizon, seconds in self.HORIZONS.items():
                c.execute(
                    """
                    INSERT OR IGNORE INTO outcomes(
                        snapshot_id, horizon, mint, action, score,
                        decided_at, due_at, entry_price, entry_liq, status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        horizon,
                        mint,
                        action,
                        score,
                        now,
                        now + seconds,
                        entry_price,
                        entry_liq,
                        "PENDING" if entry_price else "NO_ENTRY",
                    ),
                )

            c.execute(
                """
                INSERT OR IGNORE INTO decision_tracking(
                    snapshot_id, mint, action, score, decided_at,
                    entry_price, entry_liq, max_price, min_price,
                    last_price, min_liq, done
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    mint,
                    action,
                    score,
                    now,
                    entry_price,
                    entry_liq,
                    entry_price,
                    entry_price,
                    entry_price,
                    entry_liq,
                    0 if entry_price else 1,
                ),
            )

        self._tx(tx)

    def due_outcomes(
        self,
        now: Optional[float] = None,
        limit: int = 300,
    ) -> list[dict]:
        return self.fetchall(
            """
            SELECT *
            FROM outcomes
            WHERE status = 'PENDING'
              AND due_at <= ?
            ORDER BY due_at
            LIMIT ?
            """,
            (now or time.time(), limit),
        )

    def open_tracking_count(self) -> int:
        return self.fetchone("SELECT COUNT(*) n FROM decision_tracking WHERE done = 0")["n"]

    def active_tracking(self, limit: int = 600) -> list[dict]:
        """Open tracking rows, least recently polled first.

        v2.5.3 and earlier took the 600 OLDEST rows every time. With more than 600 open rows
        (live DB 21 Oct: ~2,600) the newest decisions were never polled until they became the
        oldest, so a coin's first ~12 hours — when most runs and rugs happen — went unobserved.
        Order now: called-out coins every pass (at most the daily alert cap of rows), then rows
        never checked yet, then everything else least recently checked first, so every open row
        is reached within ceil(open / limit) passes.
        """
        return self.fetchall(
            """
            SELECT *
            FROM decision_tracking
            WHERE done = 0
            ORDER BY CASE WHEN action = 'AUTO_POST' THEN 0
                          WHEN last_polled_at IS NULL THEN 1 ELSE 2 END,
                     COALESCE(last_polled_at, 0), decided_at
            LIMIT ?
            """,
            (limit,),
        )

    # ---------------------------------------------------------- provider health

    def record_provider(
        self,
        provider: str,
        ok: bool,
        error: Optional[str] = None,
    ) -> None:
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))

        def tx(c: sqlite3.Connection) -> None:
            c.execute(
                """
                INSERT OR IGNORE INTO provider_health(provider)
                VALUES (?)
                """,
                (provider,),
            )

            row = c.execute(
                """
                SELECT budget_day
                FROM provider_health
                WHERE provider = ?
                """,
                (provider,),
            ).fetchone()

            if row["budget_day"] != day:
                c.execute(
                    """
                    UPDATE provider_health
                    SET budget_day = ?, budget_used = 0
                    WHERE provider = ?
                    """,
                    (day, provider),
                )

            if ok:
                c.execute(
                    """
                    UPDATE provider_health
                    SET ok_count = ok_count + 1,
                        consecutive_errors = 0,
                        last_ok = ?,
                        budget_used = budget_used + 1,
                        status = 'OK'
                    WHERE provider = ?
                    """,
                    (now, provider),
                )

            else:
                c.execute(
                    """
                    UPDATE provider_health
                    SET err_count = err_count + 1,
                        consecutive_errors = consecutive_errors + 1,
                        last_err = ?,
                        last_error = ?,
                        budget_used = budget_used + 1,
                        status = CASE
                            WHEN consecutive_errors + 1 >= 5
                            THEN 'DOWN'
                            ELSE 'DEGRADED'
                        END
                    WHERE provider = ?
                    """,
                    (now, (error or "")[:300], provider),
                )

        self._tx(tx)

    def record_social_links(self, mint: str, links, now: Optional[float] = None) -> None:
        now = now or time.time()
        rows = [(mint, l.kind, l.key, now) for l in links]
        for row in rows:
            self.execute(
                "INSERT OR IGNORE INTO social_links(mint, kind, key, first_seen) VALUES (?,?,?,?)", row)

    def social_link_reuse(self, mint: str, links, since: float) -> dict[str, int]:
        """For each link: how many OTHER mints used the same one since ``since``."""
        out: dict[str, int] = {}
        for l in links:
            row = self.fetchone(
                "SELECT COUNT(DISTINCT mint) AS n FROM social_links "
                "WHERE kind=? AND key=? AND mint<>? AND first_seen>=?",
                (l.kind, l.key, mint, since))
            n = row["n"] if row else 0
            if n:
                out[f"{l.kind}:{l.key}"] = n
        return out

    def prune_old_data(
        self,
        discovery_retention_days: float,
        snapshot_retention_days: float,
        now: Optional[float] = None,
        curve_retention_hours: float = 0.0,
    ) -> dict[str, int]:
        """Delete old high-volume rows that nothing still depends on.

        Kept regardless of age:
          * discovery events for any mint with an alert or an early-watch record
            (the bonding-curve key and creator live in those payloads);
          * research snapshots referenced by an alert, a pending outcome or an
            unfinished decision-tracking row, and the latest snapshot of every mint.
        A retention of 0 disables that part. Freed pages are reused by SQLite, so the
        file stops growing; it does not shrink without a manual VACUUM.
        """
        now = now or time.time()
        removed = {"discovery_events": 0, "research_snapshots": 0}

        def tx(c: sqlite3.Connection) -> None:
            if discovery_retention_days and discovery_retention_days > 0:
                cur = c.execute(
                    """
                    DELETE FROM discovery_events
                    WHERE COALESCE(last_ts, ts) < ?
                      AND mint NOT IN (SELECT mint FROM early_signals)
                      AND mint NOT IN (SELECT mint FROM alerts)
                    """,
                    (now - discovery_retention_days * 86400,),
                )
                removed["discovery_events"] = cur.rowcount
                c.execute("DELETE FROM social_links WHERE first_seen < ?", (now - 14 * 86400,))
            if curve_retention_hours and curve_retention_hours > 0:
                # bonding-curve events are read only for the last 5 minutes (research.py)
                cur = c.execute("DELETE FROM curve_snapshots WHERE ts < ?",
                                (now - curve_retention_hours * 3600,))
                if cur.rowcount:
                    removed["curve_snapshots"] = cur.rowcount
            if snapshot_retention_days and snapshot_retention_days > 0:
                cur = c.execute(
                    """
                    DELETE FROM research_snapshots
                    WHERE ts < ?
                      AND id NOT IN (SELECT snapshot_id FROM alerts WHERE snapshot_id IS NOT NULL)
                      AND id NOT IN (SELECT snapshot_id FROM outcomes WHERE status = 'PENDING')
                      AND id NOT IN (SELECT snapshot_id FROM decision_tracking WHERE done = 0)
                      AND id NOT IN (SELECT MAX(id) FROM research_snapshots GROUP BY mint)
                    """,
                    (now - snapshot_retention_days * 86400,),
                )
                removed["research_snapshots"] = cur.rowcount
                cur = c.execute("DELETE FROM funnel_events WHERE ts < ?",
                                (now - max(snapshot_retention_days, 7) * 86400,))
                if cur.rowcount:
                    removed["funnel_events"] = cur.rowcount

        self._tx(tx)
        return removed

    def provider_health(self) -> list[dict]:
        return self.fetchall(
            "SELECT * FROM provider_health ORDER BY provider"
        )

    def budget_used_today(self, provider: str) -> int:
        day = time.strftime("%Y-%m-%d", time.gmtime())

        row = self.fetchone(
            """
            SELECT budget_day, budget_used
            FROM provider_health
            WHERE provider = ?
            """,
            (provider,),
        )

        if not row or row["budget_day"] != day:
            return 0

        return row["budget_used"]

    # ------------------------------------------------------------- creator stats (v2.5.9)

    def record_creator_launch(self, creator: str, ts: Optional[float] = None) -> None:
        ts = ts or time.time()
        self.execute(
            "INSERT INTO creator_stats(creator, launches, migrated, first_ts, last_ts) VALUES (?, 1, 0, ?, ?) "
            "ON CONFLICT(creator) DO UPDATE SET launches = launches + 1, last_ts = MAX(last_ts, excluded.last_ts)",
            (creator, ts, ts))

    def record_creator_migration(self, creator: str, ts: Optional[float] = None) -> None:
        ts = ts or time.time()
        self.execute(
            "INSERT INTO creator_stats(creator, launches, migrated, first_ts, last_ts) VALUES (?, 0, 1, ?, ?) "
            "ON CONFLICT(creator) DO UPDATE SET migrated = migrated + 1, last_ts = MAX(last_ts, excluded.last_ts)",
            (creator, ts, ts))

    def creator_stats(self, creator: str) -> Optional[dict]:
        return self.fetchone("SELECT * FROM creator_stats WHERE creator = ?", (creator,))

    def has_discovery_source(self, mint: str, source: str) -> bool:
        return self.fetchone("SELECT 1 AS x FROM discovery_events WHERE mint = ? AND source = ? LIMIT 1",
                             (mint, source)) is not None

    def launch_creator(self, mint: str) -> Optional[str]:
        """The creator wallet from the coin's pump.fun launch event, if the feed saw it."""
        row = self.fetchone("SELECT payload FROM discovery_events WHERE mint = ? AND source = 'pumpportal_new' "
                            "ORDER BY ts LIMIT 1", (mint,))
        if not row:
            return None
        try:
            return json.loads(row["payload"]).get("traderPublicKey")
        except (TypeError, ValueError, AttributeError):
            return None

    def backfill_creator_stats(self) -> int:
        """Once: build creator_stats from the launch and migration events still in discovery_events."""
        if self.fetchone("SELECT value FROM meta WHERE key = 'creator_stats_backfilled'"):
            return 0
        launches: dict[str, list] = {}
        migrated: set[str] = {r["mint"] for r in self.fetchall(
            "SELECT DISTINCT mint FROM discovery_events WHERE source = 'pumpportal_migration'")}
        for r in self.fetchall("SELECT mint, MIN(ts) AS ts, payload FROM discovery_events "
                               "WHERE source = 'pumpportal_new' GROUP BY mint"):
            try:
                c = json.loads(r["payload"]).get("traderPublicKey")
            except (TypeError, ValueError, AttributeError):
                c = None
            if c:
                launches.setdefault(c, []).append((r["mint"], r["ts"]))
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                for c, rows in launches.items():
                    ts = [t for _, t in rows]
                    self._conn.execute(
                        "INSERT OR REPLACE INTO creator_stats(creator, launches, migrated, first_ts, last_ts) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (c, len(rows), sum(1 for m, _ in rows if m in migrated), min(ts), max(ts)))
                self._conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('creator_stats_backfilled', ?)",
                                   (str(time.time()),))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return len(launches)

    def prune_creator_stats(self, single_days: float = 3.0, any_days: float = 30.0,
                            now: Optional[float] = None) -> int:
        """One-off creators are forgotten after `single_days`; everyone after `any_days` of silence."""
        now = now or time.time()
        with self._lock:
            return self._conn.execute(
                "DELETE FROM creator_stats WHERE (launches <= 1 AND migrated = 0 AND last_ts < ?) OR last_ts < ?",
                (now - single_days * 86400, now - any_days * 86400)).rowcount

    # ------------------------------------------------------------- creator cache

    def get_creator_cache(
        self,
        creator: str,
        max_age: float,
    ) -> Optional[dict]:
        row = self.fetchone(
            "SELECT * FROM creator_cache WHERE creator = ?",
            (creator,),
        )

        if row and time.time() - row["ts"] <= max_age:
            return json.loads(row["data"])

        return None

    def put_creator_cache(
        self,
        creator: str,
        data: dict,
    ) -> None:
        self.execute(
            """
            INSERT OR REPLACE INTO creator_cache(creator, ts, data)
            VALUES (?, ?, ?)
            """,
            (
                creator,
                time.time(),
                json.dumps(data, default=str),
            ),
        )