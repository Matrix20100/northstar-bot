from __future__ import annotations
import json
import urllib.request
from pathlib import Path

from northstar.dashboard import DashboardServer
from northstar.db import Database


def test_dashboard_serves_ui_and_health(tmp_path: Path):
    db = Database(tmp_path / "northstar.db")
    db.open(); db.close()
    srv = DashboardServer(tmp_path / "northstar.db", "127.0.0.1", 0)
    srv.start_background()
    port = srv.server.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as r:
            assert r.status == 200
            assert b"NORTHSTAR" in r.read()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as r:
            assert json.loads(r.read())["ok"] is True
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/overview", timeout=3) as r:
            payload = json.loads(r.read())
            assert payload["tracked"] == 0
            assert payload["candidates"] == []
    finally:
        srv.server.shutdown(); srv.server.server_close()


def test_dashboard_token_detail_uses_saved_report(tmp_path: Path):
    db = Database(tmp_path / "northstar.db")
    db.open()
    mint = "So11111111111111111111111111111111111111112"
    now = 1_800_000_000.0
    db.execute("INSERT INTO candidates(mint,state,discovered_at,symbol,name,last_score,last_risk,last_confidence,reasons,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (mint, "WATCHLIST", now, "SOL", "Solana", 55, "INCOMPLETE", "LOW", json.dumps(["Missing evidence"]), now))
    report = {"identity": {"symbol": "SOL", "name": "Solana"}, "flags": [{"severity": "HIGH", "message": "Example flag"}], "pool": {"price_usd": 123}}
    db.execute("INSERT INTO research_snapshots(mint,ts,action,score,risk,confidence,report) VALUES(?,?,?,?,?,?,?)",
               (mint, now, "WATCHLIST", 55, "INCOMPLETE", "LOW", json.dumps(report)))
    db.close()
    srv = DashboardServer(tmp_path / "northstar.db", "127.0.0.1", 0)
    srv.start_background(); port = srv.server.server_address[1]
    try:
        url = f"http://127.0.0.1:{port}/api/token?mint={mint}"
        with urllib.request.urlopen(url, timeout=3) as r:
            payload = json.loads(r.read())
        assert payload["state"] == "WATCHLIST"
        assert payload["report"]["flags"][0]["message"] == "Example flag"
        assert payload["reasons"] == ["Missing evidence"]
    finally:
        srv.server.shutdown(); srv.server.server_close()
