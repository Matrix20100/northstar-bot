"""v2.5.9 (operator request 22 Oct): tracked wallets, launch buys, snipers still holding, creator
launches/migrations, tiny insider trades, and launches no longer counted as "watched"."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from northstar.analysis import holdings as HO
from northstar.analysis import launch_trades as LT
from northstar.analysis import micro_trades as MT
from northstar.analysis import unified as U
from northstar.analysis.creator import CreatorHistory, analyze_creator, creator_gate, feed_history
from northstar.analysis.pda import TOKEN_2022_PROGRAM_ID, associated_token_address
from northstar.config import Settings
from northstar.db import Database
from northstar.discord_bot.embeds import launch_wallets_text
from northstar.discovery import Discovery
from northstar.models import DataStatus, Trade

MINT = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
W1 = "6ikUcjdzfkdFdsc7o5DXTLpsQLfymeFaajGemAaBGPnx"
W2 = "2toRw8MJ3YNVz1xGSXPzP2PBajNWG7YFnLZc1ymrDe5H"
W3 = "HKjuMsz7JMuiQhkZsAwqogPf1KwhwACZGMcTmmtesoWv"
CREATOR = "7mgVhYzHGYY2CVXqWWx4KLPRjjpSeEVbDqeVk5qqE33h"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.open()
    yield d
    d.close()


# ------------------------------------------------------------------ tracked wallets
def test_axiom_export_is_loaded(tmp_path):
    f = tmp_path / "w.json"
    f.write_text(json.dumps([
        {"trackedWalletAddress": W1, "name": "diego", "groups": ["Main"]},
        {"trackedWalletAddress": W2, "name": "saale", "groups": ["Smart Money"]},
        {"trackedWalletAddress": W1, "name": "dupe", "groups": []},
        {"trackedWalletAddress": "not-a-wallet", "name": "bad"},
    ]))
    ws = HO.load_tracked_wallets(f)
    assert [(w.address, w.name, w.group) for w in ws] == [(W1, "diego", "Main"), (W2, "saale", "Smart Money")]


def test_plain_text_list_and_missing_file(tmp_path):
    f = tmp_path / "w.txt"
    f.write_text(f"# my wallets\n{W1},alpha,Main\n{W3}\n")
    assert [w.address for w in HO.load_tracked_wallets(f)] == [W1, W3]
    assert HO.load_tracked_wallets(tmp_path / "nope.json") == []


def token_account(mint, amount):
    return {"data": {"parsed": {"info": {"mint": mint, "tokenAmount": {"uiAmount": amount}}},
                     "program": "spl-token-2022"}}


class FakeRpc:
    def __init__(self, balances=None, fail=False):
        self.balances, self.fail, self.asked = balances or {}, fail, []

    async def get_multiple_accounts(self, addresses):
        if self.fail:
            raise RuntimeError("rpc down")
        self.asked += addresses
        return [self.balances.get(a) for a in addresses]


def test_holdings_reads_each_wallets_token_account():
    a1 = associated_token_address(W1, MINT, TOKEN_2022_PROGRAM_ID)
    a2 = associated_token_address(W2, MINT, TOKEN_2022_PROGRAM_ID)
    rpc = FakeRpc({a1: token_account(MINT, 20_000_000), a2: token_account(MINT, 0)})
    ws = [HO.TrackedWallet(W1, "diego"), HO.TrackedWallet(W2, "saale"), HO.TrackedWallet(W3, "jerry")]
    h = asyncio.run(HO.wallet_holdings(rpc, MINT, ws, 1_000_000_000, True))
    assert h.status == "OBSERVED" and h.checked == 3
    assert [r["name"] for r in h.holding] == ["diego"] and h.holding[0]["pct"] == 2.0
    assert [r["name"] for r in h.exited] == ["saale"]       # account exists, balance 0: sold out
    assert h.held_pct == 2.0
    assert HO.summary(h.to_dict()) == "diego (2.00% of supply)"


def test_holdings_ignores_an_account_for_another_mint_and_never_guesses_on_error():
    a1 = associated_token_address(W1, MINT, TOKEN_2022_PROGRAM_ID)
    rpc = FakeRpc({a1: token_account("OtherMint1111111111111111111111111111111111", 5)})
    h = asyncio.run(HO.wallet_holdings(rpc, MINT, [HO.TrackedWallet(W1)], 1e9, True))
    assert h.status == "OBSERVED" and not h.holding and not h.exited
    h = asyncio.run(HO.wallet_holdings(FakeRpc(fail=True), MINT, [HO.TrackedWallet(W1)], 1e9, True))
    assert h.status == "UNAVAILABLE" and h.held_pct is None and "read failed" in h.note
    assert asyncio.run(HO.wallet_holdings(FakeRpc(), MINT, [], 1e9, True)).status == "NOT_CONFIGURED"


# ------------------------------------------------------------------ launch buys
def tr(slot, t, wallet, side, pct, sol=0.5):
    return Trade(f"sig{slot}{wallet[:3]}{t}", slot, t, wallet, side, pct / 100 * 1e9, sol, "VERIFIED_PUMP_EVENT")


T0 = 1_800_000_000


def test_bundled_launch_fails():
    trades = [tr(100, T0, CREATOR, "buy", 3), tr(100, T0, W1, "buy", 6), tr(101, T0 + 1, W2, "buy", 5),
              tr(120, T0 + 30, W3, "buy", 0.5)]
    lb = LT.measure(trades, CREATOR, T0, 100)
    LT.judge(lb)
    assert (lb.dev_buy_pct, lb.bundle_pct, lb.bundle_wallets) == (3.0, 11.0, 2)
    assert lb.verdict == "BUNDLED" and LT.gate(lb)[0] == "FAIL"
    assert "creation block" in LT.gate(lb)[1]


def test_giant_jump_in_buys_in_the_first_seconds_fails():
    trades = [tr(100, T0, CREATOR, "buy", 2)] + [tr(103 + i, T0 + 2 + i, w, "buy", 9)
                                                 for i, w in enumerate((W1, W2, W3))]
    lb = LT.measure(trades, CREATOR, T0, 100)
    LT.judge(lb)
    assert lb.early_buy_pct == 27.0 and lb.early_wallets == 3 and lb.snipers == [W1, W2, W3]
    assert lb.verdict == "SNIPED" and "giant jump in buys" in LT.gate(lb)[1]


def test_organic_start_passes_and_late_buys_do_not_count():
    trades = [tr(100, T0, CREATOR, "buy", 1.5), tr(104, T0 + 3, W1, "buy", 1.0), tr(110, T0 + 6, W2, "buy", 0.8),
              tr(200, T0 + 60, W3, "buy", 30)]                 # big buy a minute later: not "the start"
    lb = LT.measure(trades, CREATOR, T0, 100)
    LT.judge(lb)
    assert lb.verdict == "ORGANIC" and LT.gate(lb)[0] == "PASS"
    assert lb.early_buy_pct == 1.8 and lb.bundle_pct == 0.0


def test_too_few_trades_is_unknown_not_organic():
    lb = LT.measure([tr(100, T0, CREATOR, "buy", 2)], CREATOR, T0, 100)
    LT.judge(lb)
    assert lb.verdict == "UNKNOWN" and LT.gate(lb)[0] == "UNKNOWN"


class SigRpc:
    """Curve history of `total` signatures, newest first, 1,000 a page."""

    def __init__(self, total, trades_by_sig):
        self.sigs = [{"signature": f"s{i}", "slot": 100 + i, "blockTime": T0 + i, "err": None}
                     for i in range(total)][::-1]
        self.trades_by_sig, self.pages = trades_by_sig, 0

    async def get_signatures(self, address, limit=1000, before=None):
        self.pages += 1
        start = 0 if before is None else next(i for i, s in enumerate(self.sigs) if s["signature"] == before) + 1
        return self.sigs[start:start + limit]

    async def get_transaction(self, sig):
        return {"sig": sig}


def test_paging_reaches_the_create_transaction(monkeypatch):
    monkeypatch.setattr(LT, "decode_curve_transaction",
                        lambda tx, mint, sig: type("R", (), {"trades": [tr(int(sig[1:]) + 100, T0 + int(sig[1:]),
                                                                            W1, "buy", 0.1)]})())
    rpc = SigRpc(1300, {})
    trades, pages, seen, reached, bt, slot = asyncio.run(LT.first_trades(rpc, "curve", MINT, first_n=5))
    assert reached and pages == 2 and seen == 1300
    assert (bt, slot) == (T0, 100)                      # the oldest signature is the create transaction
    assert [t.slot for t in trades] == [100, 101, 102, 103, 104]


def test_history_too_long_is_incomplete(monkeypatch):
    rpc = SigRpc(3500, {})
    s = Settings(_env_file=None, launch_trades_max_pages=2)
    lb = asyncio.run(LT.analyze(rpc, MINT, "curve", CREATOR, s))
    assert lb.status == "INCOMPLETE" and LT.gate(lb)[0] == "UNKNOWN"
    assert "not reachable" in LT.gate(lb)[1]


# ------------------------------------------------------------------ tiny trades
def test_insider_dust_trades_fail_and_strangers_do_not():
    ins = {W1, W2}
    trades = [tr(1, T0, W1, "buy", 0, sol=0.002), tr(2, T0, W1, "sell", 0, sol=0.001),
              tr(3, T0, W2, "buy", 0, sol=0.004), tr(4, T0, W3, "buy", 0, sol=0.001),
              tr(5, T0, W3, "buy", 0, sol=1.5)]
    m = MT.measure(trades, ins, 0.01)
    assert (m["micro"], m["insider_micro"], m["insider_micro_buys"], m["insider_micro_sells"]) == (4, 3, 2, 1)
    assert MT.gate(m, 3)[0] == "FAIL" and "faking activity" in MT.gate(m, 3)[1]
    assert MT.gate(MT.measure(trades, {W3}, 0.01), 3)[0] == "PASS"
    assert MT.gate(MT.measure(trades, set(), 0.01), 3)[0] == "UNKNOWN"
    assert MT.gate(MT.measure([], ins, 0.01), 3)[0] == "UNKNOWN"


# ------------------------------------------------------------------ creator history from the feed
def launch(disc, mint, creator=CREATOR):
    asyncio.run(disc.on_pumpportal("new_token", {"mint": mint, "traderPublicKey": creator, "name": "x",
                                                 "symbol": "X", "marketCapSol": 30}))


def migrate(disc, mint):
    asyncio.run(disc.on_pumpportal("migration", {"mint": mint, "pool": "pump-amm", "txType": "migrate"}))


def mints(n):
    from northstar.analysis.pump_trades import b58encode
    return [b58encode(bytes([i]) * 32) for i in range(1, n + 1)]


def test_feed_counts_launches_and_migrations_per_creator(db):
    disc = Discovery(Settings(_env_file=None), db, None)
    ms = mints(4)
    for m in ms:
        launch(disc, m)
    launch(disc, ms[0])                       # a repeated event is not a new launch
    migrate(disc, ms[1])
    migrate(disc, ms[1])                      # nor is a repeated migration
    migrate(disc, ms[2])
    row = db.creator_stats(CREATOR)
    assert (row["launches"], row["migrated"]) == (4, 2)
    # the coin being researched is not its own "prior" launch
    assert feed_history(db, ms[1], CREATOR) == {"prior_launches": 3, "prior_migrated": 1, "since": row["first_ts"]}
    assert feed_history(db, ms[3], CREATOR)["prior_migrated"] == 2


def test_creator_gate_thresholds(db):
    s = Settings(_env_file=None)
    farm = CreatorHistory(DataStatus.OBSERVED, CREATOR, prior_launches=3, prior_migrated=2)
    assert creator_gate(farm, s)[0] == "FAIL" and "launch-farm" in creator_gate(farm, s)[1]
    serial = CreatorHistory(DataStatus.OBSERVED, CREATOR, prior_launches=7, prior_migrated=0)
    assert creator_gate(serial, s)[0] == "FAIL" and "serial launcher" in creator_gate(serial, s)[1]
    fresh = CreatorHistory(DataStatus.OBSERVED, CREATOR, prior_launches=1, prior_migrated=0)
    assert creator_gate(fresh, s)[0] == "PASS"
    assert creator_gate(CreatorHistory(DataStatus.UNKNOWN), s)[0] == "UNKNOWN"


def test_analyze_creator_uses_the_feed_when_rugcheck_has_no_history(db):
    disc = Discovery(Settings(_env_file=None), db, None)
    ms = mints(3)
    for m in ms:
        launch(disc, m)
    migrate(disc, ms[0])
    migrate(disc, ms[1])
    ch = asyncio.run(analyze_creator(ms[2], CREATOR, "rugcheck", {"creator": CREATOR}, None, None, db))
    assert ch.status == DataStatus.OBSERVED and (ch.prior_launches, ch.prior_migrated) == (2, 2)
    assert any(f.code == "CREATOR_PRIOR_MIGRATIONS" for f in ch.flags)
    assert creator_gate(ch, Settings(_env_file=None))[0] == "FAIL"


def test_backfill_from_stored_launch_events_runs_once(db):
    ms = mints(3)
    for m in ms:
        db.upsert_discovery(m, "pumpportal_new", 1.0, "x", "X", {"traderPublicKey": CREATOR})
    db.upsert_discovery(ms[0], "pumpportal_migration", 3.0, "x", "X", {"pool": "pump-amm"})
    assert db.backfill_creator_stats() == 1
    row = db.creator_stats(CREATOR)
    assert (row["launches"], row["migrated"]) == (3, 1)
    assert db.backfill_creator_stats() == 0


def test_one_off_creators_are_forgotten(db):
    db.record_creator_launch("A" * 44, ts=time.time() - 5 * 86400)
    db.record_creator_launch("B" * 44, ts=time.time() - 5 * 86400)
    db.record_creator_launch("B" * 44, ts=time.time() - 5 * 86400)
    assert db.prune_creator_stats() == 1
    assert db.creator_stats("A" * 44) is None and db.creator_stats("B" * 44)["launches"] == 2


# ------------------------------------------------------------------ decisions
@pytest.mark.parametrize("gate", ["launch_buys", "sniper_holdings", "insider_micro_trades", "creator_history"])
def test_each_rug_filter_rejects(gate):
    from test_unified_engine import gates, report
    s = Settings(_env_file=None, min_liquidity_usd=15_000, min_narrative_evidence_items=0)
    u = U.evaluate(report(gates=gates(**{gate: "FAIL"})), s)
    assert u["decision"] == U.REJECT and u["reason_class"] == "RISK_THRESHOLD"
    r = report(gates=gates(**{gate: "UNKNOWN"}))
    for g in r["gates"]:
        if g["name"] == gate:
            g["critical"] = False
    assert U.evaluate(r, s)["decision"] == U.STRONG, "missing data never blocks on its own"


def test_posts_show_tracked_wallets_launch_and_creator():
    txt = launch_wallets_text({
        "tracked_wallets": {"status": "OBSERVED", "checked": 1436, "holding_count": 2, "held_pct": 1.25,
                            "holding": [{"wallet": W1, "name": "orangie"}, {"wallet": W2, "name": ""}]},
        "launch_buys": {"verdict": "ORGANIC", "early_wallets": 4, "early_buy_pct": 3.2, "bundle_pct": 0.0,
                        "dev_buy_pct": 1.5, "sniper_held_pct": 0.8},
        "creator": {"status": "OBSERVED", "prior_launches": 1, "prior_migrated": 0}})
    assert "orangie, 2toR…De5H (1.25% of supply)" in txt
    assert "first 4 buyers took 3.2%" in txt and "snipers still hold 0.8%" in txt
    assert "1 other launch(es), 0 migrated" in txt
    none = launch_wallets_text({"tracked_wallets": {"status": "OBSERVED", "checked": 1436, "holding_count": 0}})
    assert "None of your 1436 tracked wallets" in none
    assert launch_wallets_text({}) is None


# ------------------------------------------------------------------ "watched" count
def test_unscheduled_launches_expire_quickly_and_the_curve_radar_brings_them_back(db):
    now = time.time()
    ms = mints(4)
    for m in ms:
        db.upsert_discovery(m, "pumpportal_new", 1.0, "x", "X", {"traderPublicKey": CREATOR})
    db.execute("UPDATE candidates SET discovered_at = ?, next_research_at = NULL", (now - 3600,))
    db.execute("UPDATE candidates SET next_research_at = ? WHERE mint = ?", (now, ms[1]))      # queued
    db.upsert_discovery(ms[2], "dexscreener_profile", 2.0, "x", "X", {"links": []})             # 2 feeds
    db.execute("UPDATE candidates SET discovered_at = ? WHERE mint = ?", (now - 60, ms[3]))    # brand new
    assert db.expire_unscheduled_launches(30, now) == 1
    states = {r["mint"]: r["state"] for r in db.fetchall("SELECT mint, state FROM candidates")}
    assert states == {ms[0]: "STALE", ms[1]: "DISCOVERED", ms[2]: "DISCOVERED", ms[3]: "DISCOVERED"}
    # real buying on the curve: the radar's event revives it for research
    db.upsert_discovery(ms[0], "curve_radar", 9.0, "x", "X", {"market_cap": 40_000, "buys_m5": 30})
    row = db.fetchone("SELECT state, next_research_at FROM candidates WHERE mint = ?", (ms[0],))
    assert row["state"] == "DISCOVERED" and row["next_research_at"] is not None


def test_dashboard_counts_only_followed_coins(db):
    from northstar.dashboard import view_watching
    ms = mints(4)
    for m in ms:
        db.upsert_discovery(m, "pumpportal_new", 1.0, "x", "X", {})
    db.execute("UPDATE candidates SET next_research_at = NULL")
    db.execute("UPDATE candidates SET state = 'WATCHLIST' WHERE mint = ?", (ms[0],))
    db.execute("UPDATE candidates SET next_research_at = ? WHERE mint = ?", (time.time(), ms[1]))
    w = view_watching(db._conn, time.time())
    assert (w["watched"], w["queued"], w["waiting_launches"]) == (1, 1, 2)
    assert [q["mint"] for q in w["queue"]] == [ms[0], ms[1]]


# ------------------------------------------------------------------ full research
async def test_research_checks_tracked_wallets_and_records_the_new_filters(settings, db, tmp_path):
    from conftest import MINT as CMINT, healthy_holders, seed_profile, strong_providers
    from northstar.analysis.pda import TOKEN_PROGRAM_ID
    from northstar.research import Researcher

    holder = healthy_holders()[0][0]
    f = tmp_path / "tracked.json"
    f.write_text(json.dumps([{"trackedWalletAddress": holder, "name": "whale", "groups": ["Smart Money"]},
                             {"trackedWalletAddress": W3, "name": "nobody", "groups": ["Main"]}]))
    settings.tracked_wallets_file = str(f)
    p = strong_providers()
    base = p.rpc.get_multiple_accounts
    ata = associated_token_address(holder, CMINT, TOKEN_PROGRAM_ID)

    async def accounts(addrs):
        if ata in addrs:                               # the tracked-wallet read
            return [token_account(CMINT, 12_500_000) if a == ata else None for a in addrs]
        return await base(addrs)

    p.rpc.get_multiple_accounts = accounts
    seed_profile(db)
    out = await Researcher(settings, db, p).research(CMINT)
    tw = out.report["tracked_wallets"]
    assert tw["status"] == "OBSERVED" and tw["checked"] == 2
    assert [r["name"] for r in tw["holding"]] == ["whale"] and tw["held_pct"] == 1.25
    names = {g["name"]: g["status"] for g in out.report["gates"]}
    assert "creator_history" not in names, "recorded, not a gate, unless creator_history_blocks is on"
    assert out.report["creator"]["check"]["blocking"] is False
    assert names["insider_micro_trades"] in ("PASS", "UNKNOWN")
    assert out.report["micro_trades"]["sample"] > 0


@pytest.mark.parametrize("case", ["organic", "bundled", "farm"])
async def test_an_early_watch_must_pass_the_rug_filters(settings, db, monkeypatch, case):
    from conftest import MINT as CMINT, strong_providers
    from test_launch_shape import _curve_coin
    from northstar.models import Action
    from northstar.research import Researcher

    p = strong_providers()
    cand = _curve_coin(db, p)
    first = [tr(100, T0, CREATOR, "buy", 2), tr(110, T0 + 5, W1, "buy", 1), tr(130, T0 + 20, W2, "buy", 1)]
    if case == "bundled":
        first += [tr(100, T0, W3, "buy", 14)]

    async def fake_first(rpc, curve, mint, first_n=40, max_pages=5, concurrency=8):
        return first, 1, len(first), True, T0, 100

    monkeypatch.setattr(LT, "first_trades", fake_first)
    if case == "farm":
        settings.creator_history_blocks = True
        for m in mints(4):
            db.record_creator_launch(CREATOR)
        db.execute("UPDATE creator_stats SET migrated = 3 WHERE creator = ?", (CREATOR,))
    db.execute("UPDATE discovery_events SET payload = ? WHERE mint = ? AND source = 'pumpportal_new'",
               (json.dumps({"bondingCurveKey": json.loads(db.fetchone(
                   "SELECT payload FROM discovery_events WHERE mint = ?", (CMINT,))["payload"])["bondingCurveKey"],
                   "traderPublicKey": CREATOR}), CMINT))
    out = await Researcher(settings, db, p).research(CMINT, cand)
    lb = out.report["launch_buys"]
    if case == "organic":
        assert out.action == Action.WATCHLIST and lb["verdict"] == "ORGANIC"
    elif case == "bundled":
        assert out.action == Action.REJECT and lb["verdict"] == "BUNDLED"
        assert out.reasons[0].startswith("launch buys:")
    else:
        assert out.action == Action.REJECT and any(r.startswith("creator history:") for r in out.reasons)


async def test_a_launch_farm_is_shown_but_not_rejected_by_default(settings, db):
    from conftest import MINT as CMINT, strong_providers
    from test_launch_shape import _curve_coin
    from northstar.models import Action
    from northstar.research import Researcher

    p = strong_providers()
    cand = _curve_coin(db, p)
    for _ in range(6):
        db.record_creator_launch(CREATOR)
    row = db.fetchone("SELECT payload FROM discovery_events WHERE mint = ?", (CMINT,))
    db.execute("UPDATE discovery_events SET payload = ? WHERE mint = ?",
               (json.dumps({**json.loads(row["payload"]), "traderPublicKey": CREATOR}), CMINT))
    out = await Researcher(settings, db, p).research(CMINT, cand)
    assert out.report["creator"]["check"]["status"] == "FAIL" and out.action == Action.WATCHLIST


async def test_the_launch_is_read_once_per_coin(settings, db, monkeypatch):
    from conftest import MINT as CMINT, strong_providers
    from test_launch_shape import _curve_coin
    from northstar.research import Researcher

    p = strong_providers()
    cand = _curve_coin(db, p)
    calls = []

    async def fake_first(rpc, curve, mint, first_n=40, max_pages=5, concurrency=8):
        calls.append(mint)
        return [tr(100, T0, CREATOR, "buy", 2), tr(110, T0 + 5, W1, "buy", 1), tr(130, T0 + 20, W2, "buy", 1)], \
            1, 3, True, T0, 100

    monkeypatch.setattr(LT, "first_trades", fake_first)
    r = Researcher(settings, db, p)
    a = await r.research(CMINT, cand)
    b = await r.research(CMINT, db.get_candidate(CMINT))
    assert len(calls) == 1
    assert a.report["launch_buys"]["verdict"] == b.report["launch_buys"]["verdict"] == "ORGANIC"
