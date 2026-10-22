"""v2.5.4 database size: repeat discovery events, curve retention, prefilter compaction and
round-robin outcome tracking. Synthetic rows only; the live-copy measurements are in retention.py."""
from __future__ import annotations

import asyncio
import json
import time

from conftest import addr, make_pair
from northstar import retention as RT
from northstar.config import Settings
from northstar.performance import OutcomeTracker


def _events(db, mint, source="dexscreener_boost"):
    return db.fetchall("SELECT * FROM discovery_events WHERE mint=? AND source=? ORDER BY id", (mint, source))


# ------------------------------------------------------------------ discovery repeats
def test_an_identical_repeat_is_counted_not_stored(db):
    m = addr("boost")
    assert db.upsert_discovery(m, "dexscreener_boost", 5.0, payload={"amount": 10}) is True
    assert db.upsert_discovery(m, "dexscreener_boost", 5.0, payload={"amount": 10}) is False
    rows = _events(db, m)
    assert len(rows) == 1 and rows[0]["seen"] == 2 and rows[0]["last_ts"] >= rows[0]["ts"]
    db.upsert_discovery(m, "dexscreener_boost", 5.0, payload={"amount": 30})      # changed: a new event
    db.upsert_discovery(m, "dexscreener_profile", 5.0, payload={"amount": 30})    # another feed: its own row
    assert len(_events(db, m)) == 2 and len(_events(db, m, "dexscreener_profile")) == 1
    cand = db.get_candidate(m)
    assert set(json.loads(cand["sources"])) == {"dexscreener_boost", "dexscreener_profile"}


def _raw(db, mint, source, ts, payload):
    db.execute("INSERT INTO discovery_events(mint, source, ts, payload) VALUES (?,?,?,?)",
               (mint, source, ts, json.dumps(payload)))


def test_collapse_merges_old_repeats_and_keeps_what_readers_need(db):
    m, t = addr("old"), time.time() - 3600
    _raw(db, m, "pumpportal_new", t, {"traderPublicKey": "dev", "bondingCurveKey": "k"})
    for i, desc in enumerate(["A", "A", "B", "A", "A"]):        # the latest profile is A again
        _raw(db, m, "dexscreener_profile", t + 10 + i, {"description": desc})
    for i in range(50):
        _raw(db, m, "dexscreener_boost", t + i, {"amount": 10})
    removed = RT.collapse_discovery_duplicates(db)
    assert removed == 3 + 49
    boost = _events(db, m)
    assert len(boost) == 1 and boost[0]["seen"] == 50 and boost[0]["last_ts"] == t + 49
    latest = db.fetchone("SELECT payload FROM discovery_events WHERE mint=? AND source IN "
                         "('dexscreener_profile','dexscreener_cto') ORDER BY COALESCE(last_ts, ts) DESC LIMIT 1", (m,))
    assert json.loads(latest["payload"]) == {"description": "A"}
    launch = db.fetchone("SELECT payload FROM discovery_events WHERE mint=? AND source='pumpportal_new' "
                         "ORDER BY ts LIMIT 1", (m,))
    assert json.loads(launch["payload"])["traderPublicKey"] == "dev"
    assert RT.collapse_discovery_duplicates(db) == 0, "one-time: flagged in meta"


def test_a_repeatedly_seen_event_is_not_pruned_while_still_seen(db):
    now = time.time()
    m = addr("still")
    _raw(db, m, "dexscreener_boost", now - 10 * 86400, {"amount": 1})
    db.execute("UPDATE discovery_events SET last_ts=? WHERE mint=?", (now - 60, m))
    _raw(db, addr("gone"), "dexscreener_boost", now - 10 * 86400, {"amount": 1})
    db.prune_old_data(4, 0, now=now)
    assert {r["mint"] for r in db.fetchall("SELECT mint FROM discovery_events")} == {m}


