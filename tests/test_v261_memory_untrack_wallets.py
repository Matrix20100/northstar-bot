"""v2.6.1: compact training memory for the two brains, manual untrack, and the wallet-network gate."""
from __future__ import annotations

import json
import time

import pytest

from conftest import MINT, addr
from northstar import brain as B
from northstar import learning as L
from northstar import training_store as TS
from northstar.analysis import unified as U
from northstar.analysis.wallets import ClusterResult, wallet_network_gate
from northstar.config import Settings
from northstar.models import DataStatus, GateStatus


# ------------------------------------------------------------------ training memory
def test_pack_round_trips_and_keeps_unknowns():
    names = ["a", "b", "c"]
    v = TS.unpack(TS.pack({"a": 1.5, "b": None, "c": "x"}, names), names)
    assert v == {"a": 1.5, "b": None, "c": None}
    assert len(TS.pack({n: 1.0 for n in B.ALL_FEATURES}, list(B.ALL_FEATURES))) == 4 * len(B.ALL_FEATURES)


def _track(db, sid, low, high=1.2, done=1, samples=None, ts=None, v=None):
    db.execute("INSERT INTO decision_tracking(snapshot_id, mint, action, score, decided_at, entry_price, entry_liq, "
               "max_price, min_price, last_price, min_liq, samples, missing, done, brain_vector) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (sid, f"m{sid}", "REJECT", 50, ts or time.time() - 3 * 86400, 1.0, 1000, high, low, low, 900,
                B.TRACK_MIN_SAMPLES if samples is None else samples, 0, done,
                json.dumps(v if v is not None else {"score": 40.0 + sid, "log_mcap": 4.2})))


@pytest.fixture
def ldb(db):
    L.init_schema(db)
    B.init_schema(db)
    return db


def test_harvest_labels_examples_once_and_both_brains_read_them(ldb):
    _track(ldb, 1, low=0.1)                       # rug
    _track(ldb, 2, low=0.9, high=2.5)             # 2x, order unknown from a high/low
    _track(ldb, 3, low=0.8)                       # plain
    _track(ldb, 4, low=0.8, done=0, samples=3)    # not finished: not an example yet
    ldb.execute("INSERT INTO shadow_calls(mint, ts, features, evaluated, path, vector) VALUES (?,?,?,?,?,?)",
                ("p1", time.time() - 86400, "{}", 1, json.dumps([[m, 1.0 + m / 60, 0.95, 1.0 + m / 60] for m in range(0, 180)]),
                 json.dumps({"score": 70.0})))
    got = TS.harvest(ldb)
    assert got == {"paths": 1, "tracking": 3}
    assert TS.harvest(ldb) == {"paths": 0, "tracking": 0}
    rugs = TS.examples(ldb, "rug")
    assert sorted((r["id"], r["y"]) for r in rugs) == [(-3, 0), (-2, 0), (-1, 1), (1, 0)]
    run = {r["id"]: r["y"] for r in TS.examples(ldb, "runner")}
    assert run[1] == 1, "a clean 2x minute path is a runner"
    assert run[-1] == 0 and run[-3] == 0
    assert {r["id"]: r["v"]["score"] for r in rugs}[-3] == pytest.approx(43.0)
    # the brains train from the store
    assert len(B.training_rows(ldb, 21, time.time())) == len(run)
    from northstar import rugmodel as RM
    assert len(RM.training_rows(ldb, 21, time.time())) == 4


