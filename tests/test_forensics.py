"""Wallet forensics: exchange hubs never create clusters on their own, 2-hop funding is caught,
fresh-wallet supply is measured, and the bubblemap has the right nodes and links. SYNTHETIC."""
from __future__ import annotations

import asyncio
import time

from conftest import addr
from northstar.analysis import wallets as W
from northstar.config import Settings

S = Settings(_env_file=None)
NOW = 1_800_000_000.0
OLD = NOW - 90 * 86400
FRESH = NOW - 3 * 3600


def t(funder=None, oldest=OLD, busy=False):
    return {"oldest_seen": oldest, "last_seen": NOW, "history_complete": not busy, "tx_count_lower_bound":
            1000 if busy else 40, "busy": busy, "funder": funder}


def clusters(traced, funders=None, holdings=None, creator=None):
    holdings = holdings or {w: 50_000_000 for w in traced}
    return W.build_clusters([], [], traced, holdings, 1_000_000_000, creator, S, NOW, funders=funders or {})


def test_exchange_hub_alone_is_not_a_cluster():
    hub = addr("binance-hot")
    r = clusters({addr("h1"): t(hub), addr("h2"): t(hub), addr("h3"): t(hub)}, {hub: t(busy=True)})
    assert r.max_cluster_supply_pct == 0.0 and r.hub_funders == [hub]
    assert all(c["strength"] == "WEAK" for c in r.clusters)


def test_ordinary_shared_funder_is_a_strong_cluster():
    f = addr("dev-wallet")
    r = clusters({addr("h1"): t(f), addr("h2"): t(f)}, {f: t()})
    assert r.max_cluster_supply_pct == 10.0 and r.clusters[0]["strength"] == "STRONG"


def test_two_hop_funding_is_caught():
    a, b, dev = addr("mid-a"), addr("mid-b"), addr("dev")
    r = clusters({addr("h1"): t(a), addr("h2"): t(b)}, {a: t(dev), b: t(dev), dev: t()})
    assert r.max_cluster_supply_pct == 10.0 and r.two_hop_links == 2
    ids = {n["id"]: n for n in r.bubbles}
    assert a in ids and b in ids and dev in ids                      # the chain is drawn
    assert {(l["a"], l["b"]) for l in r.links} >= {(addr("h1"), a), (addr("h2"), b), (a, dev), (b, dev)}
    assert ids[addr("h1")]["cluster"] == ids[addr("h2")]["cluster"] == 0


def test_two_hop_through_a_hub_is_ignored():
    a, b, hub = addr("mid-a"), addr("mid-b"), addr("exchange")
    r = clusters({addr("h1"): t(a), addr("h2"): t(b)}, {a: t(hub), b: t(hub), hub: t(busy=True)})
    assert r.max_cluster_supply_pct == 0.0


def test_creator_funding_two_hops_away_is_confirmed():
    creator, mid = addr("creator"), addr("mid")
    r = clusters({addr("h1"): t(mid)}, {mid: t(creator)}, creator=creator)
    assert any(c["strength"] == "CONFIRMED" for c in r.clusters)


def test_wallet_funding_another_holder_links_them():
    h1 = addr("h1")
    r = clusters({h1: t(), addr("h2"): t(h1)})
    assert r.max_cluster_supply_pct == 10.0


def test_fresh_wallet_supply_and_bubble_fields():
    r = clusters({addr("h1"): t(oldest=FRESH), addr("h2"): t()},
                 holdings={addr("h1"): 120_000_000, addr("h2"): 30_000_000})
    assert r.fresh_supply_pct == 12.0
    n = {b["id"]: b for b in r.bubbles}
    assert n[addr("h1")]["fresh"] and n[addr("h1")]["age_h"] == 3.0 and n[addr("h1")]["pct"] == 12.0
    assert not n[addr("h2")]["fresh"]


def test_trace_funders_marks_hubs_and_follows_shared_grand_funders():
    hub, a, b, dev = addr("hub"), addr("a"), addr("b"), addr("dev")
    sigs = {hub: 1000, a: 3, b: 3, dev: 5}
    first_tx_funder = {a: dev, b: dev}

    class Rpc:
        async def get_signatures(self, wallet, limit=1000):
            return [{"signature": f"{wallet}-{i}", "blockTime": OLD + i} for i in range(sigs.get(wallet, 2))][::-1]

        async def get_transaction(self, sig):
            w = sig.rsplit("-", 1)[0]
            src = first_tx_funder.get(w)
            return {"transaction": {"message": {"instructions": [
                {"parsed": {"type": "transfer", "info": {"source": src, "destination": w}}}] if src else []}}, "meta": {}}
    traced = {addr("h1"): t(a), addr("h2"): t(b), addr("h3"): t(hub)}
    out = asyncio.run(W.trace_funders(Rpc(), traced))
    assert out[hub]["busy"] and not out[a]["busy"] and out[a]["funder"] == dev and dev in out