# ------------------------------------------------------------------ curve retention
def test_curve_events_older_than_retention_are_removed(db):
    now = time.time()
    for i, age_h in enumerate((0.05, 5, 7, 30)):
        db.execute("INSERT INTO curve_snapshots(mint, ts, signature, payload) VALUES (?,?,?,'{}')",
                   (addr("c"), now - age_h * 3600, f"sig{i}"))
    removed = db.prune_old_data(0, 0, now=now, curve_retention_hours=6)
    assert removed["curve_snapshots"] == 2
    assert len(db.curve_history(addr("c"), now - 300)) == 1, "the 5-minute window research reads is intact"
    assert db.prune_old_data(0, 0, now=now) == {"discovery_events": 0, "research_snapshots": 0}


# ------------------------------------------------------------------ prefilter compaction
PREFILTER = {"mint": "M", "ts": 1.0, "sources": {"dexscreener": "OK"}, "missing": [],
             "pool": {"address": "P", "reason": "long text " * 20, "pools_for_mint": 6},
             "identity": {"symbol": "X", "name": "X coin", "dex": "pumpswap"},
             "market": {"price_usd": {"value": 0.0002, "status": "OBSERVED", "source": "dexscreener"},
                        "liquidity_usd": {"value": 4000.0, "status": "OBSERVED", "source": "dexscreener"},
                        "volume": {"m5": {"value": 10.0, "status": "OBSERVED"}},
                        "raw_provider_blob": {"value": "x" * 500}},
             "decision": {"action": "REJECT", "reasons": ["prefilter: liquidity $4,000 below prefilter $5,000"]},
             "risk": "INCOMPLETE", "final_action": "REJECT",
             "reasons": ["prefilter: liquidity $4,000 below prefilter $5,000"]}


def _snap(db, mint, ts, report, action="REJECT", score=None):
    return db.execute("INSERT INTO research_snapshots(mint, ts, action, score, report) VALUES (?,?,?,?,?)",
                      (mint, ts, action, score, json.dumps(report)))


def test_compaction_keeps_reasons_and_decision_and_skips_everything_else(db):
    now = time.time()
    old = now - 2 * 86400
    m, other = addr("pre"), addr("analysed")
    a = _snap(db, m, old, PREFILTER)
    b = _snap(db, m, old + 1, PREFILTER)                       # alert points here: untouched
    c = _snap(db, m, old + 2, PREFILTER)
    latest = _snap(db, m, now - 60, PREFILTER)                  # latest of the coin: untouched
    analysed = _snap(db, other, old, {"reasons": ["REJECT — score 40/100"], "gates": [], "unified": {}}, score=40)
    db.execute("INSERT INTO alerts(mint, status, state, created_at, snapshot_id) VALUES (?,?,?,?,?)",
               (m, "SENT", "OPEN", old, b))
    before = {r["id"]: r["report"] for r in db.fetchall("SELECT id, report FROM research_snapshots")}
    assert RT.compact_snapshots(db, 24, now) == 2
    after = {r["id"]: r["report"] for r in db.fetchall("SELECT id, report FROM research_snapshots")}
    for sid in (a, c):
        r = json.loads(after[sid])
        assert r["compacted"] == 1 and r["reasons"] == PREFILTER["reasons"] and r["decision"] == PREFILTER["decision"]
        assert r["market"]["price_usd"] == {"value": 0.0002} and r["market"]["volume"] == {"m5": {"value": 10.0}}
        assert set(r["market"]) == set(PREFILTER["market"]), "every market key stays; only metadata goes"
        assert r["pool"] == {"address": "P"} and len(after[sid]) < len(before[sid])
    for sid in (b, latest, analysed):
        assert after[sid] == before[sid]
    assert RT.compact_snapshots(db, 24, now) == 0 and RT.compact_snapshots(db, 0, now) == 0


def test_estimate_and_maintain_agree_and_are_idempotent(db):
    now = time.time()
    s = Settings(_env_file=None, curve_retention_hours=6, snapshot_compact_hours=24)
    for i in range(5):
        _raw(db, addr("dup"), "dexscreener_boost", now - 100 + i, {"amount": 1})
    db.execute("INSERT INTO curve_snapshots(mint, ts, signature, payload) VALUES (?,?,?,'{}')",
               (addr("c"), now - 30 * 3600, "s"))
    _snap(db, addr("p"), now - 3 * 86400, PREFILTER)
    _snap(db, addr("p"), now - 2 * 86400, PREFILTER)     # old, but the coin's latest: kept as it is
    est = RT.estimate(db, s, now)
    assert est["discovery_duplicate_rows"] == 4 and est["curve_events_older_than_retention"] == 1
    assert est["prefilter_reports_to_compact"] == 1
    res = RT.maintain(db, s, now)
    assert res["discovery_duplicates_merged"] == 4 and res["curve_events_removed"] == 1
    assert res["snapshots_compacted"] == 1
    again = RT.maintain(db, s, now)
    assert again["discovery_duplicates_merged"] == 0 and again["snapshots_compacted"] == 0


