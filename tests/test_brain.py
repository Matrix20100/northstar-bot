"""The learning brain: features, runner labels, walk-forward training, trust, veto/promote, embeds.
SYNTHETIC data only."""
from __future__ import annotations

import json
import random
import time

from conftest import MINT, addr, make_pair
from northstar import brain as B
from northstar import learning as L
from northstar.config import Settings
from northstar.discord_bot.embeds import callout_embed
from northstar.models import Action
from northstar.research import ResearchOutcome


def report(vol_m5=5000.0, liq=40_000.0, mcap=200_000.0, chg_h1=20.0, score=50.0, **extra):
    r = {"mint": MINT, "market": {"market_cap": {"value": mcap}, "liquidity_usd": {"value": liq},
                                  "volume": {"m5": vol_m5, "h1": vol_m5 * 6, "h6": vol_m5 * 20},
                                  "txns": {"m5": {"buys": 30, "sells": 10}, "h1": {"buys": 200, "sells": 120}},
                                  "price_change": {"m5": 5.0, "h1": chg_h1, "h6": 40.0}},
         "score": {"total": score, "components": [{"name": "demand", "points": 10, "max_points": 20}]},
         "gates": [{"name": "lp_lock", "status": "PASS"}], "risk": "MODERATE", "confidence": "MEDIUM"}
    r.update(extra)
    return r


def runner_path():
    return [[0, 1.0, 0.95, 1.0], [5, 1.6, 1.2, 1.5], [10, 2.3, 1.8, 2.1], [20, 2.0, 1.5, 1.6]]


def dud_path():
    return [[0, 1.0, 0.9, 0.95], [5, 1.05, 0.55, 0.6]]


def seed(db, n=240, now=None, seed_=7):
    """High 5-min volume coins run; low ones dump. Noise so it isn't trivially perfect."""
    L.init_schema(db)
    now = now or time.time()
    rnd = random.Random(seed_)
    for i in range(n):
        hot = rnd.random() < 0.3
        vol = rnd.uniform(15_000, 40_000) if hot else rnd.uniform(500, 6_000)
        is_runner = hot if rnd.random() > 0.1 else not hot
        rep = report(vol_m5=vol, chg_h1=rnd.uniform(-20, 60))
        db.execute("INSERT INTO shadow_calls(mint, ts, pool, entry_price, entry_mcap, liquidity, action, called, features, "
                   "evaluated, path, vector) VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
                   (addr(f"m{i}"), now - 5 * 86400 + i * 600, addr(f"p{i}"), 1.0, 200_000, 40_000, "WATCHLIST", 0,
                    "{}", json.dumps(runner_path() if is_runner else dud_path()), json.dumps(B.vector(rep))))


def test_vector_has_every_feature_and_reads_reports():
    v = B.vector(report())
    assert set(v) == set(B.FEATURES)
    assert abs(v["log_vol_m5"] - 3.69897) < 1e-4 and abs(v["liq_to_mcap"] - 0.2) < 1e-9
    assert v["c_demand"] == 0.5 and v["surge"] == 0.0
    assert B.vector({})["log_mcap"] is None                     # unknown stays unknown


def test_runner_label_needs_2x_before_the_drop():
    assert B.outcome(runner_path()) == 1
    assert B.outcome(dud_path()) == 0
    assert B.outcome([[0, 2.1, 0.5, 1.0]]) == 0                 # both in the same minute: counted as a loss
    assert B.outcome([[0, 1.9, 0.9, 1.0]]) == 0                 # never got there
    assert B.outcome([]) is None


def test_brain_learns_signal_and_earns_trust(db):
    seed(db)
    s = Settings(_env_file=None, brain_min_samples=150, brain_min_auc=0.58)
    m = B.train(db, s)
    assert m is not None and m.trusted and m.auc > 0.75 and m.recent_auc > 0.7
    assert m.importance(1)[0][0] == "5-min volume" and m.coef["log_vol_m5"] > 0
    hot, cold = B.vector(report(vol_m5=30_000)), B.vector(report(vol_m5=1_000))
    assert m.predict(hot) > 0.5 > m.predict(cold)
    assert "5-min volume" in m.reasons(hot)[0] and B.latest(db).trusted
    oof = db.fetchone("SELECT COUNT(*) n FROM shadow_calls WHERE brain_oof IS NOT NULL")["n"]
    assert oof > 100                                             # out-of-sample odds stored for the tuner


