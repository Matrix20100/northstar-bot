"""Database retention never deletes rows that tracking still depends on (synthetic data)."""
import time

from conftest import addr


def test_prune_keeps_tracked_and_recent_rows(db):
    now = time.time()
    old = now - 10 * 86400
    tracked, plain, fresh = addr("tracked"), addr("plain"), addr("fresh")
    for mint, ts in ((tracked, old), (plain, old), (fresh, now)):
        db.execute("INSERT INTO discovery_events(mint, source, ts, payload) VALUES (?, 'pumpportal_new', ?, '{}')",
                   (mint, ts))
    db.create_or_update_early_signal(tracked, 100.0, 80.0, {"mint": tracked})
    for i, mint in enumerate((plain, plain, tracked)):
        db.execute("INSERT INTO research_snapshots(mint, ts, action, report) VALUES (?, ?, 'REJECT', '{}')",
                   (mint, old + i))
    removed = db.prune_old_data(4, 7, now=now)
    assert removed == {"discovery_events": 1, "research_snapshots": 1}
    left = {r["mint"] for r in db.fetchall("SELECT mint FROM discovery_events")}
    assert left == {tracked, fresh}
    # the latest snapshot of every mint survives
    assert {r["mint"] for r in db.fetchall("SELECT mint FROM research_snapshots")} == {plain, tracked}


def test_zero_retention_disables_pruning(db):
    db.execute("INSERT INTO discovery_events(mint, source, ts, payload) VALUES ('x', 's', 1, '{}')")
    assert db.prune_old_data(0, 0) == {"discovery_events": 0, "research_snapshots": 0}
