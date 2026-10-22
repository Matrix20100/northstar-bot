"""v2.5.7: the launch-shape gate. Synthetic 1-minute candles shaped like the charts the operator
showed (22 Oct): an organic climb, an instant jump, a staircase of a few giant buys, a dump."""
from __future__ import annotations

import time

import pytest

from conftest import MINT, MemoryPublisher, make_pair, seed_profile, strong_providers
from northstar.analysis import launch as LA
from northstar.analysis import unified as U
from northstar.config import Settings
from northstar.engine import Engine
from northstar.models import Action
from northstar.research import Researcher

T0 = 1_800_000_000.0


def c(minute, o, h, low, cl, v=500.0):
    return (T0 + minute * 60, o, h, low, cl, v)


def organic(n=60):
    out, p = [], 1.0
    for m in range(n):                       # trades every minute, ~2% a minute, small pullbacks
        nxt = p * (1.025 if m % 4 else 0.985)
        out.append(c(m, p, max(p, nxt) * 1.01, min(p, nxt) * 0.99, nxt))
        p = nxt
    return out


def instant_jump():
    return [c(0, 1.0, 9.0, 1.0, 8.5), c(1, 8.5, 9.5, 7.0, 8.0)] + [c(m, 8.0, 8.4, 7.6, 8.0) for m in range(2, 40)]


def staircase():
    out = [c(0, 1.0, 1.02, 0.99, 1.0), c(1, 1.0, 1.01, 0.99, 1.0)]
    for m, (a, b) in ((5, (1.0, 2.0)), (15, (2.0, 3.0)), (25, (3.0, 4.6))):
        out += [c(m, a, b, a, b), c(m + 1, b, b * 1.01, b * 0.99, b)]
    return out + [c(m, 4.6, 4.65, 4.55, 4.6) for m in range(40, 46)]


def test_an_organic_climb_passes():
    lp = LA.analyze(organic(), T0, now_price=None)
    assert lp.verdict == "ORGANIC" and LA.gate(lp)[0] == "PASS"
    assert lp.first_jump_x < 1.2 and lp.active_share == 1.0


def test_an_instant_launch_jump_fails():
    lp = LA.analyze(instant_jump(), T0, now_price=8.0)
    assert lp.verdict == "INSTANT_JUMP" and LA.gate(lp)[0] == "FAIL"
    assert "first 2 minutes" in LA.gate(lp)[1]


def test_a_staircase_of_a_few_giant_buys_fails():
    lp = LA.analyze(staircase(), T0, now_price=4.6)
    assert lp.verdict == "STAIRCASE", lp
    assert lp.top3_share >= 0.85 and lp.active_share < 0.4


def test_a_dumped_coin_is_not_watched():
    rows = organic(60) + [c(60 + i, 3.0, 3.0, 0.5, 0.6) for i in range(5)]
    peak = max(r[2] for r in rows)
    lp = LA.analyze(rows, T0, now_price=peak * 0.2)
    assert lp.verdict == "DUMPED" and LA.gate(lp)[0] == "FAIL"
    assert LA.analyze(rows, T0, now_price=peak * 0.8).verdict == "ORGANIC"


def test_no_candles_is_unknown_never_organic():
    lp = LA.analyze([], T0, now_price=1.0)
    assert lp.status == "UNAVAILABLE" and LA.gate(lp)[0] == "UNKNOWN"


def test_the_earliest_pool_is_where_the_coin_started():
    curve = make_pair(pair_address="CurvePool1111111111111111111111111111111111", dex_id="pumpfun",
                      pair_created_at=T0 - 3600)
    amm = make_pair(pair_address="AmmPool11111111111111111111111111111111111", dex_id="pumpswap",
                    pair_created_at=T0)
    assert LA.launch_pool([amm, curve], amm) == (curve.pair_address, "pumpfun", T0 - 3600)


def test_a_failed_launch_shape_rejects_and_is_not_watched():
    from test_unified_engine import gates, report
    s = Settings(_env_file=None, min_liquidity_usd=15_000, min_narrative_evidence_items=0)
    r = report(gates=gates(launch_shape="FAIL"))
    u = U.evaluate(r, s)
    assert u["decision"] == U.REJECT and u["reason_class"] == "RISK_THRESHOLD"
    r = report(gates=gates(launch_shape="UNKNOWN") )
    for g in r["gates"]:
        if g["name"] == "launch_shape":
            g["critical"] = False
    assert U.evaluate(r, s)["decision"] == U.STRONG, "missing launch data never blocks on its own"


class FakeCG:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    async def candles(self, pool, start, end=None, urgent=False, timeframe="minute", token=None):
        self.calls.append((pool, token))
        return self.rows


async def test_research_records_the_launch_and_rejects_a_rigged_start(settings, db):
    p = strong_providers()
    p.coingecko = FakeCG(instant_jump())
    seed_profile(db)
    out = await Researcher(settings, db, p).research(MINT)
    assert out.report["launch"]["verdict"] == "INSTANT_JUMP"
    gate = next(g for g in out.report["gates"] if g["name"] == "launch_shape")
    assert gate["status"] == "FAIL"
    assert out.action == Action.REJECT
    assert p.coingecko.calls and p.coingecko.calls[0][1] == MINT, "candles for the coin, not for SOL"


async def test_research_without_a_chart_source_keeps_its_decision(settings, db):
    p = strong_providers()                     # no coingecko provider
    seed_profile(db)
    out = await Researcher(settings, db, p).research(MINT)
    assert out.report["launch"]["status"] == "UNAVAILABLE"
    assert next(g for g in out.report["gates"] if g["name"] == "launch_shape")["status"] == "UNKNOWN"
    assert out.action != Action.REJECT or "launch" not in " ".join(out.reasons)


async def test_a_watched_coin_leaves_the_list_after_its_rechecks(settings, db):
    p = strong_providers()
    p.dex.metas = []
    seed_profile(db)
    db.upsert_discovery(MINT, "dexscreener_profile", 5.0, payload={"x": 1})
    eng = Engine(settings.model_copy(update={"watchlist_max_rechecks": 1}), db, p, MemoryPublisher())
    await eng.process(MINT)
    cand = db.get_candidate(MINT)
    assert cand["state"] == "STALE" and cand["next_research_at"] is None, "watched once, then off the list"
