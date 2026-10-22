"""v3.1.0 paper trading: a simulated account follows every call's plan. SYNTHETIC data only."""
from __future__ import annotations

import json
import time

import pytest

from conftest import addr
from northstar import early_calls as EC
from northstar import paper as PT
from northstar import trade_plan as TP
from northstar.config import Settings

S = Settings(_env_file=None, paper_start_usd=1000, paper_position_pct=10, paper_fee_pct=1)


def early_call(db, mint, created, entry=40_000.0, runner=True):
    EC.init_schema(db)
    plan = TP.build_plan(entry, S, "early").__dict__
    report = {"mint": mint, "identity": {"symbol": "KIM"}, **({"runner": {"score": 70}} if runner else {})}
    db.execute("INSERT INTO early_calls(mint, created_at, status, entry_mcap, current_mcap, peak_mcap, plan, report) "
               "VALUES (?,?,?,?,?,?,?,?)", (mint, created, "SENT", entry, entry, entry, json.dumps(plan),
                                             json.dumps(report)))


def move_early(db, mint, mcap, events=(), status="SENT"):
    db.execute("UPDATE early_calls SET current_mcap=?, plan_sent=?, status=? WHERE mint=?",
               (mcap, json.dumps(sorted(events)), status, mint))


def alert(db, mint, created, entry=200_000.0, state="SENT"):
    db.execute("INSERT INTO alerts(mint, status, state, created_at, symbol, entry_mcap, current_mcap) "
               "VALUES (?,?,?,?,?,?,?)", (mint, "SENT", state, created, "BIG", entry, entry))
    return db.fetchone("SELECT id FROM alerts ORDER BY id DESC LIMIT 1")["id"]