def test_vacuum_shrinks_the_file(tmp_path):
    from northstar.db import Database
    d = Database(tmp_path / "v.db")
    d.open()
    for i in range(3000):
        d.execute("INSERT INTO curve_snapshots(mint, ts, signature, payload) VALUES (?,?,?,?)",
                  ("m", 1.0, f"s{i}", "x" * 500))
    d.execute("DELETE FROM curve_snapshots")
    d.close()
    r = RT.vacuum(str(tmp_path / "v.db"))
    assert r["after_bytes"] < r["before_bytes"]


# ------------------------------------------------------------------ bounded, fair tracking
def _track(db, sid, decided_at):
    db.execute("INSERT INTO research_snapshots(id, mint, ts, action, pool_address, report) VALUES (?,?,?,?,?,'{}')",
               (sid, addr(f"t{sid}"), decided_at, "REJECT", make_pair().pair_address))
    db.execute("INSERT INTO decision_tracking(snapshot_id, mint, action, decided_at, entry_price, max_price, "
               "min_price) VALUES (?,?,?,?,1.0,1.0,1.0)", (sid, addr(f"t{sid}"), "REJECT", decided_at))


def test_tracking_is_polled_round_robin_not_oldest_first(db):
    now = time.time()
    for sid in (1, 2, 3):
        _track(db, sid, now - 3600 + sid)

    class Dex:
        async def tokens_batch(self, mints):
            return [make_pair(base_mint=m, price_usd=1.5) for m in mints]

    tr = OutcomeTracker(db, Dex())
    tr.poll_limit = 2
    assert [r["snapshot_id"] for r in db.active_tracking(2)] == [1, 2]
    asyncio.run(tr.run_once(now))
    assert [r["snapshot_id"] for r in db.active_tracking(2)] == [3, 1], "the newest row is not starved"
    polled = {r["snapshot_id"]: r for r in db.fetchall("SELECT * FROM decision_tracking")}
    assert polled[1]["last_polled_at"] == now and polled[3]["last_polled_at"] is None
    assert polled[1]["samples"] == 1 and polled[1]["max_price"] == 1.5


# ------------------------------------------------------------------ review fixes (v2.5.4 safety check)
def test_compaction_never_changes_what_the_brain_reads():
    from northstar import brain as B
    r = json.loads(json.dumps(PREFILTER))
    r["market"].update({"fetched_at": {"value": 1_700_000_000.0, "status": "OBSERVED"},
                        "boosts_active": {"value": 3, "status": "OBSERVED"},
                        "pair_created_at": {"value": 1_699_990_000.0, "status": "OBSERVED"},
                        "market_cap": {"value": 90_000.0, "status": "OBSERVED", "note": "provider field"},
                        "txns": {"h1": {"buys": 40, "sells": 10}}, "price_change": {"h1": 12.0}})
    slim = RT.compact_prefilter_report(r)
    assert B.vector(slim) == B.vector(r)
    assert set(slim["market"]) == set(r["market"])


def test_collapse_in_chunks_matches_one_pass_and_survives_a_crash(db, monkeypatch):
    t = time.time() - 3600
    for k in range(5):
        for i in range(4):
            _raw(db, addr(f"m{k}"), "dexscreener_boost", t + i, {"amount": k})
    calls = {"n": 0}
    real = db._tx

    def flaky(fn):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated crash")
        return real(fn)

    monkeypatch.setattr(db, "_tx", flaky)
    try:
        RT.collapse_discovery_duplicates(db, max_rows=4)
    except RuntimeError:
        pass
    assert not db.fetchone("SELECT 1 FROM meta WHERE key=?", (RT.DEDUP_FLAG,)), "not flagged until complete"
    monkeypatch.setattr(db, "_tx", real)
    RT.collapse_discovery_duplicates(db, max_rows=8)
    rows = db.fetchall("SELECT mint, seen, last_ts FROM discovery_events ORDER BY mint")
    assert len(rows) == 5 and all(r["seen"] == 4 and r["last_ts"] == t + 3 for r in rows)


