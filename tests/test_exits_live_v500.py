"""v5.0.0: the exit engine, paper positions managed live, the Calls tab's bot exit, and live trading
(signing, test mode, real mode against a fake chain, limits, kill switch). No network, no real keys:
the wallet below is generated for the test."""
from __future__ import annotations

import asyncio
import json
import time

import base58
import pytest

from conftest import addr, make_pair
from northstar import exits as EX
from northstar import paper as PT
from northstar.config import Settings
from northstar.live import LiveTrader
from northstar.solana_tx import KeyError_, Wallet

S = Settings(_env_file=None)


def tick(x, t=0.0, entry=100_000.0, buys=50, sells=40, vol_m5=10_000.0, vol_h1=100_000.0, chg=0.0):
    return EX.Tick(mcap=entry * x, at=t, buys_m5=buys, sells_m5=sells, vol_m5=vol_m5, vol_h1=vol_h1, chg_m5=chg)


def pos(peak_x=1.0, entry=100_000.0):
    return EX.Position(entry_mcap=entry, opened_at=0.0, peak_mcap=entry * peak_x)


# ----------------------------------------------------------------------------- the rules
def test_hard_stop_sells_everything():
    a = EX.decide(pos(), tick(0.79, 60), S)
    assert [(x.kind, x.fraction) for x in a] == [("STOP", EX.ALL)]
    assert EX.decide(pos(), tick(0.85, 60), S) == []


def test_trailing_stop_ratchets_up_and_never_down():
    p = pos()
    assert EX.decide(p, tick(1.25, 60), S) == []                    # under 1.3x: no trail yet
    EX.decide(p, tick(1.4, 120), S)                                 # new high 1.4x -> stop at break even+
    assert p.stop_x == pytest.approx(1.05)                          # 1.4 * 0.75 = 1.05 (above 1.02)
    EX.decide(p, tick(2.5, 180), S)                                 # 2.5x high: gives back 20% -> 2.0x
    assert p.stop_x == pytest.approx(2.0)
    a = EX.decide(p, tick(1.99, 240, buys=60, sells=50), S)
    assert a and a[0].kind == "TRAIL"
    p2 = pos()
    EX.decide(p2, tick(6.0, 60), S)                                 # 5x+: gives back 15%
    assert p2.stop_x == pytest.approx(5.1)


def test_take_profit_in_steps():
    p = pos()
    a = EX.decide(p, tick(1.6, 60), S)
    assert [(x.kind, x.fraction) for x in a] == [("TP1", 0.35)]
    p.done.add("TP1")
    a = EX.decide(p, tick(2.6, 120), S)
    assert [(x.kind, x.fraction) for x in a] == [("TP2", 0.35)]


def test_sellers_taking_over_and_fading_volume():
    p = pos(peak_x=1.6)
    a = EX.decide(p, tick(1.45, 300, buys=20, sells=45, chg=-14), S)
    assert a[0].kind == "SELLERS"
    p = pos(peak_x=1.6)
    a = EX.decide(p, tick(1.3, 900, buys=10, sells=9, vol_m5=1_500, vol_h1=120_000), S)
    assert a[0].kind == "FADING"


def test_dead_money_is_cut_early_and_a_rising_coin_is_held():
    a = EX.decide(pos(peak_x=1.05), tick(0.9, 9 * 60, buys=5, sells=6), S)
    assert a[0].kind == "NO_PROGRESS"
    # a new high with buyers in control: momentum exits wait (heavy selling on a pump is not a dump)
    p = pos(peak_x=1.2)
    assert EX.decide(p, tick(1.25, 900, buys=80, sells=70, vol_m5=500, vol_h1=200_000), S) == []


def test_time_stop():
    assert EX.decide(pos(peak_x=1.2), tick(1.1, 181 * 60), S)[0].kind == "TIME"


def test_settings_change_the_rules():
    s = Settings(_env_file=None, exit_stop_pct=10)
    assert EX.decide(pos(), tick(0.89, 60), s)[0].kind == "STOP"


def test_replay_a_price_path():
    path = [tick(x, 60 * i) for i, x in enumerate([1.1, 1.6, 2.6, 3.0, 2.2, 1.0])]
    r = EX.replay(100_000.0, 0.0, path, S)
    # 35% at 1.6x, 35% at 2.6x, rest at the trail (3.0x high -> stop 2.4x, sold at 2.2x read)
    assert r["exit"] == "TRAIL" and not r["open"]
    assert r["x"] == pytest.approx(0.35 * 1.6 + 0.35 * 2.6 + 0.30 * 2.2)


