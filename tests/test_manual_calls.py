"""Researching a token on request, and calling it by hand from the dashboard.

Research never posts. A call goes through the same publisher as every automatic call, so the
pre-flight market check, the cooldown, the VIP-first ordering and the tracking all still apply.
A hard veto is shown and must be overridden on purpose; the post then says so.
"""
from __future__ import annotations

from conftest import MINT, MemoryPublisher, strong_providers
from northstar.engine import Engine


def engine(settings, db, providers=None):
    pub = MemoryPublisher()
    return Engine(settings, db, providers or strong_providers(), pub), pub


async def test_research_on_request_never_posts(settings, db):
    eng, pub = engine(settings, db)
    res = await eng.manual_research(MINT)
    assert res["ok"], res
    tok = res["token"]
    assert tok["mint"] == MINT and tok["report"]["unified"]["decision"]
    assert tok["manual"]["callable"] is True
    assert pub.sent == [], "a deep look is not a call"


async def test_research_rejects_something_that_is_not_a_mint(settings, db):
    eng, _ = engine(settings, db)
    res = await eng.manual_research("not-a-mint")
    assert res["ok"] is False and "mint" in res["error"]


async def test_a_call_needs_fresh_research_first(settings, db):
    eng, pub = engine(settings, db)
    res = await eng.manual_call(MINT, note="looks good")
    assert res["ok"] is False and "Research this token first" in res["error"]
    assert pub.sent == []


async def test_a_manual_call_posts_through_the_normal_publisher(settings, db):
    settings.discord_free_channel_id = 777
    eng, pub = engine(settings, db)
    assert (await eng.manual_research(MINT))["ok"]
    res = await eng.manual_call(MINT, note="Clean chart, real buyers.", free_copy=False)
    assert res["ok"], res
    assert len(pub.sent) == 1
    embed = pub.sent[0][1]
    assert embed["title"].startswith("📣 MANUAL CALL")
    assert "Called by hand from the dashboard" in embed["description"]
    assert "Northstar's own read" in embed["description"]
    notes = [f for f in embed["fields"] if f["name"] == "📝 Caller's note"]
    assert notes and "Clean chart" in notes[0]["value"]
    assert "Manual call." in embed["footer"]["text"]
    alert = db.fetchone("SELECT call_type, status FROM alerts WHERE mint=?", (MINT,))
    assert alert["call_type"] == "MANUAL"
    assert db.fetchall("SELECT alert_id FROM free_channel_posts") == [], "no free copy was asked for"


async def test_a_manual_call_can_ask_for_the_free_channel_copy(settings, db):
    settings.discord_free_channel_id = 777
    eng, _ = engine(settings, db)
    await eng.manual_research(MINT)
    res = await eng.manual_call(MINT, free_copy=True)
    assert res["ok"] and res["free_copy"] is True
    rows = db.fetchall("SELECT channel_id FROM free_channel_posts")
    assert rows and rows[0]["channel_id"] == 777, "scheduled after the usual VIP head start"


async def test_one_research_buys_one_call_and_the_cooldown_still_holds(settings, db):
    eng, pub = engine(settings, db)
    await eng.manual_research(MINT)
    assert (await eng.manual_call(MINT))["ok"]
    again = await eng.manual_call(MINT)
    assert again["ok"] is False and "Research this token first" in again["error"]
    await eng.manual_research(MINT)
    third = await eng.manual_call(MINT)
    assert third["ok"] is False and "cooldown" in third["error"]
    assert len(pub.sent) == 1


async def test_a_hard_veto_must_be_overridden_on_purpose(settings, db):
    eng, pub = engine(settings, db)
    assert (await eng.manual_research(MINT))["ok"]
    out, ts = eng._manual[MINT]
    out.report["unified"]["hard_vetoes"] = ["transfers run custom code (can block selling)"]

    refused = await eng.manual_call(MINT)
    assert refused["ok"] is False and refused.get("needs_override")
    assert "custom code" in refused["vetoes"][0]
    assert pub.sent == []

    res = await eng.manual_call(MINT, override=True)
    assert res["ok"], res
    embed = pub.sent[0][1]
    veto = [f for f in embed["fields"] if f["name"] == "⚠️ Posted over a hard veto"]
    assert veto and "custom code" in veto[0]["value"], "readers see that the veto was overridden"


async def test_the_preflight_check_can_still_stop_a_manual_call(settings, db):
    p = strong_providers()
    eng, pub = engine(settings, db, p)
    assert (await eng.manual_research(MINT))["ok"]

    async def gone(mint):
        return []                                    # the pool no longer answers
    p.dex.token_pairs = gone
    res = await eng.manual_call(MINT)
    assert res["ok"] is False and "no longer returns a price" in res["error"]
    assert pub.sent == []


# the dashboard endpoints ---------------------------------------------------------------------------
async def test_dashboard_research_and_call_reach_the_running_bot(settings, db):
    """The panel's HTTP thread hands work to the bot's event loop and waits for the answer."""
    import asyncio
    import json

    from test_dashboard import serve

    from northstar import dashboard as D

    settings.dashboard_password = None
    eng, pub = engine(settings, db)
    D.attach(eng, asyncio.get_running_loop())
    try:
        srv, c = serve(db, "pw-ok", settings)
        assert (await asyncio.to_thread(c.req, "/api/research", {"mint": MINT}))[0] == 401, "login first"
        assert (await asyncio.to_thread(c.req, "/api/login", {"password": "pw-ok"}))[0] == 200

        st, body = await asyncio.to_thread(c.req, "/api/research", {"mint": MINT})
        res = json.loads(body)
        assert st == 200 and res["ok"] and res["token"]["manual"]["callable"]
        assert pub.sent == []

        st, body = await asyncio.to_thread(c.req, "/api/call", {"mint": MINT, "note": "from the panel",
                                                                "free_copy": False})
        res = json.loads(body)
        assert st == 200 and res["ok"], res
        assert len(pub.sent) == 1 and pub.sent[0][1]["title"].startswith("📣 MANUAL CALL")

        st, body = await asyncio.to_thread(c.req, "/api/call", {"mint": MINT})
        assert st == 422 and "Research this token first" in json.loads(body)["error"]

        st, _ = await asyncio.to_thread(
            c.req, "/api/call", {"mint": MINT}, {"Origin": "https://evil.example"})
        assert st == 403, "another site cannot make the panel post"
        srv.server.shutdown()
    finally:
        D.LIVE.update(engine=None, settings=None, loop=None)


def test_dashboard_says_so_when_the_bot_is_not_running(db):
    import json

    from test_dashboard import serve

    from northstar import dashboard as D

    D.LIVE.update(engine=None, settings=None, loop=None)
    srv, c = serve(db, "pw-ok")
    assert c.req("/api/login", {"password": "pw-ok"})[0] == 200
    st, body = c.req("/api/research", {"mint": MINT})
    assert st == 503 and "not running" in json.loads(body)["error"]
    srv.server.shutdown()