def trade(db, mint):
    return db.fetchone("SELECT * FROM paper_trades WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))


def test_a_call_is_bought_then_follows_the_plan_exactly(db):
    t0 = time.time()
    run = PT.active_run(db, S)
    assert (run["start_usd"], run["position_pct"], run["fee_pct"]) == (1000, 10, 1)
    m = addr("kim")
    early_call(db, m, t0 + 1)
    tr = PT.PaperTrader(S, db)
    assert tr.run_once(t0 + 2)["opened"] == 1
    t = trade(db, m)
    assert t["cost_usd"] == pytest.approx(100) and t["units"] == pytest.approx(99 / 40_000)
    assert PT.cash(db, run) == pytest.approx(900)
    # TP1 at 2x: sell the plan's 40%
    move_early(db, m, 80_000, ["TP1"])
    tr.run_once(t0 + 60)
    t = trade(db, m)
    assert t["proceeds_usd"] == pytest.approx(99 / 40_000 * 0.4 * 80_000 * 0.99)
    assert t["status"] == "OPEN" and json.loads(t["done"]) == ["TP1"]
    tr.run_once(t0 + 70)                                    # the same event is never sold twice
    assert trade(db, m)["proceeds_usd"] == pytest.approx(t["proceeds_usd"])
    # trailing stop at 1.5x: sell the rest
    move_early(db, m, 60_000, ["TP1", "TRAIL"])
    assert tr.run_once(t0 + 120)["closed"] == 1
    t = trade(db, m)
    total = 99 / 40_000 * (0.4 * 80_000 + 0.6 * 60_000) * 0.99
    assert t["status"] == "CLOSED" and t["exit_reason"] == "TRAIL" and t["units_left"] == 0
    assert t["proceeds_usd"] == pytest.approx(total)
    d = PT.summary(db, S)
    assert d["closed"] == 1 and d["wins"] == 1 and d["equity"] == pytest.approx(900 + total, abs=0.01)
    assert d["pnl"] == pytest.approx(total - 100, abs=0.01)
    assert [f["kind"] for f in d["trades"][0]["fills"]] == ["BUY", "TP1", "TRAIL"]


def test_stop_loss_and_tracking_end_sell_everything(db):
    t0 = time.time()
    PT.active_run(db, S)
    a, b = addr("stop"), addr("end")
    early_call(db, a, t0 + 1)
    aid = alert(db, b, t0 + 1.5)
    tr = PT.PaperTrader(S, db)
    tr.run_once(t0 + 2)
    assert trade(db, b)["cost_usd"] == pytest.approx(90)     # 10% of the cash left after the first buy
    move_early(db, a, 22_000, ["STOP"])
    db.execute("UPDATE alerts SET current_mcap=100000, state='CLOSED' WHERE id=?", (aid,))
    tr.run_once(t0 + 60)
    ta, tb = trade(db, a), trade(db, b)
    assert ta["exit_reason"] == "STOP" and ta["proceeds_usd"] == pytest.approx(99 / 40_000 * 22_000 * 0.99)
    assert tb["exit_reason"] == "END" and tb["proceeds_usd"] == pytest.approx(90 * 0.99 * 0.5 * 0.99)
    d = PT.summary(db, S)
    assert d["closed"] == 2 and d["wins"] == 0 and d["open"] == 0
    assert sorted(t["exit"] for t in d["trades"]) == ["stop loss", "tracking ended"]


def test_old_calls_ignored_new_run_resets_and_history_is_kept(db):
    t0 = time.time()
    early_call(db, addr("before"), t0 - 600)                # posted before the run started
    PT.active_run(db, S)
    tr = PT.PaperTrader(S, db)
    assert tr.run_once(t0 + 1)["opened"] == 0
    early_call(db, addr("after"), t0 + 5)
    tr.run_once(t0 + 6)
    run2 = PT.start_run(db, 250, 50, 0, now=t0 + 10)
    assert run2["start_usd"] == 250 and PT.cash(db, run2) == 250
    d = PT.summary(db, S)
    assert d["run"]["id"] == run2["id"] and d["trades"] == []
    assert d["past_runs"][0]["trades"] == 1 and d["past_runs"][0]["end_cash"] == pytest.approx(900)
    # only calls after the new run started are bought, with the new size and no fee
    early_call(db, addr("third"), t0 + 20)
    tr.run_once(t0 + 21)
    t = trade(db, addr("third"))
    assert t["cost_usd"] == pytest.approx(125) and t["units"] == pytest.approx(125 / 40_000)


def test_bad_values_are_clamped_and_skips_are_recorded(db):
    run = PT.start_run(db, "abc", 500, -3)
    assert (run["start_usd"], run["position_pct"], run["fee_pct"]) == (1000, 100, 0)
    alert(db, addr("noentry"), time.time() + 1, entry=None)
    c = PT.PaperTrader(S, db).run_once(time.time() + 2)
    assert c["skipped"] == 1 and trade(db, addr("noentry"))["status"] == "SKIPPED"
    assert PT.summary(db, S)["skipped"] == 1


def test_disabled(db):
    assert PT.PaperTrader(Settings(_env_file=None, paper_enabled=False), db).run_once()["opened"] == 0


def test_dashboard_shows_and_restarts_the_account(db):
    from test_dashboard import serve
    PT.active_run(db, S)
    early_call(db, addr("dash"), time.time() + 1)
    PT.PaperTrader(S, db).run_once(time.time() + 2)
    srv, c = serve(db, "pw")
    assert c.req("/api/login", {"password": "pw"})[0] == 200
    st, body = c.req("/api/paper")
    d = json.loads(body)
    assert st == 200 and d["ready"] and d["open"] == 1 and d["trades"][0]["symbol"] == "KIM"
    st, body = c.req("/api/paper/start", {"start_usd": 5000, "position_pct": 20, "fee_pct": 0.5})
    assert st == 200 and json.loads(body)["run"]["start_usd"] == 5000
    assert c.req("/api/paper/start", {"start_usd": 0})[0] == 422
    d = json.loads(c.req("/api/paper")[1])
    assert d["run"]["start_usd"] == 5000 and d["equity"] == 5000 and d["past_runs"]
    srv.server.shutdown()


def test_dashboard_before_any_run_shows_the_defaults(db):
    from test_dashboard import serve
    srv, c = serve(db, "pw")
    c.req("/api/login", {"password": "pw"})
    d = json.loads(c.req("/api/paper")[1])
    assert d["ready"] is False and d["defaults"]["start_usd"] == 1000
    srv.server.shutdown()
