"""Creator-balance units, free social checks and early-watch channel routing. SYNTHETIC data only."""
from __future__ import annotations

import json
import types

import httpx
import pytest

from conftest import CREATOR, MINT, MemoryPublisher, addr, make_pair
from northstar.alerts import AlertService
from northstar.analysis import socials as SOC
from northstar.analysis.holders import Holder, analyze_holders, creator_balance_pct
from northstar.analysis.narrative import analyze_narrative
from northstar.config import Settings
from northstar.models import DataStatus
from northstar.providers.socials import FreeSocials, _safe_https, parse_tg_count


# ---------------------------------------------------------------- creator balance (RugCheck raw units)
def test_creator_balance_is_converted_from_raw_units():
    rc = {"creatorBalance": 7_264_970_000_000, "token": {"supply": 10 ** 15, "decimals": 6}}
    assert creator_balance_pct(rc, 1e9) == pytest.approx(0.726, abs=1e-3)   # was shown as 726496.9%
    no_raw_supply = {"creatorBalance": 50_000_000_000_000, "token": {"decimals": 6}}
    assert creator_balance_pct(no_raw_supply, 1e9) == pytest.approx(5.0)
    assert creator_balance_pct({"creatorBalance": 10 ** 20, "token": {"supply": 10 ** 15}}, 1e9) is None  # >100%
    assert creator_balance_pct({"creatorBalance": 5, "token": {}}, None) is None
    assert creator_balance_pct({"creatorBalance": True, "token": {"supply": 10}}, 1) is None


def test_holder_gate_uses_sane_creator_pct(settings):
    holders = [Holder(addr(f"acct{i}"), addr(f"own{i}"), 1e7, 1.0) for i in range(10)]
    rc = {"creatorBalance": 7_264_970_000_000, "token": {"supply": 10 ** 15, "decimals": 6}}
    ha = analyze_holders(holders, DataStatus.VERIFIED, "solana-rpc", settings, pair_address=None,
                         creator=CREATOR, rc=rc, supply_ui=1e9)
    assert ha.creator_pct == pytest.approx(0.726, abs=1e-3)
    assert not any(g.name == "creator_holdings" and g.status.value == "FAIL" for g in ha.gates)


# ---------------------------------------------------------------- link classification
@pytest.mark.parametrize("raw,kind,key", [
    ("https://x.com/BananaCatSol", "x_account", "bananacatsol"),
    ("twitter.com/BananaCatSol/", "x_account", "bananacatsol"),
    ("@BananaCatSol", "x_account", "bananacatsol"),
    ("https://x.com/i/communities/1890000000000000000", "x_community", "1890000000000000000"),
    ("https://x.com/elonmusk/status/1890000000000000001", "x_post", "1890000000000000001"),
    ("https://t.me/bananacatportal", "telegram", "bananacatportal"),
    ("https://www.bananacat.fun/about", "website", "bananacat.fun"),
    ({"type": "twitter", "url": "https://x.com/Other_1"}, "x_account", "other_1"),
])
def test_classify(raw, kind, key):
    link = SOC.classify(raw)
    assert (link.kind, link.key) == (kind, key)


@pytest.mark.parametrize("raw", ["https://x.com/search?q=x", "https://x.com/home", "", "https://pump.fun/coin/x",
                                 "https://ipfs.io/ipfs/abc"])
def test_classify_ignores_non_identity_links(raw):
    assert SOC.classify(raw) is None


def test_collect_reads_pump_metadata_and_dedupes():
    meta = {"twitter": "https://x.com/BananaCatSol", "telegram": "https://t.me/bananacatportal",
            "website": "https://bananacat.fun"}
    links = SOC.collect([{"type": "twitter", "url": "https://twitter.com/bananacatsol"}], None, meta)
    assert sorted(l.kind for l in links) == ["telegram", "website", "x_account"]


# ---------------------------------------------------------------- telegram count parsing / fetcher
def test_parse_tg_count_formats():
    assert parse_tg_count('<div class="tgme_page_extra">9 390 877 subscribers</div>') == 9_390_877
    assert parse_tg_count("1,234 members, 56 online") == 1234
    assert parse_tg_count("1 203 members") == 1203
    assert parse_tg_count("If you have Telegram, you can contact @x") is None


def test_only_public_https_urls_are_fetched():
    assert _safe_https("ipfs://QmAbc") == "https://ipfs.io/ipfs/QmAbc"
    for bad in ("http://example.com/m.json", "https://127.0.0.1/x", "https://localhost/x", "file:///etc/passwd",
                "https://intranet/x"):
        assert _safe_https(bad) is None


