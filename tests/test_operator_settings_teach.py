"""v4.1.0: every setting editable from the dashboard; teaching the learners with real coins. SYNTHETIC."""
from __future__ import annotations

import json
import time

import pytest

from conftest import MINT, addr
from northstar import operator_settings as OS
from northstar import teach as TE
from northstar import training_store as TS
from northstar.config import Settings


# ----------------------------------------------------------------------------- settings
def test_settings_are_validated_stored_and_applied(db):
    s = Settings(_env_file=None)
    assert OS.set_value(db, s, "runner_min_score", "52") == 52.0 and s.runner_min_score == 52.0
    assert OS.set_value(db, s, "paper_enabled", "false") is False and s.paper_enabled is False
    with pytest.raises(ValueError):
        OS.set_value(db, s, "runner_min_score", "not a number")
    with pytest.raises(ValueError):
        OS.set_value(db, s, "no_such_setting", "1")
    fresh = Settings(_env_file=None)                       # a restart: the stored values come back
    applied = OS.apply(db, fresh)
    assert applied == {"runner_min_score": 52.0, "paper_enabled": False} and fresh.runner_min_score == 52.0
    assert OS.reset(db, fresh, "runner_min_score") == Settings().runner_min_score
    assert "runner_min_score" not in OS.keys(db)


def test_secrets_and_locations_are_never_editable(db):
    s = Settings(_env_file=None)
    for key in ("discord_token", "helius_api_key", "pumpportal_api_key", "dashboard_password", "data_dir",
                "dashboard_port", "tracked_wallets_file"):
        if key in type(s).model_fields:
            assert not OS.editable(s, key)
            with pytest.raises(ValueError):
                OS.set_value(db, s, key, "x")
    assert OS.editable(s, "runner_min_score") and OS.needs_restart("curve_radar_seconds")
    assert not OS.needs_restart("runner_min_score")


def test_learners_leave_a_dashboard_value_alone(db):
    from northstar import curve_learn as CL
    s = Settings(_env_file=None, curve_learn_min_samples=1)
    OS.set_value(db, s, "runner_min_score", 50)
    assert CL.learn(db, s) is None and CL.apply_saved(db, s) is None and s.runner_min_score == 50


def test_dashboard_edits_a_setting_live_and_resets_it(db):
    from test_dashboard import serve
    srv, c = serve(db, "pw")
    c.req("/api/login", {"password": "pw"})
    st, body = c.req("/api/settings/set", {"key": "runner_min_score", "value": "48"})
    assert st == 200 and json.loads(body)["value"] == 48.0 and json.loads(body)["restart"] is False
    st, body = c.req("/api/settings/set", {"key": "discord_token", "value": "x"})
    assert st == 422
    rows = {r["name"]: r for g in json.loads(c.req("/api/settings")[1])["groups"] for r in g["settings"]}
    assert rows["runner_min_score"]["source"] == "dashboard" and rows["runner_min_score"]["editable"]
    assert not rows["discord_token"]["editable"]
    assert c.req("/api/settings/reset", {"key": "runner_min_score"})[0] == 200
    srv.server.shutdown()


# ----------------------------------------------------------------------------- teaching
def research(db, mint):
    report = {"mint": mint, "market": {"liquidity_usd": {"value": 50_000}, "market_cap": {"value": 400_000}},
              "holders": {"top10_adj": 22.0}, "lifecycle": "POOL"}
    db.execute("INSERT INTO research_snapshots(mint, ts, action, score, risk, report) VALUES (?,?,?,?,?,?)",
               (mint, time.time(), "REJECT", 40, "HIGH", json.dumps(report)))


def test_a_lesson_becomes_a_training_example_and_overrides_measured_labels(db):
    research(db, MINT)
    res = TE.teach(db, MINT, "runner", note="ran to 10x")
    assert res == {"mint": MINT, "label": "runner", "example": True}
    ex = db.fetchone("SELECT * FROM training_examples WHERE key = ?", (f"teach:{MINT}",))
    assert ex["runner"] == 1 and ex["rug"] == 0 and ex["src"] == "taught"
    # a measured example of the same coin said it rugged: the lesson wins in what the brains read
    names = list(__import__("northstar.brain", fromlist=["x"]).ALL_FEATURES)
    db.execute("INSERT INTO training_examples(key, rid, mint, ts, src, schema, feats, peak_x, low_x, rug, runner) "
               "VALUES ('path:1', 1, ?, ?, 'path', ?, ?, 1, 0.2, 1, 0)",
               (MINT, time.time() - 10, TS._schema_id(db, names), TS.pack({}, names)))
    runners = [e for e in TS.examples(db, "runner") if e["id"] == 1]
    rugs = [e for e in TS.examples(db, "rug") if e["id"] == 1]
    assert runners[0]["y"] == 1 and rugs[0]["y"] == 0
    assert TE.stats(db)["runner"] == 1
    TE.teach(db, MINT, "clear")
    assert TE.labels(db) == {} and db.fetchone("SELECT 1 AS x FROM training_examples WHERE key=?",
                                               (f"teach:{MINT}",)) is None
    with pytest.raises(ValueError):
        TE.teach(db, MINT, "moon")


def test_a_coin_without_research_keeps_the_label_only(db):
    res = TE.teach(db, addr("unseen"), "rug")
    assert res["example"] is False and TE.labels(db)[addr("unseen")] == "rug"


def test_cleanup_never_removes_a_lesson(db):
    from northstar import cleanup as CU
    research(db, MINT)
    TE.teach(db, MINT, "rug")
    db.execute("UPDATE training_examples SET ts = ? WHERE key = ?", (time.time() - 30 * 86400, f"teach:{MINT}"))
    CU.run(db)
    assert db.fetchone("SELECT 1 AS x FROM training_examples WHERE key=?", (f"teach:{MINT}",))


def test_dashboard_teach_buttons(db):
    from test_dashboard import serve
    research(db, MINT)
    db.upsert_discovery(MINT, "x", 1.0)
    srv, c = serve(db, "pw")
    c.req("/api/login", {"password": "pw"})
    st, body = c.req("/api/teach", {"mint": MINT, "label": "rug"})
    assert st == 200 and json.loads(body)["example"] is True
    assert json.loads(c.req(f"/api/token?mint={MINT}")[1])["taught"] == "rug"
    assert c.req("/api/teach", {"mint": MINT, "label": "moon"})[0] == 422
    srv.server.shutdown()


async def test_engine_applies_dashboard_values_over_learned_ones(settings, db):
    from conftest import MemoryPublisher, strong_providers
    from northstar import learning as L
    from northstar.engine import Engine
    L.init_schema(db)
    db.execute("INSERT INTO learned_settings(key, value, updated_at) VALUES ('min_opportunity_score', 70, ?)",
               (time.time(),))
    OS.set_value(db, Settings(_env_file=None), "min_opportunity_score", 42)
    eng = Engine(settings, db, strong_providers(), MemoryPublisher())
    assert settings.min_opportunity_score == 42
    await eng._learning_sync()                      # the learned 70 never overwrites the operator's 42
    assert settings.min_opportunity_score == 42
