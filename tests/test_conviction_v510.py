"""v5.1.0: conviction-graded exits. Operator: "the bot was down on coins and sold, now it ran to 2x ... we need
to take educated risks." A strong setup gets a wider stop and holds a short dip while buyers are in control; a
weak one keeps a tight stop and a smaller size; the hard stop always applies. No network."""
from __future__ import annotations

import asyncio
import time

import pytest

from conftest import addr, make_pair
from northstar import conviction as CV
from northstar import exits as EX
from northstar import paper as PT
from northstar.config import Settings

S = Settings(_env_file=None)


def strong_report():
    return {"unified": {"rug_odds": 0.08}, "risk": "LOW_DETECTED", "runner": {"score": 84},
            "launch_buys": {"status": "OBSERVED", "bundle_pct": 3.0, "early_buy_pct": 12.0, "verdict": "ORGANIC"},
            "tracked_wallets": {"status": "OBSERVED", "holding_count": 3},
            "trades": {"buyer_trend": "rising"},
            "market": {"liquidity_usd": 30_000.0, "txns": {"m5": {"buys": 70, "sells": 30}}},
            "gates": [{"name": "mint_authority", "status": "PASS"}]}


def weak_report():
    # $STATION-like: bundled and sniped launch, middling rug odds
    return {"unified": {"rug_odds": 0.35}, "risk": "ELEVATED", "runner": {"score": 62},
            "launch_buys": {"status": "OBSERVED", "bundle_pct": 24.0, "early_buy_pct": 38.0, "verdict": "BUNDLED"},
            "trades": {"buyer_trend": "declining"},
            "market": {"liquidity_usd": 12_000.0, "txns": {"m5": {"buys": 20, "sells": 30}}},
            "gates": [{"name": "launch_buys", "status": "FAIL"}]}


def tick(x, t, entry=100_000.0, buys=60, sells=40, chg=-5.0, liq=30_000.0):
    return EX.Tick(mcap=entry * x, at=t, buys_m5=buys, sells_m5=sells, vol_m5=8_000.0, vol_h1=60_000.0,
                   chg_m5=chg, liquidity=liq)


def graded(report, entry=100_000.0):
    c = CV.assess(report, S)
    p = EX.Position(entry_mcap=entry, opened_at=0.0, peak_mcap=entry, entry_liq=CV.entry_liquidity(report))
    return EX.apply_conviction(p, c), c


# ----------------------------------------------------------------------------- grading
def test_strong_and_weak_setups_get_different_risk():
    a, c = CV.assess(strong_report(), S), CV.assess(weak_report(), S)
    assert a.grade == "A" and a.score >= 70 and a.grace
    assert c.grade == "C" and not c.grace
    assert a.stop_pct > S.exit_stop_pct > c.stop_pct >= S.exit_stop_min_pct
    assert a.stop_pct <= S.exit_stop_max_pct
    assert any("bundled" in w for w in c.why) and any("rug odds" in w for w in a.why)


def test_money_lost_at_the_stop_is_never_more_than_the_plain_stop():
    for r in (strong_report(), weak_report(), {}, None):
        c = CV.assess(r, S)
        assert 0 < c.size_mult <= 1.0
        assert c.size_mult * c.stop_pct <= S.exit_stop_pct + 1e-9


def test_unknown_is_not_good_and_an_untrusted_brain_has_no_say():
    c = CV.assess({}, S)
    assert c.score <= 45 and not c.grace and c.known == 0
    base = CV.assess(strong_report(), S)
    loud = CV.assess({**strong_report(), "brain": {"p": 0.9, "base": 0.05, "trusted": False}}, S)
    assert loud.score == base.score
    trusted = CV.assess({**weak_report(), "brain": {"p": 0.3, "base": 0.05, "trusted": True}}, S)
    assert trusted.score > CV.assess(weak_report(), S).score


def test_settings_bound_the_stop():
    s = Settings(_env_file=None, exit_stop_min_pct=10, exit_stop_max_pct=25)
    assert CV.assess(strong_report(), s).stop_pct <= 25 and CV.assess(weak_report(), s).stop_pct >= 10