async def test_free_socials_fetches_metadata_and_telegram():
    def handler(req: httpx.Request):
        if req.url.host == "t.me":
            return httpx.Response(200, text="<div>2 540 members, 80 online</div>")
        return httpx.Response(200, json={"twitter": "https://x.com/BananaCatSol"})
    fs = FreeSocials(min_interval=0, transport=httpx.MockTransport(handler))
    try:
        assert (await fs.metadata("https://ipfs.io/ipfs/Qm1"))["twitter"].endswith("BananaCatSol")
        assert await fs.telegram_members("bananacatportal") == 2540
        assert await fs.telegram_members("bad handle!") is None
    finally:
        await fs.close()


# ---------------------------------------------------------------- reuse + scoring
def test_recycled_links_detected_and_score_nothing(db):
    links = SOC.collect(None, None, {"twitter": "https://x.com/serialdev", "telegram": "https://t.me/serialtg1"})
    db.record_social_links(addr("other1"), links)
    db.record_social_links(addr("other2"), links)
    ev = SOC.SocialEvidence(links=links, reuse=db.social_link_reuse(MINT, links, since=0),
                            telegram_members=5000, telegram_handle="serialtg1")
    assert set(ev.recycled) == {"x_account:serialdev", "telegram:serialtg1"}
    pts, good, bad, items = SOC.score_attention(ev)
    assert (pts, items) == (0.0, 0) and "recycled socials" in bad[0]


def test_free_attention_points_are_capped_and_weak():
    links = SOC.collect(None, None, {"twitter": "https://x.com/own", "telegram": "https://t.me/owntg123"})
    ev = SOC.SocialEvidence(links=links, telegram_members=2500, telegram_handle="owntg123")
    pts, good, _, items = SOC.score_attention(ev)
    assert pts == 4.0 and items == 1
    assert any("can be botted" in g for g in good)
    assert SOC.score_attention(SOC.SocialEvidence(links=links, telegram_members=40))[0] == 1.0


def test_narrative_uses_free_socials_and_flags_recycling():
    pair = make_pair()
    good = SOC.SocialEvidence(links=SOC.collect(None, None, {"telegram": "https://t.me/owntg123"}),
                              telegram_members=900, telegram_handle="owntg123")
    nr = analyze_narrative(pair, None, [], [], 20.0, social=good)
    assert nr.evidence_items >= 1 and nr.free_socials["telegram_members"] == 900
    base = analyze_narrative(pair, None, [], [], 20.0)
    assert nr.component.points > base.component.points
    bad = SOC.SocialEvidence(links=good.links, reuse={"telegram:owntg123": 3})
    nr_bad = analyze_narrative(pair, None, [], [], 20.0, social=bad)
    assert any(f.code == "RECYCLED_SOCIALS" for f in nr_bad.flags)


def test_unreachable_narrative_threshold_is_reported():
    s = Settings(_env_file=None, min_narrative_score=18, enable_free_socials=False)
    assert any("cannot be reached" in p for p in s.config_problems())
    assert not any("cannot be reached" in p for p in
                   Settings(_env_file=None, min_narrative_score=18).config_problems())


# ---------------------------------------------------------------- early-watch routing
def early_out():
    report = {"mint": MINT, "identity": {"name": "Banana Cat", "symbol": "BCAT"},
              "pool": {"bonding_curve": addr("curve")},
              "pre_migration": {"qualified_early_watch": True, "market_cap_sol_current": 150.0,
                                "opportunity_score": 90.0, "market_cap_change_pct": 50.0}}
    return types.SimpleNamespace(mint=MINT, report=report)


@pytest.mark.parametrize("mode,free,expect_channel,expect_status", [
    ("free", 777, "none", "UNTRACKED"),   # v3: early watches never post to free ("free" now means off)
    ("vip", 777, None, "SENT"),
    ("off", 777, "none", "UNTRACKED"),
    ("free", None, "none", "UNTRACKED"),
])
async def test_early_watch_channel_routing(tmp_path, db, mode, free, expect_channel, expect_status):
    s = Settings(_env_file=None, data_dir=str(tmp_path), dry_run=False, discord_enabled=False,
                 enable_pumpportal=False, early_watch_channel=mode,
                 discord_free_channel_id=free)
    pub = MemoryPublisher()
    await AlertService(s, db, pub).publish_early_watch(early_out())
    row = db.fetchone("SELECT status FROM early_signals WHERE mint=?", (MINT,))
    assert row["status"] == expect_status
    if expect_channel == "none":
        assert pub.sent == []
    else:
        assert pub.sent[0][0] == expect_channel
        assert "Experimental" in pub.sent[0][1]["footer"]["text"]


def test_early_watch_channel_setting_validated():
    assert Settings(_env_file=None, early_watch_channel=" VIP ").early_watch_channel == "vip"
    with pytest.raises(Exception):
        Settings(_env_file=None, early_watch_channel="everywhere")
