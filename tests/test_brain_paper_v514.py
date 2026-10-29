"""v5.1.4: the brain's paper account stopped buying for a week. Since v4.5.0 any HIGH-risk rating (most researched
coins) or a RISK_THRESHOLD / SERIOUS_WARNINGS class was a flat no, and the skip reasons were thrown away. HIGH risk is
now weighed by the setup's conviction grade; CRITICAL, failed launch/holder checks and rug flags still block; and
every decision is counted for the Paper tab."""
from __future__ import annotations

import json
import types

from conftest import addr, make_pair
from northstar import paper as PT
from northstar.models import RiskLevel


def out(mint, risk="ELEVATED", rc=None, strong=True, gates=None, p=0.4):
    report = {"brain": {"p": p}, "unified": {"version": 1, "rug_odds": 0.08 if strong else 0.25}, "risk": risk,
              "gates": gates or [{"name": "mint_authority", "status": "PASS"}],
              "runner": {"score": 85 if strong else 45},
              "launch_buys": {"status": "OBSERVED", "bundle_pct": 2.0, "early_buy_pct": 10.0,
                              "verdict": "ORGANIC" if strong else "SNIPED"},
              "tracked_wallets": {"status": "OBSERVED", "holding_count": 3 if strong else 0},
              "trades": {"buyer_trend": "rising" if strong else "declining"}}
    if rc:
        report["final"] = {"reason_class": rc}
    return types.SimpleNamespace(mint=mint, report=report, risk=RiskLevel(risk),
                                 pair=make_pair(base_mint=mint, base_symbol="BRN", market_cap=60_000.0, fdv=60_000.0,
                                                liquidity_usd=20_000.0))


def test_high_risk_with_a_strong_setup_is_bought(settings, db):
    bt = PT.BrainTrader(settings, db, None)
    assert bt.consider(out(addr("a"))) == "bought"
    t = db.fetchone("SELECT note FROM paper_trades")
    assert "bought despite HIGH risk" in t["note"]


def test_high_risk_with_a_weak_setup_is_skipped(settings, db):
    bt = PT.BrainTrader(settings, db, None)
    why = bt.consider(out(addr("b"), strong=False))
    assert why.startswith("risk system: HIGH risk and conviction") and "under 50" in why


def test_a_risk_threshold_class_is_weighed_too(settings, db):
    bt = PT.BrainTrader(settings, db, None)
    assert bt.consider(out(addr("c"), risk="MODERATE", rc="RISK_THRESHOLD")) == "bought"
    assert "risk threshold" in bt.consider(out(addr("d"), risk="MODERATE", rc="RISK_THRESHOLD", strong=False))


def test_hard_blocks_stay(settings, db):
    bt = PT.BrainTrader(settings, db, None)
    assert bt.consider(out(addr("e"), risk="CRITICAL")) == "risk system: CRITICAL risk"
    assert bt.consider(out(addr("f"), gates=[{"name": "bundled_supply", "status": "FAIL"}])).startswith(
        "risk system: failed bundled supply")
    assert bt.consider(out(addr("g"), rc="SAFETY_FAIL")) == "failed safety fail"


def test_the_old_blanket_veto_is_one_setting_away(settings, db):
    settings.paper_brain_min_conviction = 101
    bt = PT.BrainTrader(settings, db, None)
    assert bt.consider(out(addr("h"))).startswith("risk system: HIGH risk")


def test_every_decision_is_counted_and_saved(settings, db):
    bt = PT.BrainTrader(settings, db, None)
    bt.consider(out(addr("i")))
    bt.consider(out(addr("j"), risk="CRITICAL"))
    bt.consider(types.SimpleNamespace(mint=addr("k"), report={}, risk=None, pair=None))       # a curve coin
    bt.flush_skips()
    st = json.loads(db.fetchone("SELECT value FROM meta WHERE key = ?", (PT.SKIP_META_KEY,))["value"])
    assert st["considered"] == 3 and st["counts"]["bought"] == 1
    assert st["counts"]["risk system: CRITICAL risk"] == 1
    assert any(k.startswith("no brain odds") for k in st["counts"])
    assert "3 coins considered" in bt.skip_summary()
