"""Rug-scalp calls, peak-since-callout and the CoinGecko client. SYNTHETIC data only."""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from conftest import MINT, make_pair
from test_alerts_monitoring import published
from northstar import scalp
from northstar import trade_plan as TP
from northstar.config import Settings
from northstar.discord_bot.embeds import callout_embed, money, peak_text, updated_callout
from northstar.monitor import Monitor
from northstar.net import ProviderClient
from northstar.providers.coingecko import CoinGeckoOnchain, peak_after

S = Settings(_env_file=None)
SURGE = {"line": "⚡ Surge: $30,050 volume in 5 min (2.7x the hour's pace), 277 buys / 146 sells"}


def rug_report(*failed, unknown=(), acc=None, bs=None):
    gates = [{"name": n, "status": "FAIL", "reason": f"{n} failed"} for n in failed]
    gates += [{"name": n, "status": "UNKNOWN", "reason": "unknown"} for n in unknown]
    gates += [{"name": "mint_authority", "status": "PASS"}, {"name": "freeze_authority", "status": "PASS"}]
    return {"mint": MINT, "gates": gates, "market_structure": {"volume_acceleration": acc, "buy_sell_ratio_h1": bs}}


def test_scalp_only_for_tradeable_rugs_with_momentum():
    pair = make_pair(market_cap=120_000.0, liquidity_usd=30_000.0)
    v = scalp.check(rug_report("holder_concentration", "launch_bundle"), pair, S, SURGE)
    assert v and v["failed_checks"] == ["holder_concentration", "launch_bundle"]
    assert any("few wallets hold" in r for r in v["rug_reasons"]) and v["momentum"] == SURGE["line"]
    # traps are never posted, even with momentum
    for trap in ("freeze_authority", "mint_authority", "lp_lock", "liquidity_removal", "token_extensions",
                 "rugcheck_rugged"):
        assert scalp.check(rug_report("holder_concentration", trap), pair, S, SURGE) is None, trap
    assert scalp.check(rug_report("holder_concentration", unknown=("lp_lock",)), pair, S, SURGE) is None
    # no momentum, too small, or not a rug -> no scalp
    assert scalp.check(rug_report("holder_concentration"), pair, S, None) is None
    assert scalp.check(rug_report("holder_concentration"), make_pair(market_cap=20_000.0), S, SURGE) is None
    assert scalp.check(rug_report(), pair, S, SURGE) is None
    # strong 1h momentum counts only when a surge is not required
    loose = Settings(_env_file=None, scalp_require_surge=False)
    assert scalp.check(rug_report("holder_concentration", acc=3.0, bs=1.6), pair, loose, None)
    assert scalp.check(rug_report("holder_concentration", acc=0.8, bs=1.6), pair, loose, None) is None


def test_scalp_embed_is_unmistakable():
    report = {"mint": MINT, "identity": {"symbol": "RUG", "name": "Rug"}, "market": {"market_cap": {"value": 120_000}},
              "call_type": "RUG_SCALP", "surge": SURGE,
              "scalp": {"rug_reasons": ["a few wallets hold a big share of supply — top-10 own 48%"],
                        "momentum": SURGE["line"], "failed_checks": ["holder_concentration"]}}
    plan = TP.plan_lines(TP.build_plan(120_000, S, "scalp"), money)
    e = callout_embed(report, plan=plan)
    assert e["title"] == "☠️ RUG SCALP — $RUG"
    assert "THIS IS A RUG — MOMENTUM IS STRONG, SCALP ONLY" in e["description"]
    names = [f["name"] for f in e["fields"]]
    assert "☠️ Why this is a rug" in names and "📋 Scalp plan" in names and "Why Northstar selected it" not in names
    text = json.dumps(e)
    assert "-15%" in text and "trade done" in text and "not 1.15x within 10 min" in text


def test_scalp_plan_sells_everything_and_stops():
    p = TP.build_plan(100_000, S, "scalp")
    assert TP.sells_everything(p)
    ev = TP.evaluate(p, 1.7, 1.7, 60, set(), money)
    assert [k for k, _ in ev] == ["TP1", "TP2"] and "trade done" in ev[-1][1]
    assert TP.evaluate(p, 1.0, 1.7, 600, {"TP1", "TP2"}, money) == []          # nothing left to manage
    assert [k for k, _ in TP.evaluate(p, 0.84, 1.0, 60, set(), money)] == ["STOP"]
    assert [k for k, _ in TP.evaluate(p, 1.05, 1.1, 11 * 60, set(), money)] == ["TIME"]


async def test_engine_posts_scalp_instead_of_rejecting(settings, db, monkeypatch):
    from conftest import MemoryPublisher
    from northstar.engine import Engine
    from northstar.models import Action, Confidence, RiskLevel
    from northstar.research import Providers, ResearchOutcome

    class Dex:
        pairs = [make_pair(market_cap=120_000.0, liquidity_usd=30_000.0)]

        async def token_pairs(self, mint):
            return self.pairs
    db.upsert_discovery(MINT, "pumpportal_migration", 3.0, "Rug", "RUG")
    pub = MemoryPublisher()
    eng = Engine(settings, db, Providers(dex=Dex()), pub)
    report = rug_report("holder_concentration")
    report.update({"identity": {"symbol": "RUG", "name": "Rug"}, "surge": SURGE,
                   "market": {"market_cap": {"value": 120_000.0}}})
    out = ResearchOutcome(MINT, Action.REJECT, report, "h", Dex.pairs[0], reasons=["gate failed: holder_concentration"],
                          risk=RiskLevel.CRITICAL, confidence=Confidence.LOW)

    async def fake_research(mint, cand=None, force=False):
        return out
    monkeypatch.setattr(eng.researcher, "research", fake_research)
    await eng.process(MINT)
    assert pub.sent and pub.sent[0][1]["title"].startswith("☠️ RUG SCALP")
    a = db.fetchone("SELECT call_type, plan_json FROM alerts WHERE mint=?", (MINT,))
    assert a["call_type"] == "RUG_SCALP" and json.loads(a["plan_json"])["stop"] == 0.85
    assert not db.fetchone("SELECT 1 FROM free_channel_posts"), "scalps never go to the free channel"