# ----------------------------------------------------------------------------- the dip-hold
def test_a_strong_setup_holds_a_dip_and_rides_the_bounce_a_flat_stop_sold():
    # the operator's case: down 25%, buyers still in, then a 2x
    path = [tick(1.05, 30), tick(0.9, 60), tick(0.75, 90), tick(0.76, 120), tick(1.1, 150), tick(1.6, 200),
            tick(2.1, 260, buys=90, sells=50, chg=20)]
    flat = EX.replay(100_000.0, 0.0, path, S)
    assert flat["exit"] == "STOP" and flat["x"] < 0.8
    smart = EX.replay(100_000.0, 0.0, path, S, conv=CV.assess(strong_report(), S))
    assert smart["x"] > 1.4                                   # TP1 at 1.5x and the rest still riding


def test_the_dip_hold_runs_out_and_sells():
    p, _ = graded(strong_report())
    assert EX.decide(p, tick(0.62, 60), S) == [] and p.grace_since == 60 and "holding through a dip" in p.note
    assert EX.decide(p, tick(0.62, 100), S) == []
    a = EX.decide(p, tick(0.61, 60 + S.exit_grace_seconds + 1), S)
    assert a and a[0].kind == "STOP" and "no bounce" in a[0].reason


def test_no_dip_hold_when_sellers_dump_or_liquidity_leaves():
    for t in (tick(0.62, 60, buys=20, sells=60), tick(0.62, 60, chg=-30), tick(0.62, 60, liq=10_000.0)):
        p, _ = graded(strong_report())
        a = EX.decide(p, t, S)
        assert a and a[0].kind == "STOP" and "no dip-hold" in a[0].reason


def test_the_hard_stop_always_sells():
    p, _ = graded(strong_report())
    a = EX.decide(p, tick(0.5, 60), S)
    assert a and a[0].kind == "HARD_STOP"


def test_no_dip_hold_after_the_coin_already_ran():
    p, _ = graded(strong_report())
    EX.decide(p, tick(1.35, 60), S)                         # ran past the trailing-stop start
    a = EX.decide(p, tick(0.62, 120), S)
    assert a and a[0].kind in ("TRAIL", "STOP")


def test_a_weak_setup_is_cut_at_its_tight_stop():
    p, c = graded(weak_report())
    x = 1 - (c.stop_pct + 1) / 100
    assert c.stop_pct < S.exit_stop_pct
    a = EX.decide(p, tick(x, 60), S)
    assert a and a[0].kind == "STOP"


def test_conviction_off_is_the_v5_0_engine():
    s = Settings(_env_file=None, exit_conviction_enabled=False)
    p = PT.position_from_row({"entry_mcap": 100_000.0, "opened_at": 0.0, "peak_mcap": 100_000.0, "done": "[]",
                              "conv": '{"score": 90, "stop_pct": 35, "grace": true}'}, s)
    assert p.stop_pct is None and not p.grace