# ----------------------------------------------------------------------------- paper: exit manager
class Dex:
    def __init__(self, pairs):
        self.pairs = pairs

    async def tokens_batch(self, mints):
        return [p for p in self.pairs if p.base_mint in mints]


def test_exit_manager_sells_paper_positions_on_live_reads(db):
    s = Settings(_env_file=None)
    m = addr("pp")
    run = PT.active_run(db, s, "calls")
    assert PT._buy(db, run, "early:" + m, m, "PP", "runner", 100_000.0, None, time.time() - 120) is None
    dex = Dex([make_pair(base_mint=m, market_cap=150_000.0, fdv=150_000.0)])
    em = PT.ExitManager(s, db, dex)
    c = asyncio.run(em.run_once())
    t = db.fetchone("SELECT * FROM paper_trades WHERE ref = ?", ("early:" + m,))
    assert c["sold"] == 1 and t["status"] == "OPEN" and json.loads(t["done"]) == ["TP1"]
    dex.pairs = [make_pair(base_mint=m, market_cap=78_000.0, fdv=78_000.0)]
    asyncio.run(em.run_once())
    t = db.fetchone("SELECT * FROM paper_trades WHERE ref = ?", ("early:" + m,))
    assert t["status"] == "CLOSED" and t["exit_reason"] in ("TRAIL", "STOP")
    fills = json.loads(t["fills"])
    assert fills[-1]["why"]                                           # every sell says why


def test_calls_tab_shows_the_bots_exit(db):
    from northstar.dashboard import _attach_bot_exits
    s = Settings(_env_file=None)
    m = addr("cx")
    run = PT.active_run(db, s, "calls")
    PT._buy(db, run, "early:" + m, m, "CX", "runner", 100_000.0, None, time.time())
    row = dict(db.fetchone("SELECT * FROM paper_trades WHERE ref = ?", ("early:" + m,)))
    PT.apply_actions(db, row, run, 150_000.0, [EX.Action("TP1", 0.35, "tp")], time.time())
    row = dict(db.fetchone("SELECT * FROM paper_trades WHERE ref = ?", ("early:" + m,)))
    PT.apply_actions(db, row, run, 130_000.0, [EX.Action("TRAIL", EX.ALL, "trail")], time.time())
    rows = [{"id": "early:" + m, "mint": m}]
    _attach_bot_exits(db._conn, rows)
    b = rows[0]["bot_exit"]
    assert b["status"] == "CLOSED" and b["reason"] == "trailing stop"
    assert b["avg_x"] == pytest.approx(0.35 * 1.5 + 0.65 * 1.3, rel=1e-3)
    assert b["result_pct"] > 0


# ----------------------------------------------------------------------------- signing
def _wallet():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    k = Ed25519PrivateKey.generate()
    seed = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return seed + pub


def unsigned_tx(payer: bytes, versioned=True) -> bytes:
    other = bytes(range(32))
    msg = (b"\x80" if versioned else b"") + bytes([1, 0, 1]) + bytes([2]) + payer + other + bytes(32) + bytes([0])
    if versioned:
        msg += bytes([0])                                             # no address-table lookups
    return bytes([1]) + bytes(64) + msg


@pytest.mark.parametrize("versioned", [True, False])
def test_sign_transaction_puts_a_valid_signature_in_the_payer_slot(versioned):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    raw = _wallet()
    w = Wallet.from_text(base58.b58encode(raw).decode())
    tx = unsigned_tx(w.public, versioned)
    signed = w.sign_transaction(tx)
    assert len(signed) == len(tx) and signed[65:] == tx[65:]
    Ed25519PublicKey.from_public_bytes(w.public).verify(signed[1:65], tx[65:])     # raises if wrong
    assert w.fee_payer(tx) == w.address
    assert Wallet.from_text(json.dumps(list(raw))).address == w.address           # solana-keygen JSON format


def test_bad_keys_are_refused_without_echoing_them():
    raw = _wallet()
    with pytest.raises(KeyError_) as e:
        Wallet(raw[:32] + bytes(32))                                  # public half does not match
    assert "damaged" in str(e.value)
    secret = base58.b58encode(raw).decode()
    with pytest.raises(KeyError_) as e:
        Wallet.from_text(secret + "0OIl")                             # not base58
    assert secret not in str(e.value)
    other = Wallet(_wallet())
    with pytest.raises(ValueError):
        Wallet(raw).sign_transaction(unsigned_tx(other.public))        # not our transaction