def test_noise_is_never_trusted(db):
    L.init_schema(db)
    rnd = random.Random(3)
    now = time.time()
    for i in range(240):
        db.execute("INSERT INTO shadow_calls(mint, ts, pool, entry_price, action, called, features, evaluated, path, vector) "
                   "VALUES (?,?,?,?,?,?,?,1,?,?)",
                   (addr(f"n{i}"), now - 5 * 86400 + i * 600, addr(f"q{i}"), 1.0, "REJECT", 0, "{}",
                    json.dumps(runner_path() if rnd.random() < 0.3 else dud_path()),
                    json.dumps(B.vector(report(vol_m5=rnd.uniform(500, 40_000))))))
    m = B.train(db, Settings(_env_file=None))
    assert m is not None and not m.trusted                      # random outcomes: no say in calls


def test_too_little_data_does_not_train(db):
    seed(db, n=30)
    assert B.train(db, Settings(_env_file=None)) is None
    assert "not trained yet" in B.describe(None)


def test_decision_tracking_is_a_second_source(db):
    L.init_schema(db)
    now = time.time()

    def snap(i, mint, ts, peak, done=1):
        db.execute("INSERT INTO research_snapshots(id, mint, ts, action, report) VALUES (?,?,?,?,?)",
                   (i, mint, ts, "REJECT", json.dumps(report())))
        db.execute("INSERT INTO decision_tracking(snapshot_id, mint, action, decided_at, entry_price, max_price, "
                   "min_price, samples, done) VALUES (?,?,?,?,?,?,?,?,?)", (i, mint, "REJECT", ts, 1.0, peak, 0.8, 10, done))
    snap(1, addr("a"), now - 30 * 3600, 3.0)                    # runner
    snap(2, addr("b"), now - 30 * 3600, 1.2)                    # finished, never ran
    snap(3, addr("c"), now - 2 * 3600, 1.1, done=0)             # too early to judge: skipped
    snap(4, addr("d"), now - 2 * 3600, 2.5, done=0)             # already a runner: counted
    snap(5, MINT, now - 30 * 3600, 5.0)                         # has a shadow path at the same time: skipped
    db.execute("INSERT INTO shadow_calls(mint, ts, pool, entry_price, action, called, features, evaluated, path, vector) "
               "VALUES (?,?,?,?,?,?,?,1,?,?)", (MINT, now - 30 * 3600 + 60, addr("p"), 1.0, "REJECT", 0, "{}",
                                               json.dumps(dud_path()), json.dumps(B.vector(report()))))
    rows = B.training_rows(db, 7, now)
    got = {(r["src"], r["y"]) for r in rows}
    assert len(rows) == 4 and ("path", 0) in got
    assert sorted(r["y"] for r in rows if r["src"] == "tracking") == [0, 1, 1]


class FakeModel:
    def __init__(self, p, trusted=True):
        self.p, self.trusted, self.auc, self.n, self.positives = p, trusted, 0.7, 300, 40

    def predict(self, v):
        return self.p

    def reasons(self, v):
        return ["5-min volume"], ["liquidity vs market cap"]


def engine(settings, db, model):
    from northstar.engine import Engine
    from northstar.research import Providers
    from conftest import MemoryPublisher
    eng = Engine(settings, db, Providers(), MemoryPublisher())
    eng.brain = model
    return eng


def outcome(action, blockers):
    rep = report()
    rep["decision"] = {"action": action.value, "blockers": blockers, "reasons": []}
    return ResearchOutcome(MINT, action, rep, pair=make_pair())


def test_trusted_brain_vetoes_a_low_odds_call(settings, db):
    settings.brain_min_probability = 0.08
    out = outcome(Action.AUTO_POST, [])
    p = engine(settings, db, FakeModel(0.03))._apply_brain(out)
    assert p == 0.03 and out.action == Action.WATCHLIST and out.report["brain"]["vetoed"]


