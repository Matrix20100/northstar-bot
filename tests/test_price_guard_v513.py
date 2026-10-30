"""v5.1.3: verified prices. Operator: "the bot lies to me, it posts milestones that didn't happen and lies about the
current price of a coin." $Pumpers (called at $16.6K, real high $20.5K, still on its curve) was posted as "hit 25x,
peak 27.10x ($450.7K)" from a DexScreener pool. No network: Jupiter, the chain and DexScreener are faked."""
from __future__ import annotations

import asyncio
import json
import time

from conftest import make_pair
from northstar import early_calls as EC
from northstar import paper as PT
from northstar import price_guard as PG
from northstar.config import Settings

import test_early_calls as TE

MINT = TE.MINT
NOW = TE.NOW


class Jup:
    """A fake Jupiter: a 0.05 SOL buy returns tokens so the implied market cap is `mcap` (1B supply, 6 decimals,
    SOL at $200). None = Jupiter cannot be asked; "NO_ROUTE" = nothing can fill a buy."""
    def __init__(self, mcap):
        self.mcap, self.calls = mcap, 0

    async def quote(self, mint, amount_raw, out_mint=None, slippage_bps=500):
        self.calls += 1
        if self.mcap is None:
            return None
        if self.mcap == "NO_ROUTE":
            return {"no_route": True}
        price = self.mcap / 1e9                               # USD per token
        from northstar.providers.solana_rpc import USDC_MINT
        usd = amount_raw / 1e6 if mint == USDC_MINT else amount_raw / 1e9 * 200.0   # v5.2.1: USDC quotes
        tokens = usd / price
        return {"no_route": False, "out_amount": tokens * 1e6}


class Rpc(TE.FakeRpc):
    async def get_token_supply(self, mint):
        return {"amount": str(10 ** 15), "decimals": 6, "uiAmount": 1e9}


def guard(jup_mcap):
    return PG.PriceGuard(Rpc(0), Jup(jup_mcap), TE.FakeSol())


# ----------------------------------------------------------------------------- which pools are believable
def test_only_believable_pools_are_used():
    real = make_pair(base_mint=MINT, dex_id="pumpswap", market_cap=250_000.0, liquidity_usd=40_000.0)
    thin = make_pair(base_mint=MINT, dex_id="meteora", market_cap=450_700.0, liquidity_usd=900.0)
    odd_quote = make_pair(base_mint=MINT, dex_id="raydium", market_cap=300_000.0, liquidity_usd=50_000.0,
                          quote_mint="SomeOtherToken1111111111111111111111111111")
    curve = make_pair(base_mint=MINT, dex_id="pumpfun", market_cap=20_000.0, liquidity_usd=None)
    assert PG.trusted(real) and PG.trusted(curve)
    assert not PG.trusted(thin) and not PG.trusted(odd_quote)
    assert not PG.trusted(real, mint="AnotherMint")
    assert PT.pick_pair([thin, real], MINT) is real


# ----------------------------------------------------------------------------- the check itself
def test_a_jump_jupiter_agrees_with_is_used():
    g = guard(440_000.0)
    assert asyncio.run(g.verify(MINT, 450_700.0, 16_600.0)) == 450_700.0
    assert g.stats["confirmed"] == 1


def test_a_jump_jupiter_disagrees_with_is_replaced_by_the_real_price():
    g = guard(20_500.0)
    assert asyncio.run(g.verify(MINT, 450_700.0, 16_600.0)) == 20_500.0
    assert g.stats["corrected"] == 1


def test_a_jump_nothing_can_buy_or_that_cannot_be_checked_is_not_used():
    assert asyncio.run(guard("NO_ROUTE").verify(MINT, 450_700.0, 16_600.0)) is None
    g = guard(None)                                           # Jupiter down right now
    assert asyncio.run(g.verify(MINT, 450_700.0, 16_600.0)) is None
    assert asyncio.run(g.verify(MINT, 450_700.0, 16_600.0)) is None   # still unchecked: still not used


def test_small_moves_are_not_checked():
    j = Jup(1.0)
    g = PG.PriceGuard(Rpc(0), j, TE.FakeSol())
    assert asyncio.run(g.verify(MINT, 30_000.0, 20_000.0)) == 30_000.0 and j.calls == 0