# ----------------------------------------------------------------------------- paper and live
def test_paper_calls_are_graded_sized_and_managed(db, monkeypatch):
    m1, m2 = addr("strong"), addr("weak")
    calls = [{"ref": f"early:{m}", "mint": m, "symbol": sym, "kind": "runner", "created_at": time.time(),
              "entry": 100_000.0, "current": 100_000.0, "plan": None, "events": [], "open": True}
             for m, sym in ((m1, "STRONG"), (m2, "WEAK"))]
    monkeypatch.setattr(PT, "calls_since", lambda *_a, **_k: calls)
    monkeypatch.setattr(CV, "report_for", lambda _db, _ref, mint: strong_report() if mint == m1 else weak_report())
    PT.active_run(db, S, "calls")
    assert PT.PaperTrader(S, db).run_once()["opened"] == 2
    rows = {r["symbol"]: dict(r) for r in db.fetchall("SELECT * FROM paper_trades")}
    assert rows["STRONG"]["stop_pct"] > rows["WEAK"]["stop_pct"]
    assert rows["STRONG"]["cost_usd"] > rows["WEAK"]["cost_usd"]           # weaker setup, smaller size

    class Dex:
        async def tokens_batch(self, mints):
            return [make_pair(base_mint=m, market_cap=75_000.0, fdv=75_000.0, liquidity_usd=30_000.0) for m in mints]
    asyncio.run(PT.ExitManager(S, db, Dex()).run_once(now=time.time() + 60))
    rows = {r["symbol"]: dict(r) for r in db.fetchall("SELECT * FROM paper_trades")}
    assert rows["WEAK"]["status"] == "CLOSED"                               # -25%: past its tight stop
    assert rows["STRONG"]["status"] == "OPEN" and rows["STRONG"]["stop_pct"] > 25   # inside its wider stop
    d = PT.summary(db, S)
    assert [g["grade"] for g in d["by_grade"]] == ["C"]
    strong = next(t for t in d["trades"] if t["symbol"] == "STRONG")
    assert strong["conv"]["grade"] == "A" and strong["conv"]["why"]


def test_live_trading_skips_weak_setups_and_sizes_the_rest(db, monkeypatch):
    from test_exits_live_v500 import _call, _live
    t, chain, built, dex, m = _live(db, test_mode=True, exit_conviction_enabled=True)
    monkeypatch.setattr(PT, "calls_since", lambda *_a, **_k: _call(m))
    monkeypatch.setattr(CV, "report_for", lambda *_a: weak_report())
    assert asyncio.run(t.run_once())["bought"] == 0
    row = db.fetchone("SELECT * FROM live_trades")
    assert row["status"] == "SKIPPED" and "conviction" in row["error"]
    db.execute("DELETE FROM live_trades")
    monkeypatch.setattr(CV, "report_for", lambda *_a: strong_report())
    assert asyncio.run(t.run_once())["bought"] == 1
    row = db.fetchone("SELECT * FROM live_trades")
    c = CV.assess(strong_report(), t.s)
    assert row["sol_in"] == pytest.approx(0.1 * c.size_mult) and row["sol_in"] <= 0.1
    assert row["conviction"] == c.score and t.status()["trades"][0]["conv"]["grade"] == "A"


def test_a_trailing_exit_under_the_entry_does_not_claim_a_profit():
    from northstar import trade_plan as TP
    p = TP.build_plan(36_500.0, S, "early")
    ev = dict(TP.evaluate(p, 0.26, 5.9, 600, {"TP1", "TP2"}, lambda v: f"${v:,.0f}"))
    assert "TRAIL" in ev and "keep the profit" not in ev["TRAIL"] and "below your entry" in ev["TRAIL"]
    ev = dict(TP.evaluate(p, 2.8, 5.0, 600, {"TP1", "TP2"}, lambda v: f"${v:,.0f}"))
    assert "keep the profit" in ev["TRAIL"]


def test_test_mode_needs_a_funded_wallet_and_says_so(db, monkeypatch):
    from test_exits_live_v500 import _call, _live
    t, chain, built, dex, m = _live(db, test_mode=True)
    chain.balance = 0.0                                           # a new, empty wallet
    monkeypatch.setattr(PT, "calls_since", lambda *_a, **_k: _call(m))
    assert asyncio.run(t.run_once())["bought"] == 0
    row = db.fetchone("SELECT * FROM live_trades")
    assert row["status"] == "SKIPPED" and "test mode spends nothing" in row["error"]
    assert "simulateTransaction" not in chain.calls               # no doomed simulation


def test_account_not_found_is_explained(db):
    from test_exits_live_v500 import _live
    from northstar.live import TradeError
    t, chain, *_ = _live(db, test_mode=True)

    async def sim(method, params, cache_ttl=0):
        return {"value": {"err": "AccountNotFound", "logs": []}}
    chain.call = sim
    with pytest.raises(TradeError, match="no SOL on-chain"):
        asyncio.run(t._send(b"x", simulate=True))