# ----------------------------------------------------------------------------- live trading against a fake chain
class FakeChain:
    def __init__(self, wallet: Wallet, mint: str):
        self.w, self.mint, self.calls, self.sent = wallet, mint, [], []
        self.balance = 1.0
        self.next_delta = None

    async def call(self, method, params, cache_ttl=0):
        self.calls.append(method)
        if method == "getBalance":
            return {"value": int(self.balance * 1e9)}
        if method == "simulateTransaction":
            return {"value": {"err": None, "logs": []}}
        if method == "sendTransaction":
            self.sent.append(params[0])
            return f"SIG{len(self.sent)}"
        if method == "getSignatureStatuses":
            return {"value": [{"confirmationStatus": "confirmed", "err": None}]}
        if method == "getTransaction":
            sol, tok = self.next_delta
            pre_t = [{"owner": self.w.address, "mint": self.mint, "uiTokenAmount": {"uiAmount": 1000.0}}]
            post_t = [{"owner": self.w.address, "mint": self.mint, "uiTokenAmount": {"uiAmount": 1000.0 + tok}}]
            return {"transaction": {"message": {"accountKeys": [{"pubkey": self.w.address}]}},
                    "meta": {"preBalances": [10_000_000_000], "postBalances": [int(10_000_000_000 + sol * 1e9)],
                             "preTokenBalances": pre_t, "postTokenBalances": post_t}}
        raise AssertionError(method)


def _live(db, test_mode=True, **kw):
    w = Wallet(_wallet())
    m = addr("live")
    s = Settings(_env_file=None, trade_enabled=True, trade_test_mode=test_mode, **kw)
    chain = FakeChain(w, m)
    built = []

    async def http(url, data):
        built.append(dict(data))
        return 200, unsigned_tx(w.public)
    dex = Dex([make_pair(base_mint=m, market_cap=100_000.0, fdv=100_000.0)])
    t = LiveTrader(s, db, chain, dex, wallet=w, http=http, sleep=lambda _s: asyncio.sleep(0))
    return t, chain, built, dex, m


def _call(m, entry=100_000.0, kind="runner"):
    return [{"ref": f"early:{m}", "mint": m, "symbol": "LIVE", "kind": kind, "created_at": time.time(),
             "entry": entry, "current": entry, "plan": None, "events": [], "open": True}]


def test_test_mode_simulates_and_never_sends(db, monkeypatch):
    t, chain, built, dex, m = _live(db, test_mode=True)
    monkeypatch.setattr(PT, "calls_since", lambda *_a, **_k: _call(m))
    assert asyncio.run(t.run_once())["bought"] == 1
    assert "sendTransaction" not in chain.calls and "simulateTransaction" in chain.calls
    assert built[0]["action"] == "buy" and built[0]["denominatedInSol"] == "true" and built[0]["pool"] == "auto"
    row = db.fetchone("SELECT * FROM live_trades")
    assert row["test"] == 1 and row["status"] == "OPEN" and row["sol_in"] == pytest.approx(0.1)
    dex.pairs = [make_pair(base_mint=m, market_cap=75_000.0, fdv=75_000.0)]
    asyncio.run(t.manage())
    row = db.fetchone("SELECT * FROM live_trades")
    assert row["status"] == "CLOSED" and row["exit_reason"] == "STOP"
    assert built[-1]["action"] == "sell" and built[-1]["amount"] == "100%"
    assert t.today_result_sol() == pytest.approx(-0.025)
    assert "sendTransaction" not in chain.calls


