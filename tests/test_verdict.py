"""Second-brain rug verdict and embed sizing. SYNTHETIC reports."""
from __future__ import annotations

from northstar.analysis.verdict import assess, bubble_summary, embed_text
from northstar.discord_bot.embeds import callout_embed, embed_size


def base(**kw):
    r = {"mint": "M", "security": {"mint_authority": "REVOKED (VERIFIED)", "freeze_authority": "REVOKED (VERIFIED)",
                                   "lp_status": "BURNED — 100.0% of LP tokens burned (VERIFIED on-chain, PumpSwap pool)"},
         "holders": {"top10_adj": 16.0, "max_single_adj": 3.0, "creator_pct": 0.0},
         "clusters": {"status": "INFERRED", "max_cluster_supply_pct": 0.0, "clusters": [], "fresh_supply_pct": 1.0,
                      "launch_bundle_pct": 2.0, "bubbles": [{"id": "a"}], "wallets_aged": 10},
         "trades": {"unique_buyers": 120, "creator_sold_pct_supply": 0.0},
         "creator": {"prior_launches": 2, "prior_checked": 2, "prior_rugged": 0, "summary": "2 prior"},
         "flags": [], "gates": []}
    for k, v in kw.items():
        r[k] = {**r.get(k, {}), **v} if isinstance(v, dict) else v
    return r


def test_clean_coin_looks_clean_with_reasons():
    v = assess(base())
    assert v["level"] == "LOOKS CLEAN" and v["score"] == 0 and not v["red"]
    assert "LP burned/locked (verified on-chain)" in v["green"] and "mint authority revoked" in v["green"]


def test_active_mint_authority_is_a_rug():
    v = assess(base(security={"mint_authority": "ACTIVE — Abcd…wxyz (VERIFIED)"}))
    assert v["level"] == "RUG LIKELY" and v["score"] == 100 and "MINT" in v["headline"]


def test_trap_flags_are_rugs():
    for code in ("PERMANENT_DELEGATE", "RUGCHECK_RUGGED", "TRANSFER_HOOK"):
        assert assess(base(flags=[{"code": code}]))["level"] == "RUG LIKELY"
    # calibrated on real outcomes: a liquidity drop usually fires after the dump; serious, not a trap
    v = assess(base(flags=[{"code": "LIQUIDITY_REMOVAL"}]))
    assert v["level"] == "CAUTION" and v["red"][0]["level"] == "high"


def test_data_backed_flags_are_serious():
    for code in ("POSSIBLE_WASH_TRADING", "BUYING_CONCENTRATED", "POSSIBLE_COPYCAT", "VOLUME_LIQUIDITY_IMPLAUSIBLE"):
        assert assess(base(flags=[{"code": code}]))["red"][0]["level"] == "high"
    assert assess(base(holders={"max_single_adj": 9.0}))["red"][0]["level"] == "high"


def test_learned_crash_rates_annotate_and_upgrade():
    stats = {"top10": {"n": 40, "crash": 36, "run2x": 4}, "insider_holders": {"n": 5, "crash": 5, "run2x": 0}}
    v = assess(base(holders={"top10_adj": 30.0}), stats=stats)
    red = v["red"][0]
    assert red["key"] == "top10" and red["crash_rate"] == 0.9 and red["upgraded"] and red["level"] == "high"
    assert "crashed 90% of 40 times" in embed_text(v)
    v2 = assess(base(flags=[{"code": "INSIDER_HOLDERS"}]), stats=stats)
    assert "crash_rate" not in v2["red"][0] and v2["red"][0]["level"] == "medium"     # too few to trust


def test_connected_wallets_and_fresh_supply_are_high_risk():
    v = assess(base(clusters={"max_cluster_supply_pct": 24.0, "fresh_supply_pct": 18.0,
                              "clusters": [{"strength": "STRONG", "held_pct_of_supply_top20_only": 24.0, "size": 5}]}))
    assert v["level"] == "HIGH RISK"
    texts = " ".join(r["text"] for r in v["red"])
    assert "24.0%" in texts and "brand-new wallets" in texts


def test_dev_rug_history_and_selling():
    v = assess(base(creator={"prior_rugged": 3}, trades={"creator_sold_pct_supply": 4.0}))
    assert v["level"] == "HIGH RISK" and any("rugged" in r["text"] for r in v["red"])


def test_one_medium_flag_is_caution():
    v = assess(base(holders={"top10_adj": 33.0}, clusters={"launch_bundle_pct": 9.0}))
    assert v["level"] == "CAUTION"


def test_missing_key_checks_is_not_called_clean():
    v = assess({"mint": "M", "security": {}, "holders": {}, "clusters": {}, "flags": [], "gates": []})
    assert v["level"] == "NOT ENOUGH DATA"


def test_pre_migration_has_no_lp_to_pull():
    v = assess(base(lifecycle="PRE_MIGRATION_BONDING_CURVE", security={"lp_status": "NOT_APPLICABLE"}))
    assert any("bonding curve" in g for g in v["green"])


def test_bubble_summary_and_embed_text():
    cl = {"clusters": [{"strength": "CONFIRMED", "held_pct_of_supply_top20_only": 12.5, "size": 4}],
          "two_hop_links": 2, "hub_funders": ["x"], "fresh_supply_pct": 3.0, "bubbles": [{}]}
    s = bubble_summary(cl)
    assert "12.5% across 4 wallets (linked to the dev)" in s and "2 wallets linked through a middle wallet" in s and "1 exchange" in s
    t = embed_text(assess(base(clusters=cl)))
    assert t.startswith("🔴") or t.startswith("🟠")
    assert "🫧" in t


def test_callout_embed_shows_rug_check_and_always_fits_discord():
    rep = base(identity={"symbol": "MOCK", "name": "Mock"}, pool={"address": "P"},
               market={"market_cap": {"value": 100_000}})
    rep["verdict"] = assess(rep)
    e = callout_embed(rep, plan=["x" * 1000])
    assert any(f["name"] == "🧠 Rug check" and "LOOKS CLEAN" in f["value"] for f in e["fields"])
    rep["narrative"] = {"thesis": "y" * 3000}
    rep["caveats"] = ["z" * 1000] * 5
    rep["market_structure"] = {"entry_quality": "w" * 2000}
    rep["brain"] = {"p": 0.3, "ups": ["a" * 900], "downs": ["b" * 900], "trusted": True, "auc": 0.6, "n": 9}
    big = callout_embed(rep, plan=["p" * 1023])
    assert embed_size(big) <= 5800
