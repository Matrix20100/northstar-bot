"""v2.6.0: the learned rug model, the reworked score, and the Pulse view."""
from __future__ import annotations

import json
import random
import sqlite3
import time

import pytest

from northstar import brain as B
from northstar import rugmodel as RM
from northstar.analysis import unified as U
from northstar.config import Settings
from test_unified_engine import gates, report


@pytest.fixture
def s() -> Settings:
    return Settings(_env_file=None, discord_token="t", discord_channel_id=1, discord_vip_channel_id=2,
                    min_liquidity_usd=15_000, min_narrative_evidence_items=0,
                    min_volume_acceleration=0, min_buy_sell_ratio_h1=0)


def with_rug(r, p, trusted=True):
    r["rug_model"] = {"p": p, "trusted": trusted, "auc": 0.9, "recent_auc": 0.88, "n": 5000}
    return r


# ------------------------------------------------------------------ decisions
def test_low_rug_odds_make_a_clean_coin_a_call_with_a_safety_led_score(s):
    u = U.evaluate(with_rug(report(), 0.05), s)
    assert u["decision"] == U.STRONG
    assert u["rug_odds"] == 0.05
    assert u["score"] == pytest.approx(0.6 * 95 + 0.4 * u["opportunity_score"], abs=0.11)
    assert "rug odds **5%**" in U.panel_text(u)


def test_high_rug_odds_reject_even_a_clean_looking_coin(s):
    u = U.evaluate(with_rug(report(), 0.72), s)
    assert u["decision"] == U.REJECT and u["reason_class"] == U.RC_RUG
    assert u["why"][0].startswith("learned rug odds 72%")


def test_middling_rug_odds_cap_a_call_at_watch(s):
    u = U.evaluate(with_rug(report(), 0.55), s)
    assert u["decision"] in (U.WATCH, U.CAUTION) and u["decision"] != U.STRONG


def test_an_untrusted_model_changes_nothing(s):
    plain = U.evaluate(report(), s)
    u = U.evaluate(with_rug(report(), 0.9, trusted=False), s)
    assert (u["decision"], u["score"]) == (plain["decision"], plain["score"]) and u["rug_odds"] is None
    s.rug_model_enabled = False
    assert U.evaluate(with_rug(report(), 0.9), s)["decision"] == plain["decision"]


def test_high_top10_is_not_a_reject_when_the_model_says_low_rug_odds(s):
    """The Blacktober case: top-10 over the limit, otherwise sound."""
    r = report(gates=gates(holder_concentration="FAIL"))
    assert U.evaluate(r, s)["decision"] == U.REJECT                    # no model: the rule blocks
    u = U.evaluate(with_rug(report(gates=gates(holder_concentration="FAIL")), 0.12), s)
    assert u["decision"] != U.REJECT
    assert any("not blocking: learned rug odds 12%" in t for t in u["suspicions"])
    u = U.evaluate(with_rug(report(gates=gates(holder_concentration="FAIL")), 0.45), s)
    assert u["decision"] == U.REJECT, "higher odds: the threshold still blocks"


def test_traps_and_the_launch_filters_are_never_overruled(s):
    for g in ("mint_authority", "launch_buys", "sniper_holdings", "insider_micro_trades", "liquidity"):
        u = U.evaluate(with_rug(report(gates=gates(**{g: "FAIL"})), 0.01), s)
        assert u["decision"] == U.REJECT, g


def test_the_brain_and_ai_refolds_keep_the_rug_odds(s):
    r = with_rug(report(), 0.72)
    u = U.evaluate(r, s)
    again = U.apply_model(u, r, 0.5, True, baseline=0.1, auc=0.62, s=s)
    assert again["decision"] == U.REJECT and again["rug_odds"] == 0.72
    ai = U.apply_ai(again, r, {"verdict": "CALL", "score": 95}, s)
    assert ai["decision"] == U.REJECT