def test_compaction_keeps_rugs_and_runners_and_shrinks_the_raw_tables(ldb):
    for i in range(1, 41):
        _track(ldb, i, low=0.1 if i % 10 == 0 else 0.8, high=2.5 if i % 7 == 0 else 1.2,
               ts=time.time() - (60 - i) * 86400)
    TS.harvest(ldb)
    s = Settings(_env_file=None, training_max_examples=10, tracking_retention_days=21)
    out = TS.compact(ldb, s)
    st = TS.stats(ldb)
    assert st["examples"] == 10 and out["plain_thinned"] == 30
    assert st["rugs"] == 4, "every rug survives"
    left = ldb.fetchall("SELECT rid, rug, runner FROM training_examples")
    assert all(r["rug"] == 1 or r["runner"] is None or r["runner"] == 1 or r["rid"] for r in left)
    # raw tracking: harvested numbers cleared, old finished rows deleted, called coins kept
    assert out["tracking_deleted"] > 0
    assert ldb.fetchone("SELECT COUNT(*) n FROM decision_tracking WHERE decided_at < ?",
                        (time.time() - 22 * 86400,))["n"] == 0


def test_a_new_brain_signal_does_not_invalidate_old_examples(ldb, monkeypatch):
    _track(ldb, 1, low=0.1, v={"score": 55.0})
    TS.harvest(ldb)
    monkeypatch.setattr(B, "ALL_FEATURES", list(B.ALL_FEATURES) + ["brand_new_signal"])
    _track(ldb, 2, low=0.9, v={"score": 60.0, "brand_new_signal": 3.0})
    TS.harvest(ldb)
    ex = {r["id"]: r["v"] for r in TS.examples(ldb, "rug")}
    assert ex[-1]["score"] == 55.0 and "brand_new_signal" not in ex[-1]
    assert ex[-2]["brand_new_signal"] == 3.0


def test_runner_brain_needs_to_rank_near_call_coins_too(monkeypatch):
    """Tracked coins easy to separate, near-call (path) coins pure noise: not trusted."""
    import random
    rnd = random.Random(3)
    rows = []
    for i in range(1200):
        src = "path" if i % 3 == 0 else "tracking"
        y = int(rnd.random() < 0.2)
        x = rnd.random() if src == "path" else (1.0 if y else 0.0) + rnd.random() * 0.1
        rows.append({"id": i + 1 if src == "path" else -(i + 1), "ts": i, "v": {"score": x}, "y": y, "src": src})

    class DB:
        def fetchall(self, *a, **k):
            return []

        def fetchone(self, *a, **k):
            return None

        def execute(self, *a, **k):
            return 0

    monkeypatch.setattr(B, "training_rows", lambda *a, **k: rows)
    monkeypatch.setattr(B, "active_features", lambda db: ["score"])
    s = Settings(_env_file=None, brain_feature_search=False, brain_min_samples=100)
    m = B.train(DB(), s, 10_000)
    assert m.auc > 0.7 and m.path_auc < 0.58 and not m.trusted


# ------------------------------------------------------------------ untrack
def test_untrack_stops_everything_and_nothing_brings_it_back(db):
    db.upsert_discovery(MINT, "radar_surge", 9.0, "Trump $1", "1", {"x": 1})
    db.set_state(MINT, "WATCHLIST", next_research_at=time.time())
    aid = db.execute("INSERT INTO alerts(mint, status, state, created_at) VALUES (?,?,?,?)",
                     (MINT, "SENT", "OPEN", time.time() - 600))
    db.create_or_update_early_signal(MINT, 100.0, 80.0, {"mint": MINT})
    db.update_early_signal_status(MINT, "SENT", 1, 2)
    res = db.untrack(MINT, "obvious rug, fresh connected holders", "test")
    assert res["candidate"] and res["calls_closed"] == 1 and res["early_watches_ended"] == 1
    cand = db.get_candidate(MINT)
    assert cand["state"] == "IGNORED" and cand["next_research_at"] is None
    assert json.loads(cand["reasons"])[0].startswith("untracked by test: obvious rug")
    assert db.get_alert(aid)["state"] == "CLOSED"
    assert db.due_candidates(10, time.time() + 10) == []
    # new feed events, a surge, a migration: still ignored
    db.upsert_discovery(MINT, "curve_radar", 9.0, "Trump $1", "1", {"market_cap": 99_000})
    db.upsert_discovery(MINT, "pumpportal_migration", 3.0, "Trump $1", "1", {"pool": "x"})
    assert db.get_candidate(MINT)["state"] == "IGNORED"
    assert db.retrack(MINT) and db.get_candidate(MINT)["state"] == "DISCOVERED"
    assert not db.retrack(MINT)


