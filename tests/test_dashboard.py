"""Control panel + self-test: locked by default, login and lockout, no secrets ever served,
every view works on a real-shaped database, and the self-test reports honestly. SYNTHETIC data."""
from __future__ import annotations

import asyncio
import http.cookiejar
import json
import socket
import sqlite3
import time
import urllib.error
import urllib.request

import httpx
import pytest

from conftest import MINT, addr
from northstar import learning as L
from northstar import selftest as ST
from northstar.config import Settings
from northstar.dashboard import DashboardServer, view_settings


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def seeded(db):
    L.init_schema(db)
    now = time.time()
    db.upsert_discovery(MINT, "pumpportal_migration", 3.0, "Mock", "MOCK")
    db.execute("INSERT INTO research_snapshots(mint, ts, action, score, report) VALUES (?,?,?,?,?)",
               (MINT, now - 60, "WATCHLIST", 55.0, json.dumps({"mint": MINT, "gates": [{"name": "lp_lock", "status": "PASS"}]})))
    db.execute("UPDATE candidates SET last_research_at=?, last_action='WATCHLIST', last_score=55, reasons=? WHERE mint=?",
               (now - 60, json.dumps(["score 55 < 60"]), MINT))
    db.execute("INSERT INTO radar_obs(mint, ts, pool, price, metrics, fired) VALUES (?,?,?,?,?,1)",
               (MINT, now - 120, addr("pool"), 1.0, json.dumps({"spike": 4.2, "vol_m5": 9000, "buys": 40, "sells": 9})))
    return db