def test_curve_prune_runs_in_chunks(db):
    now = time.time()
    for i in range(7):
        db.execute("INSERT INTO curve_snapshots(mint, ts, signature, payload) VALUES (?,?,?,'{}')",
                   ("m", now - 10 * 3600 - i, f"s{i}"))
    db.execute("INSERT INTO curve_snapshots(mint, ts, signature, payload) VALUES ('m', ?, 'new', '{}')", (now,))
    assert RT.prune_curve(db, 6, now, chunk=2) == 7
    assert db.fetchone("SELECT COUNT(*) n FROM curve_snapshots")["n"] == 1
    assert RT.prune_curve(db, 0, now) == 0


def test_retention_settings_defaults_bounds_and_example_file():
    import pytest
    from pydantic import ValidationError
    d = Settings(_env_file=None)
    assert (d.curve_retention_hours, d.snapshot_compact_hours, d.tracking_poll_limit) == (6.0, 24.0, 600)
    odd = Settings(_env_file=None, curve_retention_hours=-1, snapshot_compact_hours=-5, tracking_poll_limit=-3)
    assert (odd.curve_retention_hours, odd.snapshot_compact_hours, odd.tracking_poll_limit) == (0.0, 0.0, 600)
    assert Settings(_env_file=None, tracking_poll_limit=10 ** 6).tracking_poll_limit == 1200
    with pytest.raises(ValidationError):
        Settings(_env_file=None, curve_retention_hours="six")
    from pathlib import Path
    ex = Settings(_env_file=str(Path(__file__).resolve().parents[1] / ".env.example"))
    assert (ex.curve_retention_hours, ex.snapshot_compact_hours, ex.tracking_poll_limit) == (6.0, 24.0, 600)
    assert (ex.discovery_retention_days, ex.snapshot_retention_days) == (4.0, 7.0)


def test_env_variables_reach_the_retention_settings(monkeypatch):
    monkeypatch.setenv("NORTHSTAR_CURVE_RETENTION_HOURS", "12")
    monkeypatch.setenv("NORTHSTAR_SNAPSHOT_COMPACT_HOURS", "0")
    monkeypatch.setenv("NORTHSTAR_TRACKING_POLL_LIMIT", "900")
    s = Settings(_env_file=None)
    assert (s.curve_retention_hours, s.snapshot_compact_hours, s.tracking_poll_limit) == (12.0, 0.0, 900)


def test_vacuum_refuses_while_the_bot_holds_its_lock(tmp_path):
    import pytest
    from northstar.db import Database
    from northstar.engine import InstanceLock
    d = Database(tmp_path / "v.db")
    d.open()
    d.close()
    lk = InstanceLock(tmp_path / "northstar.lock")
    lk.acquire()
    try:
        with pytest.raises(RuntimeError, match="stop the bot"):
            RT.vacuum(str(tmp_path / "v.db"), str(tmp_path / "northstar.lock"))
    finally:
        lk.release()
    assert RT.vacuum(str(tmp_path / "v.db"), str(tmp_path / "northstar.lock"))["after_bytes"] > 0


def test_every_open_row_is_polled_and_new_rows_are_not_starved(db):
    now = time.time()
    for sid in range(1, 8):
        _track(db, sid, now - 7200 + sid)

    class Dex:
        calls = 0

        async def tokens_batch(self, mints):
            Dex.calls += 1
            return [make_pair(base_mint=m, price_usd=1.1) for m in mints]

    tr = OutcomeTracker(db, Dex())
    tr.poll_limit = 3
    for k in range(3):
        asyncio.run(tr.run_once(now + k))
    samples = {r["snapshot_id"]: r["samples"] for r in db.fetchall("SELECT snapshot_id, samples FROM decision_tracking")}
    assert min(samples.values()) >= 1 and max(samples.values()) - min(samples.values()) <= 1
    _track(db, 99, now + 10)                                    # a brand-new decision
    assert db.active_tracking(3)[0]["snapshot_id"] == 99
    tr.poll_limit = -1                                          # a bad value never means "all rows"
    assert len(db.active_tracking(600)) == 8
    asyncio.run(tr.run_once(now + 20))
    assert Dex.calls == 4


