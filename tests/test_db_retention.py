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
        assert r["market"]["price_usd"] == {"value": 0.0002} and "raw_provider_blob" not in r["market"]
        assert r["pool"] == {"address": "P"} and len(after[sid]) < len(before[sid]) / 2
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
    assert res["discovery_duplicates_merged"] == 4 and res["pruned"]["curve_snapshots"] == 1
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