def test_real_mode_reads_amounts_from_the_chain_and_sells_in_steps(db, monkeypatch):
    t, chain, built, dex, m = _live(db, test_mode=False)
    monkeypatch.setattr(PT, "calls_since", lambda *_a, **_k: _call(m))
    chain.next_delta = (-0.1012, 5000.0)                             # spent 0.1 SOL + fees, got 5,000 tokens
    assert asyncio.run(t.run_once())["bought"] == 1
    row = db.fetchone("SELECT * FROM live_trades")
    assert row["test"] == 0 and row["sol_in"] == pytest.approx(0.1012) and row["tokens"] == 5000.0
    dex.pairs = [make_pair(base_mint=m, market_cap=160_000.0, fdv=160_000.0)]
    chain.next_delta = (0.056, -1750.0)                              # TP1: 35% sold
    asyncio.run(t.manage())
    row = db.fetchone("SELECT * FROM live_trades")
    assert built[-1]["amount"] == "35%" and row["tokens_left"] == 3250.0 and row["status"] == "OPEN"
    dex.pairs = [make_pair(base_mint=m, market_cap=110_000.0, fdv=110_000.0)]
    chain.next_delta = (0.07, -3250.0)                               # trail: the rest
    asyncio.run(t.manage())
    row = db.fetchone("SELECT * FROM live_trades")
    assert row["status"] == "CLOSED" and row["exit_reason"] == "TRAIL" and built[-1]["amount"] == "100%"
    assert row["sol_out"] == pytest.approx(0.126)
    assert len(chain.sent) == 3


def test_limits_block_new_buys(db, monkeypatch):
    t, chain, built, dex, m = _live(db, test_mode=False, trade_max_open=1)
    monkeypatch.setattr(PT, "calls_since", lambda *_a, **_k: _call(m))
    chain.balance = 0.05                                             # under 0.1 + 0.03 reserve
    assert asyncio.run(t.run_once())["bought"] == 0
    assert "wallet balance" in db.fetchone("SELECT error FROM live_trades")["error"]
    # chasing: the coin ran 40% since the call
    db.execute("DELETE FROM live_trades")
    chain.balance = 1.0
    dex.pairs = [make_pair(base_mint=m, market_cap=140_000.0, fdv=140_000.0)]
    asyncio.run(t.run_once())
    assert "already up 40%" in db.fetchone("SELECT error FROM live_trades")["error"]
    # daily loss limit
    db.execute("DELETE FROM live_trades")
    now = time.time()
    db.execute("INSERT INTO live_trades(ref, mint, test, opened_at, sol_in, sol_out, status, closed_at) "
               "VALUES ('x', 'x', 0, ?, 1.0, 0.4, 'CLOSED', ?)", (now, now))
    assert "daily loss limit" in asyncio.run(t.can_buy())
    # trading off
    t.s.trade_enabled = False
    assert asyncio.run(t.run_once())["bought"] == 0 and asyncio.run(t.can_buy()) == "trading is off"


def test_only_followed_calls_are_bought_and_never_scalps(db, monkeypatch):
    t, chain, built, dex, m = _live(db, test_mode=True)
    monkeypatch.setattr(PT, "calls_since", lambda *_a, **_k: _call(m, kind="standard") + [
        {**_call(m)[0], "ref": "alert:9", "kind": "rug_scalp"}])
    assert asyncio.run(t.run_once())["bought"] == 0
    t.s.trade_follow = "all"
    assert asyncio.run(t.run_once())["bought"] == 1                  # the standard call, not the scalp


def test_a_transaction_for_another_wallet_is_refused(db, monkeypatch):
    t, chain, built, dex, m = _live(db, test_mode=False)
    other = Wallet(_wallet())

    async def http(url, data):
        return 200, unsigned_tx(other.public)
    t._http = http
    monkeypatch.setattr(PT, "calls_since", lambda *_a, **_k: _call(m))
    asyncio.run(t.run_once())
    row = db.fetchone("SELECT * FROM live_trades")
    assert row["status"] == "FAILED" and "another wallet" in row["error"] and chain.sent == []


def test_kill_switch_sells_everything(db, monkeypatch):
    t, chain, built, dex, m = _live(db, test_mode=True)
    monkeypatch.setattr(PT, "calls_since", lambda *_a, **_k: _call(m))
    asyncio.run(t.run_once())
    t.kill()
    asyncio.run(t.manage())
    row = db.fetchone("SELECT * FROM live_trades")
    assert row["status"] == "CLOSED" and row["exit_reason"] == "KILL"
    asyncio.run(t.manage())
    assert t.kill_requested is False                                  # nothing left: the switch resets