# ------------------------------------------------------------------ final review: called coins, coverage, resume
def _track_action(db, sid, decided_at, action):
    _track(db, sid, decided_at)
    db.execute("UPDATE decision_tracking SET action=? WHERE snapshot_id=?", (action, sid))


def test_a_new_callout_is_checked_on_the_very_next_pass_and_every_pass(db):
    now = time.time()
    for sid in range(1, 21):                                  # a backlog of never-checked rejections
        _track(db, sid, now - 3600 + sid)
    _track_action(db, 99, now, "AUTO_POST")                    # the newest row: a callout

    class Dex:
        async def tokens_batch(self, mints):
            return [make_pair(base_mint=m, price_usd=1.2) for m in mints]

    tr = OutcomeTracker(db, Dex())
    tr.poll_limit = 5
    for k in range(4):
        assert db.active_tracking(5)[0]["snapshot_id"] == 99
        asyncio.run(tr.run_once(now + k))
    rows = {r["snapshot_id"]: r["samples"] for r in db.fetchall("SELECT snapshot_id, samples FROM decision_tracking")}
    assert rows[99] == 4
    assert sum(1 for sid in range(1, 21) if rows[sid] == 1) == 16, "the backlog still advances 4 rows a pass"
    for k in range(4, 8):
        asyncio.run(tr.run_once(now + k))
    rows = {r["snapshot_id"]: r["samples"] for r in db.fetchall("SELECT snapshot_id, samples FROM decision_tracking")}
    assert min(rows[sid] for sid in range(1, 21)) >= 1, "every older open decision is reached"