# ------------------------------------------------------------------ training
def synth_db(db, n=600, seed=7, first_id=1):
    """Coins whose rug outcome depends on liquidity vs market cap and trade count, like the real data."""
    from northstar import learning as L
    L.init_schema(db)
    B.init_schema(db)
    RM.init_schema(db)
    rnd = random.Random(seed)
    now = time.time()
    for i in range(n):
        liq = rnd.uniform(0.05, 0.6)
        tx = rnd.uniform(1, 4)
        p = 1 / (1 + 2.718 ** (-(-14 * liq + 3.0 * (tx - 2.5) + 2.0)))
        rug = rnd.random() < p
        low = rnd.uniform(0.05, 0.29) if rug else rnd.uniform(0.35, 1.0)
        v = {f: rnd.random() for f in B.FEATURES}
        v.update(liq_to_mcap=liq, log_txns_m5=tx)
        db.execute("INSERT INTO decision_tracking(snapshot_id, mint, action, score, decided_at, entry_price, "
                   "entry_liq, max_price, min_price, last_price, min_liq, samples, missing, done, brain_vector) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (first_id + i, f"m{first_id + i}", "REJECT", 50, now - (n - i) * 60, 1.0, 1000, 1.2, low, low, 900,
                    B.TRACK_MIN_SAMPLES, 0, 1, json.dumps(v)))
    return now


def test_training_learns_the_rug_pattern_and_earns_trust(db):
    now = synth_db(db)
    s = Settings(_env_file=None, rug_model_min_samples=300, rug_model_min_auc=0.7)
    m = RM.train(db, s, now + 1)
    assert m is not None and m.trusted and m.auc > 0.75
    assert m.coef["liq_to_mcap"] < 0 < m.coef["log_txns_m5"]
    latest = RM.latest(db)
    assert latest.trusted and latest.n == m.n
    safe = {f: 0.5 for f in B.FEATURES} | {"liq_to_mcap": 0.55, "log_txns_m5": 1.2}
    risky = {f: 0.5 for f in B.FEATURES} | {"liq_to_mcap": 0.06, "log_txns_m5": 3.9}
    assert latest.predict(safe) < 0.2 < 0.6 < latest.predict(risky)


def test_too_few_examples_or_a_weak_model_is_not_trusted(db):
    now = synth_db(db, n=80)
    assert RM.train(db, Settings(_env_file=None), now + 1) is None
    now = synth_db(db, n=600, seed=9, first_id=1000)
    m = RM.train(db, Settings(_env_file=None, rug_model_min_auc=0.99), now + 1)
    assert m is not None and not m.trusted


def test_an_incomplete_window_that_never_rugged_is_not_a_safe_example(db):
    from northstar import learning as L
    L.init_schema(db)
    B.init_schema(db)
    now = time.time()
    db.execute("INSERT INTO decision_tracking(snapshot_id, mint, action, score, decided_at, entry_price, entry_liq, "
               "max_price, min_price, last_price, min_liq, samples, missing, done, brain_vector) "
               "VALUES (1,'a','REJECT',50,?,1,1,1,0.8,0.8,1,3,0,0,?)", (now - 600, json.dumps({"score": 1})))
    db.execute("INSERT INTO decision_tracking(snapshot_id, mint, action, score, decided_at, entry_price, entry_liq, "
               "max_price, min_price, last_price, min_liq, samples, missing, done, brain_vector) "
               "VALUES (2,'b','REJECT',50,?,1,1,1,0.1,0.1,1,3,0,0,?)", (now - 600, json.dumps({"score": 1})))
    rows = RM.training_rows(db, 21, now)
    assert [(r["id"], r["y"]) for r in rows] == [(-2, 1)], "a rug is a rug at once; 'safe' needs the full window"


