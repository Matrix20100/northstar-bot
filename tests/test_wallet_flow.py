"""v3.5.0: tracked wallets' live trades -> money flow -> research now. SYNTHETIC data only."""
from __future__ import annotations

import asyncio
import json
import time
import types

from conftest import MINT, MemoryPublisher, addr, make_pair, strong_providers
from northstar import wallet_flow as WF
from northstar.analysis.holdings import TrackedWallet
from northstar.config import Settings

W1, W2, W3, BOT, STRANGER = (addr(x) for x in ("w1", "w2", "w3", "botw", "stranger"))
WALLETS = [TrackedWallet(W1, "alpha"), TrackedWallet(W2, "beta"), TrackedWallet(W3, "gamma"),
           TrackedWallet(BOT, "spammer")]


def trade(wallet, side="buy", sol=1.0, mint=MINT):
    return {"traderPublicKey": wallet, "txType": side, "solAmount": sol, "mint": mint, "marketCapSol": 200.0}


def test_two_tracked_wallets_buying_the_same_coin_triggers_research(db):
    wf = WF.WalletFlow(Settings(_env_file=None), db, WALLETS)
    t0 = time.time()
    assert wf.observe(trade(STRANGER), t0) is None                  # not a tracked wallet: ignored
    assert wf.observe(trade(W1, sol=1.5), t0) is None               # one buyer: recorded, no trigger yet
    sig = wf.observe(trade(W2, sol=2.0), t0 + 60)
    assert sig and sig["mint"] == MINT and sig["buyers"] == 2 and sig["buy_sol"] == 3.5
    assert "alpha" in sig["why"] and "beta" in sig["why"]
    assert wf.observe(trade(W3), t0 + 120) is None                  # same coin within 10 min: no repeat trigger
    wf.observe(trade(W1, "sell", 0.5), t0 + 130)
    f = wf.flow(MINT, t0 + 140)
    assert (f["buyers"], f["buy_sol"], f["sell_sol"]) == (3, 4.5, 0.5)
    top = wf.top(t0 + 140)
    assert top[0]["mint"] == MINT and top[0]["buyers"] == 3 and top[0]["net_sol"] == 4.0
    # outside the window it no longer counts
    assert wf.flow(MINT, t0 + 3600)["buyers"] == 0


def test_one_big_buy_triggers_research(db):
    wf = WF.WalletFlow(Settings(_env_file=None, wallet_flow_big_buy_sol=5.0), db, WALLETS)
    sig = wf.observe(trade(W1, sol=8.0, mint=addr("whale")))
    assert sig and "8.0 SOL in one trade" in sig["why"]


def test_bot_wallets_are_muted_and_the_daily_budget_pauses_the_stream(db):
    s = Settings(_env_file=None, wallet_bot_trades_per_hour=5, wallet_watch_max_events_day=20)
    wf = WF.WalletFlow(s, db, WALLETS)
    t0 = time.time()
    for i in range(6):
        wf.observe(trade(BOT, mint=addr(f"spam{i}")), t0 + i)
    assert BOT in wf.muted and BOT not in wf.keys()
    assert db.fetchone("SELECT COUNT(*) n FROM wallet_trades WHERE wallet=?", (BOT,))["n"] == 5
    for i in range(20):
        wf.observe(trade(W1, mint=addr(f"x{i}")), t0 + 10 + i)
    assert wf.paused and wf.status()["paused"]
    # a new UTC day unmutes and unpauses
    assert wf._roll_day(t0 + 2 * 86400) and not wf.paused and wf.muted == set() and wf.events_today == 0


async def test_stream_subscribes_tracked_wallets_and_drops_muted_ones():
    from northstar.providers.pumpportal import PumpPortalStream
    keys = [addr(f"k{i}") for i in range(450)]
    sent = []

    class WS:
        async def send(self, m):
            sent.append(json.loads(m))

    want = list(keys)
    st = PumpPortalStream(lambda *a: None, "key-not-real", account_keys=lambda: want)
    st._ws = WS()
    assert await st.sync_accounts() == 450
    subs = [m for m in sent if m["method"] == "subscribeAccountTrade"]
    assert len(subs) == 3 and sum(len(m["keys"]) for m in subs) == 450 and max(len(m["keys"]) for m in subs) <= 200
    sent.clear()
    want.remove(keys[0])                                            # muted as a bot
    assert await st.sync_accounts() == 449
    assert sent == [{"method": "unsubscribeAccountTrade", "keys": [keys[0]]}]
    no_key = PumpPortalStream(lambda *a: None, None, account_keys=lambda: keys)
    no_key._ws = WS()
    sent.clear()
    assert await no_key.sync_accounts() == 0 and sent == []         # metered: never without a key


async def test_a_wallet_signal_researches_the_coin_now(settings, db, tmp_path):
    from northstar.engine import Engine
    path = tmp_path / "tracked.json"
    path.write_text(json.dumps([{"trackedWalletAddress": W1, "name": "alpha"},
                                {"trackedWalletAddress": W2, "name": "beta"}]))
    settings.tracked_wallets_file = str(path)
    p = strong_providers()

    async def tokens_batch(mints):
        return [make_pair(base_mint=m, dex_id="pumpfun", market_cap=40_000.0, fdv=40_000.0) for m in mints]
    p.dex.tokens_batch = tokens_batch
    eng = Engine(settings, db, p, MemoryPublisher())
    assert eng.wallet_flow is not None and len(eng.wallet_flow.names) == 2
    researched = []

    async def fake_research(mint):
        researched.append(mint)
    eng._radar_research = fake_research
    coin = addr("flowcoin")
    await eng.discovery.on_pumpportal("trade", trade(W1, mint=coin))
    await eng.discovery.on_pumpportal("trade", trade(W2, mint=coin))
    await asyncio.sleep(0.01)
    assert researched == [coin] and eng.stats["wallet_signals"] == 1
    from northstar.runner import radar_metrics
    assert radar_metrics(db, coin)["market_cap"] == 40_000             # the runner check can judge it
    assert "wallet_flow" in db.get_candidate(coin)["sources"]
    # a dropped coin is not brought back
    gone = addr("dropped")
    db.upsert_discovery(gone, "pumpportal_new", 1.0)
    db.set_state(gone, "DROPPED")
    for w in (W1, W2):
        await eng.discovery.on_pumpportal("trade", trade(w, mint=gone))
    await asyncio.sleep(0.01)
    assert gone not in researched


def test_attention_counts_wallets_buying_now(db):
    from northstar.attention import Attention
    wf = WF.WalletFlow(Settings(_env_file=None), db, WALLETS)
    for w in (W1, W2, W3):
        wf.observe(trade(w, sol=1.0))
    a = Attention(Settings(_env_file=None))
    a.flow = wf
    res = a.score(MINT, "Coin", "COIN")
    assert res["parts"]["flow"] == 45 and res["reasons"][0].startswith("3 tracked wallets bought")


def test_dashboard_money_flow_card(db):
    from test_dashboard import serve
    wf = WF.WalletFlow(Settings(_env_file=None), db, WALLETS)
    wf.observe(trade(W1, sol=2.0))
    srv, c = serve(db, "pw")
    c.req("/api/login", {"password": "pw"})
    d = json.loads(c.req("/api/flow")[1])
    assert d["top"][0]["mint"] == MINT and d["top"][0]["buy_sol"] == 2.0 and d["api_key"] is False
    srv.server.shutdown()
