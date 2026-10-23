"""v4.4.0: posts the Uxento Vision Forwarder (tools/vision-forwarder) sends to Discord are read by the
bot's existing tracker-feed reader (social_feed.py): contract address, X account and ticker.

The payloads are built by the extension's own code (node tools/vision-forwarder/test/dump_messages.js)
from synthetic items; skipped when node is not installed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from northstar import social_feed as SF

ROOT = Path(__file__).resolve().parents[1]
MINT = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"


@pytest.fixture(scope="module")
def payloads():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run([node, str(ROOT / "tools/vision-forwarder/test/dump_messages.js")], capture_output=True,
                         text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def as_discord_message(payload: dict, mid: str) -> dict:
    """What Discord's REST API returns for a webhook message: the same content and embeds."""
    return {"id": mid, "content": payload.get("content") or "", "embeds": payload.get("embeds") or [],
            "webhook_id": "123", "author": {"bot": True, "username": payload.get("username")}}


def test_tweet_with_a_contract_address(payloads):
    p = SF.parse(as_discord_message(payloads[0], "1"))
    assert p["mints"] == [MINT]
    assert p["account"] == "alice"
    assert "DOGWIF" in p["tickers"]


def test_fomo_trade(payloads):
    p = SF.parse(as_discord_message(payloads[1], "2"))
    assert p["mints"] == [MINT]          # the CA is what matters; a Fomo caller has no X link


def test_follow_update_has_no_coin_but_is_kept(payloads):
    p = SF.parse(as_discord_message(payloads[2], "3"))
    assert p["mints"] == [] and p["account"] == "dave" and p["text"].strip()


def test_forwarded_contract_address_is_researched(db, settings, payloads):
    """The feed poll turns the forwarded CA into a research signal, exactly as for a J7 / uxento post."""
    import asyncio
    settings.twitter_feed_channel_ids = "555"
    msgs = [as_discord_message(payloads[0], "101")]

    async def fetch(url, headers):
        return 200, (msgs if "after=" in url else [{"id": "100", "content": "older", "embeds": []}])

    feed = SF.TwitterFeed(settings, db, "token-not-real", fetch=fetch)
    asyncio.run(feed.poll(1000.0))                       # first read: start from now
    signals = asyncio.run(feed.poll(1001.0))
    assert [s["mint"] for s in signals] == [MINT]
    assert signals[0]["why"] == "@alice posted its contract address"


def test_a_bundle_of_posts_is_read_as_separate_posts(db, settings, payloads):
    """v4.5.0: the forwarder sends up to 5 posts per message; each embed is its own post."""
    import asyncio
    settings.twitter_feed_channel_ids = "555"
    bundle = {"id": "201", "content": "", "embeds": [p["embeds"][0] for p in payloads], "webhook_id": "123"}
    assert [mid for mid, _ in SF.split(bundle)] == ["201:0", "201:1", "201:2"]

    async def fetch(url, headers):
        return 200, ([bundle] if "after=" in url else [{"id": "200", "content": "older", "embeds": []}])

    feed = SF.TwitterFeed(settings, db, "token-not-real", fetch=fetch)
    asyncio.run(feed.poll(1000.0))
    signals = asyncio.run(feed.poll(1001.0))
    rows = db.fetchall("SELECT msg_id, account FROM social_mentions ORDER BY msg_id")
    assert [(r["msg_id"], r["account"]) for r in rows] == [("201:0", "alice"), ("201:1", None), ("201:2", "dave")]
    assert [s["mint"] for s in signals] == [MINT]            # the same CA twice in a bundle is researched once
    # after a restart the feed resumes after the bundle, not before it
    again = SF.TwitterFeed(settings, db, "token-not-real", fetch=fetch)
    assert again.last_id["555"] == "201"