def test_coverage_numbers_and_warning(db):
    import logging
    from northstar.performance import MAX_POLL_LIMIT, checks_per_day
    # the 21 Oct backup had 2,100-3,300 open rows: with 600 a pass every 2 min, 120-180 checks a day
    assert checks_per_day(3300, 600, 120) == 120.0 and checks_per_day(2100, 600, 120) == 180.0
    assert checks_per_day(2648, 600, 120) == 144.0
    assert checks_per_day(0, 600, 120) == float("inf") and MAX_POLL_LIMIT == 1200
    now = time.time()
    for sid in range(1, 40):
        _track(db, sid, now - 60)
    tr = OutcomeTracker(db, None)
    tr.poll_interval = 3600.0                                  # absurdly slow: must warn
    seen = []

    class H(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    lg, h = logging.getLogger("northstar.performance"), H(level=logging.WARNING)
    lg.addHandler(h)
    try:
        tr._check_coverage(1, now)
        tr._check_coverage(1, now + 10)                        # at most once an hour
    finally:
        lg.removeHandler(h)
    assert sum("NORTHSTAR_TRACKING_POLL_LIMIT" in m for m in seen) == 1


def test_cleanup_while_new_events_arrive_keeps_every_count(db):
    """The bot keeps inserting while the one-time merge runs; nothing is double-counted or lost."""
    m, t = addr("live"), time.time() - 600
    for i in range(6):
        _raw(db, m, "dexscreener_boost", t + i, {"amount": 5})
    db.upsert_discovery(m, "dexscreener_boost", 1.0, payload={"amount": 5})   # arrives before the merge
    RT.collapse_discovery_duplicates(db, max_rows=2)
    db.upsert_discovery(m, "dexscreener_boost", 1.0, payload={"amount": 5})   # arrives after the merge
    rows = _events(db, m)
    assert len(rows) == 1 and rows[0]["seen"] == 8
    assert RT.collapse_discovery_duplicates(db, force=True) == 0 and _events(db, m)[0]["seen"] == 8


def test_db_command_is_a_dry_run_by_default(tmp_path, monkeypatch, capsys):
    from northstar import cli
    from northstar.config import get_settings
    from northstar.db import Database
    monkeypatch.setenv("NORTHSTAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    d = Database(tmp_path / "northstar.db")
    d.open()
    for i in range(3):
        d.execute("INSERT INTO discovery_events(mint, source, ts, payload) VALUES ('m', 's', ?, '{}')", (i,))
    d.close()
    assert cli.main(["db", "--vacuum"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "--vacuum ignored" in out
    d = Database(tmp_path / "northstar.db")
    d.open()
    assert d.fetchone("SELECT COUNT(*) n FROM discovery_events")["n"] == 3, "dry run changed nothing"
    d.close()
    get_settings.cache_clear()


# ------------------------------------------------------------------ release: protected data, disk guard
def test_retention_never_touches_callout_or_learning_data(db):
    """Aggressive settings, everything old: callouts, outcomes and learning rows all survive."""
    from northstar import learning as L
    L.init_schema(db)
    old = time.time() - 60 * 86400
    m = addr("called")
    sid = _snap(db, m, old, PREFILTER)
    _snap(db, addr("other"), old, PREFILTER)
    _snap(db, addr("other"), old + 1, PREFILTER)
    aid = db.execute("INSERT INTO alerts(mint, status, state, created_at, snapshot_id) VALUES (?,?,?,?,?)",
                     (m, "SENT", "OPEN", old, sid))
    db.execute("INSERT INTO price_history(alert_id, ts, price, status) VALUES (?,?,?,?)", (aid, old, 1.0, "OK"))
    db.execute("INSERT INTO decision_tracking(snapshot_id, mint, action, decided_at, entry_price, max_price, "
               "min_price, samples, done, brain_vector) VALUES (?,?,?,?,1,2.5,0.9,300,1,'{}')",
               (sid, m, "AUTO_POST", old))
    db.execute("INSERT INTO outcomes(snapshot_id, horizon, mint, action, decided_at, due_at, status) "
               "VALUES (?,?,?,?,?,?,?)", (sid, "24h", m, "AUTO_POST", old, old + 86400, "OBSERVED"))
    db.execute("INSERT INTO shadow_calls(mint, ts, features, evaluated, path, label) VALUES (?,?,'{}',1,'[]','RUNNER')",
               (m, time.time() - 86400))
    db.create_or_update_early_signal(m, 100.0, 80.0, {"mint": m})
    _raw(db, m, "pumpportal_new", old, {"traderPublicKey": "dev"})
    protected = ("alerts", "price_history", "decision_tracking", "outcomes", "shadow_calls", "early_signals")
    before = {t: db.fetchall(f"SELECT * FROM {t}") for t in protected}
    s = Settings(_env_file=None, discovery_retention_days=1, snapshot_retention_days=1,
                 curve_retention_hours=1, snapshot_compact_hours=1)
    RT.maintain(db, s)
    assert {t: db.fetchall(f"SELECT * FROM {t}") for t in protected} == before
    snap = db.fetchone("SELECT report FROM research_snapshots WHERE id=?", (sid,))
    assert json.loads(snap["report"]) == PREFILTER, "the callout's snapshot is kept whole"
    assert db.fetchone("SELECT COUNT(*) n FROM discovery_events WHERE mint=?", (m,))["n"] == 1, \
        "launch event of a called coin is kept (creator, curve key)"


def test_vacuum_refuses_without_enough_free_disk(tmp_path, monkeypatch):
    import collections
    import shutil
    import pytest
    from northstar.db import Database
    d = Database(tmp_path / "v.db")
    d.open()
    d.close()
    Usage = collections.namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(10, 10, 1))
    before = (tmp_path / "v.db").read_bytes()
    with pytest.raises(RuntimeError, match="not enough free disk"):
        RT.vacuum(str(tmp_path / "v.db"))
    assert (tmp_path / "v.db").read_bytes() == before


def test_startup_vacuum_only_when_worth_it_and_never_raises(tmp_path):
    from northstar.db import Database
    p = tmp_path / "s.db"
    assert RT.vacuum_on_start(str(tmp_path / "missing.db")) is None
    d = Database(p)
    d.open()
    for i in range(4000):
        d.execute("INSERT INTO curve_snapshots(mint, ts, signature, payload) VALUES (?,?,?,?)",
                  ("m", 1.0, f"s{i}", "x" * 900))
    d.execute("DELETE FROM curve_snapshots")
    d.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    d.close()
    assert RT.vacuum_on_start(str(p), min_share=0.2, min_bytes=10 ** 12) is None, "too little to gain"
    r = RT.vacuum_on_start(str(p), min_share=0.2, min_bytes=1)
    assert r and r["after_bytes"] < r["before_bytes"]
    assert RT.vacuum_on_start(str(p), min_share=0.2, min_bytes=1) is None, "nothing left to free"
    assert Settings(_env_file=None).db_vacuum_on_start is False


# ------------------------------------------------------------------ v2.5.5: automatic size control
def _full_report():
    from test_unified_engine import report as clean_report
    from northstar.analysis import unified as U
    r = clean_report()
    r["holders"]["holders"] = [{"owner": addr(f"h{i}"), "pct": 1.0 + i} for i in range(20)]
    r["trades"]["sample"] = [{"sig": f"s{i}", "usd": 10.0 * i} for i in range(30)]
    r["clusters"]["bubbles"] = [{"id": i, "pct": 0.5} for i in range(15)]
    r["pre_migration_rpc_diagnostics"] = {"calls": list(range(50))}
    r["unified"] = U.evaluate(r, Settings(_env_file=None))
    return r


def test_old_analysis_reports_shrink_without_changing_what_learning_reads():
    from northstar import brain as B
    from northstar import learning as L
    from northstar.analysis.verdict import assess
    r = _full_report()
    slim = RT.compact_analysis_report(json.loads(json.dumps(r)))
    assert slim["compacted"] == 2 and len(json.dumps(slim)) < len(json.dumps(r))
    assert "checks" not in slim["unified"] and "holders" not in slim["holders"] and "sample" not in slim["trades"]
    assert slim["clusters"] == r["clusters"], "the bubble map feeds the brain: kept whole"
    assert slim.get("reasons") == r.get("reasons") and slim["gates"] == r["gates"]
    assert slim["unified"]["decision"] == r["unified"]["decision"] and slim["unified"]["vetoes"] == r["unified"]["vetoes"]
    assert B.vector(slim, 1.0) == B.vector(r, 1.0)
    assert assess(slim)["red"] == assess(r)["red"] and assess(slim)["level"] == assess(r)["level"]
    assert L.features_from(slim, None, None) == L.features_from(r, None, None)
    assert RT.compact_analysis_report(slim) is None, "idempotent"


def test_repeated_snapshots_are_thinned_but_history_and_references_survive(db):
    from northstar import learning as L
    L.init_schema(db)
    now = time.time()
    old = now - 3 * 86400
    m = addr("rep")
    rej = {"reasons": ["REJECT — score 30/100"], "final": {"reason_class": "WEAK_OPPORTUNITY"}}
    watch = {"reasons": ["WATCH — score 55/100"], "final": {"reason_class": "BELOW_CALL_LINE"}}
    ids = [_snap(db, m, old + i * 60, rep, action=a) for i, (rep, a) in enumerate(
        [(rej, "REJECT"), (rej, "REJECT"), (rej, "REJECT"), (watch, "WATCHLIST"), (watch, "WATCHLIST"),
         (rej, "REJECT"), (rej, "REJECT"), (rej, "REJECT")])]
    db.execute("INSERT INTO decision_tracking(snapshot_id, mint, action, decided_at) VALUES (?,?,?,?)",
               (ids[2], m, "REJECT", old))                                    # tracked: kept
    db.execute("INSERT INTO shadow_calls(mint, ts, features) VALUES (?,?,'{}')", (m, old + 5 * 60 + 5))  # pairs with ids[6]
    removed = RT.thin_repeated_snapshots(db, 24, now)
    left = [r["id"] for r in db.fetchall("SELECT id FROM research_snapshots ORDER BY id")]
    assert left == [ids[0], ids[2], ids[3], ids[5], ids[6], ids[7]] and removed == 2
    assert RT.thin_repeated_snapshots(db, 24, now) == 0
    fresh = _snap(db, m, now - 60, rej)                                       # a newer, recent repeat
    assert RT.thin_repeated_snapshots(db, 24, now) == 1, "the old repeat is no longer the latest"
    left = [r["id"] for r in db.fetchall("SELECT id FROM research_snapshots ORDER BY id")]
    assert fresh in left and ids[7] not in left, "recent snapshots are never thinned"


def test_callout_price_history_keeps_peaks_lows_and_event_order(db):
    now = time.time()
    t0 = now - 3 * 86400
    aid = db.execute("INSERT INTO alerts(mint, status, state, created_at, entry_price) VALUES (?,?,?,?,1.0)",
                     (addr("call"), "SENT", "CLOSED", t0))
    prices = [1.0 + 0.01 * (i % 7) for i in range(300)]
    prices[40], prices[120], prices[200] = 2.4, 0.55, 0.2                # 2x first, then 0.6x, then 0.3x
    for i, pr in enumerate(prices):
        db.execute("INSERT INTO price_history(alert_id, ts, price, status) VALUES (?,?,?,?)",
                   (aid, t0 + i * 10, pr, "OK"))
    recent = [db.execute("INSERT INTO price_history(alert_id, ts, price, status) VALUES (?,?,?,?)",
                         (aid, now - 600 + i, 1.1, "OK")) for i in range(30)]
    removed = RT.downsample_price_history(db, 24, 5, now)
    rows = db.fetchall("SELECT ts, price FROM price_history WHERE alert_id=? ORDER BY ts", (aid,))
    assert removed > 200 and len(rows) < 80
    seq = [r["price"] for r in rows]
    assert max(seq) == 2.4 and min(seq) == 0.2
    assert seq.index(2.4) < seq.index(0.55) < seq.index(0.2), "order of 2x, 0.6x and 0.3x kept"
    assert len([r for r in rows if r["ts"] >= now - 600]) == 30, "the last 24 h keep every update"
    assert RT.downsample_price_history(db, 24, 5, now) == 0, "idempotent"
    assert RT.downsample_price_history(db, 0, 5, now) == 0


def test_market_snapshots_keep_the_day_research_reads(db):
    now = time.time()
    for age_h in (1, 20, 30, 100):
        db.execute("INSERT INTO market_snapshots(mint, ts, price_usd) VALUES ('m', ?, 1.0)", (now - age_h * 3600,))
    assert RT.prune_market_snapshots(db, 10, now) == 2, "never under 25 h, whatever the setting"
    assert RT.prune_market_snapshots(db, 48, now) == 0
    assert len(db.market_history("m", now - 24 * 3600)) == 2


def test_space_goes_back_to_the_disk_automatically(tmp_path):
    from northstar.db import Database
    p = tmp_path / "auto.db"
    d = Database(p)
    d.open()
    assert d.fetchone("PRAGMA auto_vacuum")["auto_vacuum"] == 2, "new databases can shrink themselves"
    for i in range(3000):
        d.execute("INSERT INTO curve_snapshots(mint, ts, signature, payload) VALUES (?,?,?,?)",
                  ("m", 1.0, f"s{i}", "x" * 900))
    d.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    big = p.stat().st_size
    res = RT.maintain(d, Settings(_env_file=None))
    d.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert res["curve_events_removed"] == 3000 and res.get("pages_returned_to_disk", 0) > 0
    assert p.stat().st_size < big / 2
    d.close()


def test_an_old_database_switches_to_automatic_shrinking_after_one_vacuum(tmp_path):
    import sqlite3
    from northstar.db import Database
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY, mint TEXT, status TEXT, state TEXT, created_at REAL)")
    c.execute("INSERT INTO alerts(mint, status, state, created_at) VALUES ('kept', 'SENT', 'OPEN', 1)")
    c.commit()
    c.close()
    d = Database(p)
    d.open()
    assert d.fetchone("PRAGMA auto_vacuum")["auto_vacuum"] == 0
    assert RT.incremental_vacuum(d) == 0, "nothing to do before the switch"
    d.close()
    RT.vacuum(str(p))
    d = Database(p)
    d.open()
    assert d.fetchone("PRAGMA auto_vacuum")["auto_vacuum"] == 2
    assert d.fetchone("SELECT mint FROM alerts")["mint"] == "kept", "existing records survive"
    d.close()
