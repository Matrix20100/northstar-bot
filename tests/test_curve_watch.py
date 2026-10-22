"""v2.6.2: outcomes of pump.fun coins in the early band, measured from the curve radar's own data."""
from __future__ import annotations

import time

from conftest import make_pair
from northstar import curve_watch as CW
from northstar.config import Settings

S = Settings(_env_file=None)
T0 = 1_800_000_000.0


def curve(mint, mc, vol=9_000, buys=60, sells=30, age_min=10, dex="pumpfun", now=T0):
    return make_pair(base_mint=mint, dex_id=dex, market_cap=mc, fdv=mc, liquidity_usd=mc * 0.35,
                     pair_created_at=now - age_min * 60, vol={"m5": vol, "h1": vol * 6, "h6": vol * 20},
                     txns={"m5": {"buys": buys, "sells": sells}, "h1": {"buys": 400, "sells": 300}},
                     price_change={"m5": 12.0, "h1": 80.0})


def test_entry_is_recorded_once_then_peak_low_and_migration_are_followed(db):
    CW.observe(db, {"A": [curve("A", 5_000)]}, T0, S)                 # below the band: not yet
    assert db.fetchone("SELECT COUNT(*) n FROM curve_watch")["n"] == 0
    CW.observe(db, {"A": [curve("A", 30_000, age_min=12)]}, T0 + 60, S)
    row = db.fetchone("SELECT * FROM curve_watch WHERE mint='A'")
    assert (row["entry_mc"], row["age_min"], row["vol_m5"], row["buys_m5"]) == (30_000, 13.0, 9_000, 60)
    CW.observe(db, {"A": [curve("A", 15_000)]}, T0 + 600, S)          # dips to 0.5x
    CW.observe(db, {"A": [curve("A", 90_000, dex="pumpswap")]}, T0 + 1800, S)   # migrates, 3x
    CW.observe(db, {"A": [curve("A", 60_000, dex="pumpswap")]}, T0 + 2400, S)
    row = db.fetchone("SELECT * FROM curve_watch WHERE mint='A'")
    assert row["peak_mc"] == 90_000 and row["peak_at"] == T0 + 1800
    assert row["low_mc"] == 15_000 and row["low_before_peak"] == 15_000
    assert row["migrated"] == 1 and row["last_mc"] == 60_000
    assert CW.open_mints(db, T0 + 2400, 4) == ["A"]
    res = CW.observe(db, {}, T0 + 5 * 3600, S)
    assert res["closed"] == 1 and CW.open_mints(db, T0 + 5 * 3600, 4) == []
    # a coin is scored once: re-entering the band later does not create a second entry
    CW.observe(db, {"A": [curve("A", 30_000)]}, T0 + 6 * 3600, S)
    assert db.fetchone("SELECT COUNT(*) n FROM curve_watch")["n"] == 1


def test_a_migrated_coin_never_enters(db):
    CW.observe(db, {"B": [curve("B", 30_000, dex="pumpswap")]}, T0, S)
    assert db.fetchone("SELECT COUNT(*) n FROM curve_watch")["n"] == 0


def _scored(db, mint, entry, peak, low, low_before, age=10, vol=9_000, buys=60, sells=30, now=None):
    now = now or time.time()
    db.execute("INSERT INTO curve_watch(mint, entered_at, age_min, entry_mc, vol_m5, buys_m5, sells_m5, peak_mc, "
               "low_mc, low_before_peak, last_mc, last_ts, migrated, done) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
               (mint, now - 6 * 3600, age, entry, vol, buys, sells, peak, low, low_before, low, now, int(peak > 70_000)))


def test_rules_are_scored_on_real_outcomes(db):
    CW.init_schema(db)
    _scored(db, "w1", 30_000, 95_000, 25_000, 25_000)                  # clean 3x
    _scored(db, "w2", 40_000, 85_000, 10_000, 15_000)                  # 2x after a 0.375x dip: not clean
    _scored(db, "r1", 50_000, 52_000, 8_000, 50_000)                   # rug
    _scored(db, "old", 30_000, 90_000, 30_000, 30_000, age=120)        # too old for the clone
    _scored(db, "thin", 30_000, 30_000, 25_000, 30_000, vol=2_000)     # too little volume
    ev = CW.evaluate(db)
    assert ev["scored"] == 5
    nex = ev["rules"]["nexas clone"]
    assert nex["n"] == 3
    assert (nex["hit_2x"], nex["hit_3x"], nex["clean_2x"], nex["fell_to_0_3x"]) == (66.7, 33.3, 33.3, 66.7)
    assert ev["rules"]["every coin in the band"]["n"] == 5
    text = CW.format_report(ev)
    assert "nexas clone" in text and "clean2x" in text


def test_dashboard_serves_the_rule_table(db):
    import json
    from test_dashboard import serve
    CW.init_schema(db)
    _scored(db, "w1", 30_000, 95_000, 25_000, 25_000)
    srv, c = serve(db, "pw")
    assert c.req("/api/login", {"password": "pw"})[0] == 200
    st, body = c.req("/api/curve")
    d = json.loads(body)
    assert st == 200 and d["scored"] == 1 and d["rules"]["nexas clone"]["hit_3x"] == 100.0
    srv.server.shutdown()


def test_prune_keeps_recent_rows(db):
    CW.init_schema(db)
    _scored(db, "a", 30_000, 30_000, 30_000, 30_000, now=time.time() - 30 * 86400)
    _scored(db, "b", 30_000, 30_000, 30_000, 30_000)
    assert CW.prune(db) == 1


async def test_the_curve_radar_feeds_the_watch(db):
    from northstar.early_calls import CurveRadar
    from northstar.analysis.pump_trades import b58encode
    mint = b58encode(bytes([7]) * 32)
    db.upsert_discovery(mint, "pumpportal_new", 1.0, "x", "X", {"traderPublicKey": "dev"})

    class Dex:
        async def tokens_batch(self, mints):
            return [curve(mint, 35_000, now=time.time())] if mint in mints else []

    s = Settings(_env_file=None)
    await CurveRadar(s, db, Dex()).run_once()
    row = db.fetchone("SELECT * FROM curve_watch WHERE mint = ?", (mint,))
    assert row and row["entry_mc"] == 35_000