def test_trusted_brain_promotes_only_soft_blocked_coins(settings, db):
    settings.brain_promote_probability = 0.25
    out = outcome(Action.WATCHLIST, ["score 38.0 < 40", "narrative score 0.0 below minimum"])
    engine(settings, db, FakeModel(0.4))._apply_brain(out)
    assert out.action == Action.AUTO_POST and out.report["brain"]["promoted"]
    assert out.reasons[0].startswith("🧠 promoted")
    hard = outcome(Action.WATCHLIST, ["score 38.0 < 40", "critical check unresolved: lp_lock — unknown"])
    engine(settings, db, FakeModel(0.9))._apply_brain(hard)
    assert hard.action == Action.WATCHLIST                       # never rescues a safety problem


def test_untrusted_brain_only_shows_odds(settings, db):
    settings.brain_min_probability = 0.5
    out = outcome(Action.AUTO_POST, [])
    engine(settings, db, FakeModel(0.01, trusted=False))._apply_brain(out)
    assert out.action == Action.AUTO_POST and out.report["brain"]["p"] == 0.01


def test_pre_migration_coins_are_not_scored(settings, db):
    out = outcome(Action.REJECT, [])
    out.report["lifecycle"] = "PRE_MIGRATION_BONDING_CURVE"
    assert engine(settings, db, FakeModel(0.9))._apply_brain(out) is None and "brain" not in out.report


def test_embed_shows_learned_odds():
    rep = report(identity={"symbol": "MOCK", "name": "Mock"}, pool={"address": addr("pool")})
    rep["brain"] = {"p": 0.23, "base": 0.13, "trusted": True, "auc": 0.67, "n": 731, "ups": ["5-min volume"],
                    "downs": ["market cap"], "win_x": 2.0, "loss_x": 0.6}
    e = callout_embed(rep)
    f = next(x for x in e["fields"] if x["name"] == "🧠 Learned runner odds")["value"]
    assert "**23%** chance to 2x before -40% (average coin: 13%)" in f and "▲ 5-min volume" in f and "0.67" in f
    rep["brain"]["trusted"] = False
    assert "Still learning" in next(x for x in callout_embed(rep)["fields"] if x["name"] == "🧠 Learned runner odds")["value"]


def test_tuner_trains_brain_and_only_tunes_its_bars_when_trusted(db):
    seed(db)
    s = Settings(_env_file=None, min_opportunity_score=40, min_confidence="MEDIUM", max_allowed_risk="MODERATE")
    L.tune(db, s, days=14, min_samples=20)
    assert B.latest(db) is not None
    assert db.fetchone("SELECT COUNT(*) n FROM brain_models")["n"] == 1


def test_tracking_numbers_survive_snapshot_pruning(db):
    L.init_schema(db)
    now = time.time()
    db.execute("INSERT INTO research_snapshots(id, mint, ts, action, report) VALUES (?,?,?,?,?)",
               (9, addr("z"), now - 10 * 86400, "REJECT", json.dumps(report(vol_m5=25_000))))
    db.execute("INSERT INTO decision_tracking(snapshot_id, mint, action, decided_at, entry_price, max_price, min_price, "
               "samples, done) VALUES (9,?,?,?,1.0,2.4,0.9,40,1)", (addr("z"), "REJECT", now - 10 * 86400))
    assert B.store_tracking_vectors(db) == 1
    db.execute("DELETE FROM research_snapshots WHERE id=9")          # weekly retention removed the report
    rows = B.training_rows(db, 21, now)
    assert len(rows) == 1 and rows[0]["y"] == 1 and rows[0]["v"]["log_vol_m5"] > 4.3
    assert B.store_tracking_vectors(db) == 0                          # not redone


def test_learning_tables_are_pruned(db):
    L.init_schema(db)
    now = time.time()
    for age_days in (2, 30, 60):
        db.execute("INSERT INTO shadow_calls(mint, ts, features) VALUES (?,?,'{}')", (addr("s"), now - age_days * 86400))
        db.execute("INSERT INTO radar_obs(mint, ts, metrics) VALUES (?,?,'{}')", (addr("r"), now - age_days * 86400))
    assert L.prune(db, now) == {"shadow_calls": 1, "radar_obs": 2}