def test_the_key_is_never_shown_or_editable(db):
    from northstar import operator_settings as OS
    from northstar.dashboard import view_settings
    raw = _wallet()
    secret = base58.b58encode(raw).decode()
    s = Settings(_env_file=None, trade_wallet_private_key=secret)
    assert not OS.editable(s, "trade_wallet_private_key")
    groups = view_settings(db._conn, s)["groups"]
    shown = json.dumps(groups)
    assert secret not in shown and '"set"' in shown
    t = LiveTrader.from_settings(s, db, None, None)
    st = json.dumps(t.status())
    assert secret not in st and t.wallet.address in st


# ----------------------------------------------------------------------------- the queue and Discord
def test_runner_call_messages_are_edited_only_on_a_change(db, monkeypatch):
    import test_early_calls as TE
    s, pub, c = TE.caller(db)
    asyncio.run(c.consider(TE.outcome(TE.curve_report())))
    db.execute("UPDATE early_calls SET created_at=?, last_edit_at=? WHERE mint=?", (TE.NOW, TE.NOW, TE.MINT))
    st, tr = TE.tracker(db, pub, monkeypatch, cap_sol=200.0)         # same price as the call
    st.embed_edit_min_seconds = 300
    asyncio.run(tr.run_once(TE.NOW + 30))
    n = len(pub.edits)
    asyncio.run(tr.run_once(TE.NOW + 60))
    assert len(pub.edits) == n, "nothing changed: no edit"


def test_today_counts_open_test_positions_at_their_value(db, monkeypatch):
    t, chain, built, dex, m = _live(db, test_mode=True)
    monkeypatch.setattr(PT, "calls_since", lambda *_a, **_k: _call(m))
    asyncio.run(t.run_once())
    dex.pairs = [make_pair(base_mint=m, market_cap=120_000.0, fdv=120_000.0)]
    asyncio.run(t.manage())
    assert t.today_result_sol() == pytest.approx(0.02)               # open, worth 1.2x: +0.02 SOL


class SlowChain(FakeChain):
    """Sends fine, but the transaction is never seen as confirmed (network trouble)."""
    async def call(self, method, params, cache_ttl=0):
        if method == "getSignatureStatuses":
            self.calls.append(method)
            return {"value": [None]}
        return await super().call(method, params, cache_ttl)


def test_a_buy_sent_but_not_confirmed_is_still_managed(db, monkeypatch):
    t, chain, built, dex, m = _live(db, test_mode=False)
    slow = SlowChain(t.wallet, m)
    t.rpc = slow
    clock = [time.time()]
    t._now = lambda: clock[0]

    async def sleep(_s):
        clock[0] += 5
    t._sleep = sleep
    monkeypatch.setattr(PT, "calls_since", lambda *_a, **_k: _call(m))
    asyncio.run(t.run_once(clock[0]))
    row = db.fetchone("SELECT * FROM live_trades")
    assert row["status"] == "OPEN" and "not confirmed" in row["error"] and len(slow.sent) == 1
    # the exit engine sells it like any other position ("100%" of whatever the wallet holds)
    t.rpc = chain
    chain.next_delta = (0.07, -4000.0)
    dex.pairs = [make_pair(base_mint=m, market_cap=70_000.0, fdv=70_000.0)]
    asyncio.run(t.manage())
    assert db.fetchone("SELECT status FROM live_trades")["status"] == "CLOSED" and built[-1]["amount"] == "100%"


def test_real_positions_stay_real_and_test_positions_stay_simulated(db, monkeypatch):
    t, chain, built, dex, m = _live(db, test_mode=False)
    monkeypatch.setattr(PT, "calls_since", lambda *_a, **_k: _call(m))
    chain.next_delta = (-0.1, 5000.0)
    asyncio.run(t.run_once())
    t.s.trade_test_mode = True                                       # switched back to test mode
    dex.pairs = [make_pair(base_mint=m, market_cap=75_000.0, fdv=75_000.0)]
    chain.next_delta = (0.07, -5000.0)
    asyncio.run(t.manage())
    assert db.fetchone("SELECT status FROM live_trades")["status"] == "CLOSED"
    assert len(chain.sent) == 2, "the real position was sold for real despite test mode"


def test_one_position_per_coin(db, monkeypatch):
    t, chain, built, dex, m = _live(db, test_mode=True)
    monkeypatch.setattr(PT, "calls_since", lambda *_a, **_k: _call(m) + [{**_call(m)[0], "ref": "alert:7"}])
    assert asyncio.run(t.run_once())["bought"] == 1
    assert "already held" in db.fetchone("SELECT error FROM live_trades WHERE ref = 'alert:7'")["error"]