async def test_the_engine_skips_an_untracked_coin(settings, db):
    from conftest import MemoryPublisher, seed_profile, strong_providers
    from northstar.engine import Engine

    p = strong_providers()
    seed_profile(db)
    eng = Engine(settings, db, p, MemoryPublisher())
    res = await eng.untrack(MINT, "rug", "test")
    assert res["ok"]
    before = db.fetchone("SELECT COUNT(*) n FROM research_snapshots")["n"]
    await eng.process(MINT)
    assert db.fetchone("SELECT COUNT(*) n FROM research_snapshots")["n"] == before
    assert (await eng.untrack("not-a-mint"))["ok"] is False


def test_dashboard_untrack_without_the_bot(db):
    from test_dashboard import serve
    db.upsert_discovery(MINT, "radar_surge", 9.0, "Mock", "MOCK", {"x": 1})
    srv, c = serve(db, "pw")
    assert c.req("/api/login", {"password": "pw"})[0] == 200
    st, body = c.req("/api/untrack", {"mint": MINT, "reason": "rug"})
    assert st == 200 and json.loads(body)["candidate"] is True
    assert json.loads(c.req(f"/api/token?mint={MINT}")[1])["state"] == "IGNORED"
    st, body = c.req("/api/retrack", {"mint": MINT})
    assert st == 200 and json.loads(body)["ok"]
    assert c.req("/api/untrack", {"mint": "bad"})[0] == 422
    srv.server.shutdown()


# ------------------------------------------------------------------ wallet network
def cr(**kw):
    base = dict(status=DataStatus.INFERRED, wallets_aged=12, fresh_wallet_share=20.0, fresh_supply_pct=2.0,
                max_cluster_supply_pct=1.0, clusters=[])
    base.update(kw)
    return ClusterResult(**base)


S = Settings(_env_file=None)


@pytest.mark.parametrize("kw, words", [
    ({"fresh_supply_pct": 22.0}, "brand-new wallets"),
    ({"fresh_wallet_share": 92.0}, "traced holder/buyer wallets are brand new"),
    ({"max_cluster_supply_pct": 11.0, "clusters": [{"size": 6, "held_pct_of_supply_top20_only": 11.0}]},
     "6 wallets linked by shared funding hold 11.0%"),
])
def test_fresh_or_connected_holders_fail(kw, words):
    g = wallet_network_gate(cr(**kw), S)
    assert g.status == GateStatus.FAIL and words in g.reason


def test_a_healthy_holder_base_passes_and_untraced_is_unknown():
    g = wallet_network_gate(cr(two_hop_links=1), S)
    assert g.status == GateStatus.PASS and "12 wallets traced" in g.reason and "2-hop" in g.reason
    assert wallet_network_gate(cr(status=DataStatus.UNAVAILABLE, wallets_aged=0), S).status == GateStatus.UNKNOWN
    assert wallet_network_gate(cr(fresh_wallet_share=95.0, wallets_aged=3), S).status == GateStatus.PASS, \
        "3 wallets are too few to call the holder base brand new"


def test_wallet_network_rejects_and_the_rug_brain_cannot_overrule_it():
    from test_unified_engine import gates, report
    s = Settings(_env_file=None, min_liquidity_usd=15_000, min_narrative_evidence_items=0)
    r = report(gates=gates(wallet_network="FAIL"))
    r["rug_model"] = {"p": 0.02, "trusted": True}
    u = U.evaluate(r, s)
    assert u["decision"] == U.REJECT and u["reason_class"] == "RISK_THRESHOLD"