class Client:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def req(self, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            resp = self.op.open(r, timeout=20)
            return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()


def serve(db, password, settings=None):
    port = free_port()
    s = settings or Settings(_env_file=None, discord_token="SECRET-DISCORD-123", helius_api_key="SECRET-HELIUS-456",
                             coingecko_api_key="SECRET-CG-789", dashboard_password=password or None)
    srv = DashboardServer(str(db.path), "127.0.0.1", port, settings=s, password=password)
    srv.start_background()
    return srv, Client(port)


def test_locked_until_a_password_is_set(seeded):
    srv, c = serve(seeded, "")
    assert c.req("/")[0] == 200
    assert json.loads(c.req("/api/session")[1])["configured"] is False
    assert c.req("/api/status")[0] == 401
    assert c.req("/api/login", {"password": ""})[0] == 401
    srv.server.shutdown()


def test_login_views_and_no_secret_ever_served(seeded):
    srv, c = serve(seeded, "pw-correct")
    assert c.req("/api/status")[0] == 401
    assert c.req("/api/login", {"password": "nope"})[0] == 401
    assert c.req("/api/login", {"password": "pw-correct"})[0] == 200
    body = b""
    for ep in ("/api/status", "/api/watching", "/api/calls", "/api/decisions", "/api/learning", "/api/settings",
               f"/api/token?mint={MINT}", "/api/selftest"):
        st, b = c.req(ep)
        assert st == 200, (ep, b[:300])
        body += b
    for secret in (b"SECRET-DISCORD", b"SECRET-HELIUS", b"SECRET-CG", b"pw-correct"):
        assert secret not in body
    w = json.loads(c.req("/api/watching")[1])
    # v4.5.0: a scheduled (not yet watched) coin is counted in the research queue, not listed as watched
    assert w["queued"] == 1 and w["watched"] == 0 and w["surges"][0]["metrics"]["spike"] == 4.2
    d = json.loads(c.req("/api/decisions")[1])["decisions"][0]
    assert d["last_action"] == "WATCHLIST" and d["reasons"] == ["score 55 < 60"]
    assert c.req("/api/logout", {})[0] == 200 and c.req("/api/status")[0] == 401
    srv.server.shutdown()


def test_cross_site_posts_refused_and_brute_force_locked_out(seeded):
    srv, c = serve(seeded, "pw-correct")
    assert c.req("/api/login", {"password": "pw-correct"}, {"Origin": "https://evil.example"})[0] == 403
    for _ in range(5):
        assert c.req("/api/login", {"password": "wrong"})[0] == 401
    assert c.req("/api/login", {"password": "pw-correct"})[0] == 429         # even the right one, for 10 min
    srv.server.shutdown()


def test_settings_view_hides_secrets_and_shows_sources(seeded):
    s = Settings(_env_file=None, discord_token="SECRET-DISCORD-123", plan_tp1=2.5)
    seeded.execute("INSERT INTO learned_settings(key, value, updated_at) VALUES ('plan_stop', 0.6, ?)", (time.time(),))
    c = sqlite3.connect(str(seeded.path))
    c.row_factory = sqlite3.Row
    v = view_settings(c, s)
    flat = {x["name"]: x for g in v["groups"] for x in g["settings"]}
    assert flat["discord_token"]["value"] == "set" and flat["discord_token"]["secret"]
    assert flat["helius_api_key"]["value"] == "not set"
    assert flat["plan_tp1"]["source"] == ".env" and flat["plan_stop"]["source"] == "learned"
    assert "SECRET" not in json.dumps(v, default=str)


def test_selftest_runner_one_at_a_time():
    r = ST.Runner()
    assert r.start(lambda: (time.sleep(0.3), [ST._r("x", "a", ST.PASS, "ok")])[1])
    assert not r.start(lambda: [])                                           # already running
    for _ in range(50):
        if not r.running:
            break
        time.sleep(0.05)
    st = r.state()
    assert st["summary"]["overall"] == ST.PASS and st["results"][0]["name"] == "a"


def test_discord_check_reports_login_and_channels():
    def handler(req):
        if req.url.path.endswith("/users/@me"):
            return httpx.Response(200, json={"username": "Northstar"})
        if req.url.path.endswith("/channels/111"):
            return httpx.Response(200, json={"name": "vip-calls"})
        return httpx.Response(403, json={})
    s = Settings(_env_file=None, discord_token="t", discord_channel_id=111, discord_free_channel_id=222)
    res = {r["name"]: r for r in asyncio.run(ST.check_discord(s, httpx.MockTransport(handler)))}
    assert res["Bot login"]["status"] == ST.PASS and "Northstar" in res["Bot login"]["detail"]
    assert res["VIP channel"]["detail"] == "#vip-calls"
    assert res["Free channel"]["status"] == ST.FAIL and "permissions" in res["Free channel"]["detail"]
    bad = asyncio.run(ST.check_discord(s, httpx.MockTransport(lambda r: httpx.Response(401))))
    assert bad[0]["status"] == ST.FAIL


def test_provider_checks_flag_failures_and_empty_answers():
    class Dex:
        async def latest_profiles(self):
            return [1, 2]

    class Rpc:
        async def call(self, m, p):
            raise RuntimeError("rpc down")

    class CG:
        async def trending_pools(self, d):
            return []
    p = type("P", (), {"dex": Dex(), "rpc": Rpc(), "coingecko": CG(), "rugcheck": None})()
    res = {r["name"]: r for r in asyncio.run(ST.check_providers(p))}
    assert res["DexScreener"]["status"] == ST.PASS and res["Solana RPC"]["status"] == ST.FAIL
    assert res["CoinGecko / GeckoTerminal"]["status"] == ST.WARN


def test_engine_and_learning_checks(seeded):
    class Eng:
        stats = {"started_at": time.time() - 7200, "researched": 50, "auto_post": 1, "errors": 0}
        queue = asyncio.Queue()
        radar = type("R", (), {"trending": {"a": {}}})()
    res = {r["name"]: r for r in ST.check_engine(Eng(), str(seeded.path))}
    assert res["Running"]["status"] == ST.PASS and res["Research loop"]["status"] == ST.PASS
    assert ST.check_engine(None, str(seeded.path))[0]["status"] == ST.SKIP
    lr = {r["name"]: r for r in ST.check_learning(Settings(_env_file=None), str(seeded.path))}
    assert lr["Brain"]["status"] == ST.WARN and "not trained" in lr["Brain"]["detail"]


def test_code_check_runs_pytest_without_the_bots_settings(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text(
        "import os\n"
        "def test_ok():\n    assert True\n"
        "def test_no_bot_env():\n    assert not [k for k in os.environ if k.startswith('NORTHSTAR_')]\n")
    monkeypatch.setattr(ST, "ROOT", tmp_path)
    monkeypatch.setenv("NORTHSTAR_DISCORD_TOKEN", "real-token-must-not-leak")
    r = ST.check_code(timeout=120)[0]
    assert r["status"] == ST.PASS and "2 passed" in r["detail"]
    (tmp_path / "tests" / "test_bad.py").write_text("def test_bad():\n    assert 1 == 2\n")
    r = ST.check_code(timeout=120)[0]
    assert r["status"] == ST.FAIL and "1 failed" in r["detail"] and "test_bad" in r["detail"]


def test_health_endpoint_and_token_detail_use_saved_report(seeded):
    srv, c = serve(seeded, "pw")
    st, b = c.req("/healthz")
    assert st == 200 and json.loads(b)["ok"] is True
    c.req("/api/login", {"password": "pw"})
    tok = json.loads(c.req(f"/api/token?mint={MINT}")[1])
    assert tok["report"]["gates"][0]["name"] == "lp_lock" and tok["snapshot"]["action"] == "WATCHLIST"
    assert c.req("/api/token?mint=" + addr("unknown"))[0] == 404
    assert b"NORTHSTAR" in c.req("/")[1]
    srv.server.shutdown()
