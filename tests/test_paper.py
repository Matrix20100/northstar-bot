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


# ----------------------------------------------------------------------------- v3.2.0 brain account
import asyncio  # noqa: E402
import types  # noqa: E402

from conftest import make_pair  # noqa: E402


def researched(mint, p=0.2, rug=0.1, mc=500_000.0):
    report = {"brain": {"p": p}, "unified": {"version": 1, "rug_odds": rug}}
    return types.SimpleNamespace(mint=mint, report=report,
                                 pair=make_pair(base_mint=mint, base_symbol="BRN", market_cap=mc, fdv=mc))


class Dex:
    def __init__(self):
        self.caps = {}

    async def tokens_batch(self, mints):
        return [make_pair(base_mint=m, market_cap=self.caps[m], fdv=self.caps[m]) for m in mints if m in self.caps]


def test_brain_buys_only_its_confident_safe_picks(db):
    s = Settings(_env_file=None, paper_brain_max_open=2)
    bt = PT.BrainTrader(s, db, Dex())
    assert bt.consider(researched(addr("b1"))) == "bought"
    assert bt.consider(researched(addr("b1"))) == "already traded this run"
    assert "runner odds" in bt.consider(researched(addr("low"), p=0.05))
    assert "rug odds" in bt.consider(researched(addr("rug"), rug=0.5))
    assert bt.consider(types.SimpleNamespace(mint=addr("x"), report={}, pair=None)) is None
    assert bt.consider(researched(addr("b2"))) == "bought"
    assert "already open" in bt.consider(researched(addr("b3")))
    t = trade(db, addr("b1"))
    assert t["kind"] == "brain" and t["entry_mcap"] == 500_000 and t["note"] == "runner odds 20% · rug odds 10%"
    # a separate account: the calls account is untouched
    assert PT.summary(db, S, account="calls")["trades"] == []
    assert PT.summary(db, s, account="brain")["open"] == 2


def test_brain_positions_follow_the_standard_plan_on_live_prices(db):
    s = Settings(_env_file=None)
    plan = TP.build_plan(500_000.0, s, "standard")
    dex = Dex()
    bt = PT.BrainTrader(s, db, dex)
    t0 = time.time()
    m = addr("run")
    bt.consider(researched(m), now=t0)
    dex.caps[m] = 500_000 * plan.tp1
    asyncio.run(bt.run_once(t0 + 60))
    t = trade(db, m)
    assert json.loads(t["done"]) == ["TP1"]
    assert t["proceeds_usd"] == pytest.approx(t["units"] * plan.tp1_sell_pct / 100 * 500_000 * plan.tp1 * 0.99)
    dex.caps[m] = 500_000 * plan.stop * 0.9                 # crashes: the plan exits the rest
    assert asyncio.run(bt.run_once(t0 + 120))["closed"] == 1
    assert trade(db, m)["status"] == "CLOSED"


def test_brain_position_closes_after_the_hold_time_even_without_a_price(db):
    s = Settings(_env_file=None, paper_brain_hold_hours=1)
    bt = PT.BrainTrader(s, db, Dex())
    t0 = time.time()
    bt.consider(researched(addr("hold")), now=t0)
    asyncio.run(bt.run_once(t0 + 7200))
    t = trade(db, addr("hold"))
    assert t["status"] == "CLOSED" and t["exit_reason"] == "HOLD"
    assert t["proceeds_usd"] == pytest.approx(t["cost_usd"] * 0.99 * 0.99)     # last price = entry


def test_brain_off(db):
    s = Settings(_env_file=None, paper_brain_enabled=False)
    assert PT.BrainTrader(s, db).consider(researched(addr("off"))) is None


def test_dashboard_switches_accounts(db):
    from test_dashboard import serve
    PT.BrainTrader(Settings(_env_file=None), db).consider(researched(addr("dashb")))
    srv, c = serve(db, "pw")
    c.req("/api/login", {"password": "pw"})
    d = json.loads(c.req("/api/paper?account=brain")[1])
    assert d["ready"] and d["account"] == "brain" and d["trades"][0]["kind"] == "brain"
    assert d["brain"]["min_p"] == 0.15
    st, body = c.req("/api/paper/start", {"account": "brain", "start_usd": 300, "position_pct": 5, "fee_pct": 1})
    assert st == 200 and json.loads(body)["run"]["account"] == "brain"
    assert json.loads(c.req("/api/paper?account=brain")[1])["equity"] == 300
    assert json.loads(c.req("/api/paper?account=calls")[1]).get("run", {}).get("start_usd") != 300
    srv.server.shutdown()