async def test_scalp_disabled_means_plain_reject(settings, db, monkeypatch):
    settings.scalp_enabled = False
    assert scalp.check(rug_report("holder_concentration"), make_pair(), settings, SURGE) is None


# ---------------------------------------------------------------- peak since callout
async def test_embed_shows_and_updates_peak(settings, db):
    settings.embed_edit_min_seconds = 0
    aid, pub, p, out = await published(settings, db)
    assert any(f["name"] == "Peak since callout" for f in pub.sent[0][1]["fields"])
    mon = Monitor(settings, db, pub, p.dex, None)
    p.dex.pairs = [make_pair(price_usd=out.pair.price_usd * 1.8, market_cap=out.pair.market_cap * 1.8)]
    await mon.run_once()
    p.dex.pairs = [make_pair(price_usd=out.pair.price_usd * 1.2, market_cap=out.pair.market_cap * 1.2)]
    await mon.run_once()
    a = db.get_alert(aid)
    assert abs(a["peak_mcap"] - out.pair.market_cap * 1.8) < 1 and a["peak_at"]
    peak = next(f for f in pub.edits[-1][1]["fields"] if f["name"] == "Peak since callout")["value"]
    assert peak.startswith(money(out.pair.market_cap * 1.8)) and "1.80x" in peak
    since = next(f for f in pub.edits[-1][1]["fields"] if f["name"] == "Since callout")["value"]
    assert "1.20x" in since


async def test_candle_high_raises_peak_between_checks(settings, db):
    aid, pub, p, out = await published(settings, db)

    class CG:
        async def candles(self, pool, start, end=None, urgent=False):
            t = time.time()
            return [(t - 60, 1, out.pair.price_usd * 2.6, 1, out.pair.price_usd * 1.1, 1000.0)]
    mon = Monitor(settings, db, pub, p.dex, None, CG())
    await mon.run_once()                                   # schedules the candle check
    await asyncio.gather(*list(mon._candle_tasks.values()))
    await mon.run_once()                                   # applies it
    a = db.get_alert(aid)
    assert abs(a["max_multiple"] - 2.6) < 1e-6 and abs(a["peak_mcap"] - out.pair.market_cap * 2.6) < 1


def test_peak_text_for_old_alerts_and_updated_embed_inserts_field():
    alert = {"entry_mcap": 100_000.0, "max_multiple": 2.0, "sent_at": 1000.0, "peak_at": 1600.0,
             "current_mcap": 150_000.0, "warnings": "[]", "last_update_at": 2000.0}
    assert peak_text(alert) == "$200.0K · 2.00x · 10 min after call"
    old = {"fields": [{"name": "Called at market cap", "value": "x"}, {"name": "Since callout", "value": "x"}]}
    names = [f["name"] for f in updated_callout(old, alert)["fields"]]
    assert names == ["Called at market cap", "Since callout", "Peak since callout"]


# ---------------------------------------------------------------- CoinGecko client
def _client(handler, name):
    return ProviderClient(name, "https://x.invalid/api/v3", 600, 0, 5.0, 1, transport=httpx.MockTransport(handler))


async def test_coingecko_candles_and_trending_with_fallback():
    calls = []

    def keyed(req):
        calls.append(("keyed", req.url.path))
        return httpx.Response(429, json={})

    def keyless(req):
        calls.append(("keyless", req.url.path))
        if "ohlcv" in req.url.path:
            now = int(time.time())
            return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": [
                [now - 60, 1.0, 1.5, 0.9, 1.2, 10.0], [now - 120, 1.0, 1.1, 0.9, 1.0, 5.0], ["bad"]]}}})
        return httpx.Response(200, json={"data": [{"attributes": {
            "address": "POOL1", "name": "X / SOL", "market_cap_usd": "120000", "fdv_usd": "130000",
            "reserve_in_usd": "40000", "volume_usd": {"m5": "9000", "h1": "20000"},
            "transactions": {"m5": {"buys": 50, "sells": 20, "buyers": 40}},
            "price_change_percentage": {"m5": "12", "h1": "40"}},
            "relationships": {"base_token": {"data": {"id": "solana_MINT1"}}}}]})
    cg = CoinGeckoOnchain(_client(keyed, "coingecko_onchain"), _client(keyless, "geckoterminal"))
    candles = await cg.candles("POOL1", time.time() - 600, urgent=True)
    assert len(candles) == 2 and candles[0][0] < candles[1][0]
    assert calls[0][0] == "keyed" and any(c[0] == "keyless" for c in calls)      # urgent: key first, then fallback
    assert peak_after(candles, 1.0, time.time() - 600)[0] == 1.5
    rows = await cg.trending_pools()
    assert rows[0]["mint"] == "MINT1" and rows[0]["market_cap"] == 120000.0 and rows[0]["buys_m5"] == 50
    assert calls[-1][0] == "keyless"                                                # background: keyless first


def test_coingecko_plan_setting_validated():
    assert Settings(_env_file=None, coingecko_plan=" PRO ").coingecko_plan == "pro"
    import pytest
    with pytest.raises(Exception):
        Settings(_env_file=None, coingecko_plan="gold")
    assert "secret-key" in Settings(_env_file=None, coingecko_api_key="secret-key").secrets()