def test_scoring_skips_curve_coins_and_never_raises():
    m = B.Model(["score"], {"score": 0}, {"score": 1}, {"score": 1.0}, 0.0, n=10, trusted=True)
    assert RM.score(m, {"lifecycle": "PRE_MIGRATION_BONDING_CURVE"}) is None
    assert RM.score(None, {}) is None
    out = RM.score(m, {"score": {"total": 2.0}})
    assert out["trusted"] and 0 < out["p"] < 1


# ------------------------------------------------------------------ engine wiring
async def test_engine_scores_every_researched_coin_with_the_rug_model(settings, db):
    from conftest import MINT, MemoryPublisher, seed_profile, strong_providers
    from northstar.engine import Engine

    p = strong_providers()
    seed_profile(db)
    eng = Engine(settings, db, p, MemoryPublisher())
    eng.rug = B.Model(["score"], {"score": 0}, {"score": 1}, {"score": 0.0}, 3.0, n=500, trusted=True)  # ~95% rug
    await eng.process(MINT)
    snap = db.fetchone("SELECT report FROM research_snapshots WHERE mint=? ORDER BY id DESC LIMIT 1", (MINT,))
    rep = json.loads(snap["report"])
    assert rep["rug_model"]["p"] > 0.9
    assert rep["unified"]["decision"] == U.REJECT and rep["unified"]["rug_odds"] > 0.9
    assert db.get_candidate(MINT)["last_action"] == "REJECT"


# ------------------------------------------------------------------ pulse
def test_pulse_groups_by_stage_and_lists_coins_that_passed(db):
    from northstar.dashboard import view_pulse

    now = time.time()
    rows = [
        ("A" * 32, "WATCHLIST", {"identity": {"symbol": "GOOD"}, "market": {"market_cap": {"value": 150_000}},
                                "gates": [{"name": "liquidity", "status": "PASS"}],
                                "unified": {"decision": "WATCH", "score": 61, "rug_odds": 0.12}}),
        ("B" * 32, "REJECT", {"identity": {"symbol": "TOPX"}, "market": {"market_cap": {"value": 90_000}},
                             "gates": [{"name": "holder_concentration", "status": "FAIL", "reason": "top-10 41%"}],
                             "unified": {"decision": "REJECT", "rug_odds": 0.7}}),
        ("C" * 32, "REJECT", {"lifecycle": "PRE_MIGRATION_BONDING_CURVE", "identity": {"symbol": "CURVE"},
                             "pre_migration": {"market_cap_usd_current": 12_000},
                             "reasons": ["market cap already +700% since launch (limit 400%): entry would be late"]}),
        ("D" * 32, "INSUFFICIENT_DATA", {"lifecycle": "PRE_MIGRATION_BONDING_CURVE", "identity": {"symbol": "LATE"},
                                        "pre_migration": {"market_cap_usd_current": 55_000}}),
        ("E" * 32, "REJECT", {"identity": {"symbol": "DUST"}, "reasons": ["prefilter: liquidity $9 below prefilter $5,000"]}),
    ]
    for mint, action, rep in rows:
        db.execute("INSERT INTO research_snapshots(mint, ts, action, score, report) VALUES (?,?,?,?,?)",
                   (mint, now - 60, action, 50, json.dumps(rep)))
    c = sqlite3.connect(str(db.path))
    c.row_factory = sqlite3.Row
    d = view_pulse(c, now)
    cols = {k: [x["symbol"] for x in v] for k, v in d["columns"].items()}
    assert cols == {"new": ["CURVE"], "final": ["LATE"], "migrated": ["GOOD", "TOPX"]}
    assert [x["symbol"] for x in d["passed"]] == ["GOOD"]
    assert d["funnel"]["prefiltered"] == 1 and d["funnel"]["researched"] == 5
    topx = next(x for x in d["columns"]["migrated"] if x["symbol"] == "TOPX")
    assert [f["name"] for f in topx["failed"]] == ["holder concentration", "learned rug odds"]
    curve = d["columns"]["new"][0]
    assert curve["failed"][0]["name"] == "already pumped"