def test_a_fall_needs_a_second_read_without_jupiter():
    g = PG.PriceGuard(Rpc(0), None, TE.FakeSol())
    assert asyncio.run(g.verify(MINT, 3_000.0, 20_000.0)) is None
    assert asyncio.run(g.verify(MINT, 3_100.0, 20_000.0)) == 3_100.0
    # and a fall nothing can buy into is real
    assert asyncio.run(guard("NO_ROUTE").verify(MINT, 3_000.0, 20_000.0)) == 3_000.0


# ----------------------------------------------------------------------------- the posts
def _called(db):
    s, pub, c = TE.caller(db)
    asyncio.run(c.consider(TE.outcome(TE.curve_report())))     # called at $40K
    db.execute("UPDATE early_calls SET created_at=? WHERE mint=?", (NOW, MINT))
    return pub


def _tracker(db, pub, monkeypatch, pairs, jup):
    from northstar.research import Researcher
    monkeypatch.setattr(Researcher, "_parse_pump_curve_account", staticmethod(lambda acc: None))   # unreadable
    s = Settings(_env_file=None, runner_calls_enabled=False, discord_channel_id=111, discord_free_channel_id=222,
                 embed_edit_min_seconds=0)

    class Dex:
        async def tokens_batch(self, mints):
            return list(pairs)
    g = PG.PriceGuard(Rpc(0), Jup(jup), TE.FakeSol())
    return EC.EarlyCallTracker(s, db, pub, Rpc(0), Dex(), TE.FakeSol(), guard=g)


def _milestones(pub, n0):
    return [e for _ch, e in pub.sent[n0:] if "hit" in json.dumps(e, default=str).lower()
            and "since the call" in json.dumps(e, default=str).lower()]


def test_pumpers_a_thin_pool_cannot_post_a_25x(db, monkeypatch):
    pub = _called(db)
    n0 = len(pub.sent)
    fake = make_pair(base_mint=MINT, dex_id="meteora", market_cap=1_084_000.0, liquidity_usd=2_000.0)   # "27x"
    tr = _tracker(db, pub, monkeypatch, [fake], jup=49_000.0)
    asyncio.run(tr.run_once(NOW + 60))
    r = db.fetchone("SELECT * FROM early_calls WHERE mint=?", (MINT,))
    assert r["peak_mcap"] < 60_000 and r["phase"] == "CURVE" and json.loads(r["milestones"]) == []
    assert not _milestones(pub, n0)


def test_pumpers_a_deep_but_wrong_pool_is_corrected_by_a_real_buy(db, monkeypatch):
    pub = _called(db)
    n0 = len(pub.sent)
    wrong = make_pair(base_mint=MINT, dex_id="raydium", market_cap=1_084_000.0, liquidity_usd=60_000.0)
    tr = _tracker(db, pub, monkeypatch, [wrong], jup=49_000.0)
    asyncio.run(tr.run_once(NOW + 60))
    r = db.fetchone("SELECT * FROM early_calls WHERE mint=?", (MINT,))
    assert r["current_mcap"] == 49_000.0 and r["peak_mcap"] < 60_000 and json.loads(r["milestones"]) == []
    assert not _milestones(pub, n0)


def test_a_real_run_still_posts_its_milestone(db, monkeypatch):
    pub = _called(db)
    n0 = len(pub.sent)
    real = make_pair(base_mint=MINT, dex_id="pumpswap", market_cap=130_000.0, liquidity_usd=30_000.0)
    tr = _tracker(db, pub, monkeypatch, [real], jup=125_000.0)
    asyncio.run(tr.run_once(NOW + 60))
    r = db.fetchone("SELECT * FROM early_calls WHERE mint=?", (MINT,))
    assert r["current_mcap"] == 130_000.0 and 3.0 in json.loads(r["milestones"])
    assert _milestones(pub, n0)


# ----------------------------------------------------------------------------- paper sells on checked prices only
def test_paper_does_not_take_profit_on_an_unconfirmed_jump(db):
    s = Settings(_env_file=None)
    run = PT.active_run(db, s, "calls")
    PT._buy(db, run, "early:x", MINT, "PUMPERS", "runner", 16_600.0, None, time.time() - 60)

    class Dex:
        async def tokens_batch(self, mints):
            return [make_pair(base_mint=MINT, dex_id="raydium", market_cap=300_000.0, liquidity_usd=60_000.0)]
    asyncio.run(PT.ExitManager(s, db, Dex(), guard=guard("NO_ROUTE")).run_once())
    t = db.fetchone("SELECT * FROM paper_trades")
    assert t["status"] == "OPEN" and t["last_mcap"] == 16_600.0 and "TP1" not in t["done"]
