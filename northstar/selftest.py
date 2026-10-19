"""Self-test: checks every part of Northstar and reports PASS / WARN / FAIL per check.

Run from the dashboard (Self-test tab) or `python main.py selftest`. It never posts to Discord and
never changes settings. Groups:
  config     settings problems, secrets present, plan validity
  discord    bot token valid, each configured channel reachable
  data       DexScreener, RugCheck, Solana RPC, CoinGecko/GeckoTerminal answer right now
  database   integrity, size, table sizes
  engine     research/discovery/radar loops are actually running (live bot only)
  learning   brain trained/trusted, outcomes being evaluated, recent changes
  code       the full offline test suite (pytest), with the bot's settings stripped from the
             environment so tests can never touch real keys or channels
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx

ROOT = Path(__file__).resolve().parents[1]
PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


def _r(group: str, name: str, status: str, detail: str, ms: Optional[float] = None) -> dict:
    return {"group": group, "name": name, "status": status, "detail": detail,
            "ms": None if ms is None else round(ms)}


async def _timed(coro):
    t = time.monotonic()
    try:
        return await coro, None, (time.monotonic() - t) * 1000
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {str(e)[:160]}", (time.monotonic() - t) * 1000


# ----------------------------------------------------------------------------- config
def check_config(s) -> list[dict]:
    out = []
    probs = s.config_problems()
    out.append(_r("config", "Settings", FAIL if probs else PASS, "; ".join(probs) if probs else "no problems found"))
    for label, val in (("Discord token", s.discord_token), ("Helius key", s.helius_api_key),
                       ("CoinGecko key", s.coingecko_api_key), ("PumpPortal key", s.pumpportal_api_key)):
        has = bool(val and val.get_secret_value())
        need = label == "Discord token" and s.discord_enabled and not s.dry_run
        out.append(_r("config", label, PASS if has else (FAIL if need else WARN),
                      "set" if has else ("missing — required to post" if need else "not set (optional)")))
    out.append(_r("config", "Posting mode", WARN if s.dry_run or not s.discord_enabled else PASS,
                  "DRY RUN — researching but never posting" if s.dry_run else
                  ("Discord disabled" if not s.discord_enabled else "live: posts calls to Discord")))
    return out


# ----------------------------------------------------------------------------- discord
async def check_discord(s, transport: Optional[httpx.AsyncBaseTransport] = None) -> list[dict]:
    tok = s.discord_token.get_secret_value() if s.discord_token else ""
    if not tok:
        return [_r("discord", "Bot login", SKIP, "no Discord token set")]
    out = []
    async with httpx.AsyncClient(base_url="https://discord.com/api/v10", timeout=10, transport=transport,
                                 headers={"Authorization": f"Bot {tok}"}) as c:
        r, err, ms = await _timed(c.get("/users/@me"))
        if err or r.status_code != 200:
            return [_r("discord", "Bot login", FAIL, err or f"HTTP {r.status_code} — token rejected", ms)]
        me = r.json()
        out.append(_r("discord", "Bot login", PASS, f"logged in as {me.get('username')}", ms))
        for label, cid in (("VIP channel", s.discord_channel_id), ("Free channel", s.discord_free_channel_id),
                           ("Warning channel", s.discord_warning_channel_id)):
            if not cid:
                out.append(_r("discord", label, SKIP if label != "VIP channel" else FAIL, "not set"))
                continue
            r, err, ms = await _timed(c.get(f"/channels/{cid}"))
            if err or r.status_code != 200:
                why = {403: "the bot can't see this channel (permissions)", 404: "channel not found"}.get(
                    getattr(r, "status_code", 0), err or f"HTTP {r.status_code}")
                out.append(_r("discord", label, FAIL, why, ms))
            else:
                out.append(_r("discord", label, PASS, f"#{r.json().get('name')}", ms))
    return out


# ----------------------------------------------------------------------------- data providers
async def check_providers(p) -> list[dict]:
    checks = []
    if getattr(p, "dex", None):
        checks.append(("DexScreener", p.dex.latest_profiles(), lambda v: f"{len(v or [])} new token profiles"))
    if getattr(p, "rugcheck", None):
        checks.append(("RugCheck", p.rugcheck.summary("So11111111111111111111111111111111111111112"),
                       lambda v: "answered"))
    if getattr(p, "rpc", None):
        checks.append(("Solana RPC", p.rpc.call("getSlot", []), lambda v: f"slot {v:,}" if isinstance(v, int) else "answered"))
    if getattr(p, "coingecko", None):
        checks.append(("CoinGecko / GeckoTerminal", p.coingecko.trending_pools("5m"),
                       lambda v: f"{len(v or [])} trending Solana pools"))
    out = []
    results = await asyncio.gather(*(_timed(asyncio.wait_for(coro, 30)) for _n, coro, _f in checks))
    for (name, _coro, fmt), (v, err, ms) in zip(checks, results):
        empty = not err and name.startswith("CoinGecko") and not v
        status = FAIL if err else (WARN if ms > 8000 or empty else PASS)
        detail = err or ("answered with no data — the source may be rate-limiting or down" if empty else fmt(v))
        out.append(_r("data", name, status, detail + (" (slow)" if not err and ms > 8000 else ""), ms))
    if not checks:
        out.append(_r("data", "Data sources", FAIL, "no data providers enabled"))
    return out


# ----------------------------------------------------------------------------- AI judge / events
def check_ai_and_events(s, db_path: str, now: Optional[float] = None) -> list[dict]:
    now = now or time.time()
    out = []
    if not getattr(s, "early_call_enabled", False):
        return [_r("narrative", "Early narrative calls", SKIP, "turned off")]
    has_key = bool(s.ai_secret()) or s.ai_provider == "local"
    from .ai_judge import PRESETS
    model = s.ai_model or PRESETS.get(s.ai_provider, ("", "", ""))[1]
    out.append(_r("narrative", "AI judge", PASS if (has_key and s.ai_enabled) else WARN,
                  f"{s.ai_provider} · model {model} · caps {s.ai_max_calls_per_hour}/h, "
                  f"${s.ai_daily_budget_usd:.2f}/day" if has_key and s.ai_enabled else
                  f"no NORTHSTAR_AI_KEY for provider '{s.ai_provider}' — early narrative calls are off"))
    feeds = [n for n, on in (("news", s.events_news), ("trending searches", s.events_trends),
                             ("Wikipedia", s.events_wikipedia)) if on]
    out.append(_r("narrative", "Event feeds", PASS if feeds else WARN,
                  "launch waves + " + ", ".join(feeds) if feeds else "launch waves only (news feeds off)"))
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        day0 = now - (now % 86400)
        a = c.execute("SELECT COUNT(*) n, COALESCE(SUM(cost_usd), 0) c FROM ai_judgements WHERE ts >= ?",
                      (day0,)).fetchone()
        calls = c.execute("SELECT COUNT(*) n, SUM(status IN ('SENT','DRY_RUN')) open FROM early_calls "
                          "WHERE created_at >= ?", (now - 86400,)).fetchone()
        c.close()
        spent, budget = float(a["c"] or 0), float(s.ai_daily_budget_usd)
        out.append(_r("narrative", "AI spend today", WARN if spent >= budget else PASS,
                      f"${spent:.2f} of ${budget:.2f} · {a['n']} coins judged"))
        out.append(_r("narrative", "Early calls (24 h)", PASS, f"{calls['n'] or 0} posted · {calls['open'] or 0} still tracked"))
    except sqlite3.Error:
        out.append(_r("narrative", "AI spend today", SKIP, "no judgements recorded yet"))
    return out


# ----------------------------------------------------------------------------- database
def check_database(db_path: str) -> list[dict]:
    out = []
    path = Path(db_path)
    if not path.exists():
        return [_r("database", "Database file", FAIL, f"{path} not found")]
    size = path.stat().st_size / 1e6
    out.append(_r("database", "Database file", WARN if size > 3000 else PASS, f"{size:,.0f} MB"))
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        t = time.monotonic()
        res = c.execute("PRAGMA quick_check(1)").fetchone()[0]
        out.append(_r("database", "Integrity", PASS if res == "ok" else FAIL, res, (time.monotonic() - t) * 1000))
        counts = []
        for table in ("candidates", "research_snapshots", "alerts", "decision_tracking", "shadow_calls", "radar_obs"):
            try:
                counts.append(f"{table} {c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:,}")
            except sqlite3.Error:
                counts.append(f"{table} —")
        out.append(_r("database", "Tables", PASS, " · ".join(counts)))
        c.close()
    except sqlite3.Error as e:
        out.append(_r("database", "Integrity", FAIL, f"{type(e).__name__}: {e}"))
    return out


# ----------------------------------------------------------------------------- engine
def check_engine(engine, db_path: str, now: Optional[float] = None) -> list[dict]:
    now = now or time.time()
    if engine is None:
        return [_r("engine", "Bot engine", SKIP, "not running inside the bot (run from the dashboard of the live bot)")]
    out = []
    st = engine.stats
    up = now - st.get("started_at", now)
    out.append(_r("engine", "Running", PASS, f"up {up / 3600:.1f} h · researched {st.get('researched', 0):,} · "
                  f"calls {st.get('auto_post', 0)} · errors {st.get('errors', 0)}"))
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        last = c.execute("SELECT MAX(ts) FROM research_snapshots").fetchone()[0]
        disc = c.execute("SELECT MAX(ts) FROM discovery_events").fetchone()[0]
        c.close()
    except sqlite3.Error:
        last = disc = None
    grace = up < 600
    for name, ts, limit in (("Research loop", last, 900), ("Discovery feeds", disc, 900)):
        if ts is None:
            out.append(_r("engine", name, WARN if grace else FAIL, "nothing recorded yet"))
        else:
            age = now - ts
            out.append(_r("engine", name, PASS if age < limit else (WARN if grace else FAIL),
                          f"last activity {age / 60:.0f} min ago"))
    q = getattr(engine, "queue", None)
    if q is not None:
        n = q.qsize()
        out.append(_r("engine", "Research queue", WARN if n > 2000 else PASS, f"{n:,} coins waiting"))
    radar = getattr(engine, "radar", None)
    out.append(_r("engine", "Surge radar", PASS if radar is not None else WARN,
                  f"on · {len(getattr(radar, 'trending', {}) or {})} trending coins tracked" if radar else "off"))
    return out


# ----------------------------------------------------------------------------- learning
def check_learning(s, db_path: str, now: Optional[float] = None) -> list[dict]:
    now = now or time.time()
    out = []
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        try:
            m = c.execute("SELECT * FROM brain_models ORDER BY ts DESC LIMIT 1").fetchone()
        except sqlite3.Error:
            m = None
        if m is None:
            out.append(_r("learning", "Brain", WARN, "not trained yet (needs 60 evaluated coins)"))
        else:
            auc = f"{m['auc']:.2f}" if m["auc"] is not None else "n/a"
            out.append(_r("learning", "Brain", PASS if m["trusted"] else WARN,
                          f"{'TRUSTED' if m['trusted'] else 'learning'} · {m['n']} coins, {m['positives']} runners · "
                          f"accuracy on unseen coins {auc} · trained {(now - m['ts']) / 3600:.1f} h ago"))
        try:
            ev = c.execute("SELECT COUNT(*) FROM shadow_calls WHERE evaluated=1 AND ts > ?", (now - 86400 * 2,)).fetchone()[0]
            pend = c.execute("SELECT COUNT(*) FROM shadow_calls WHERE evaluated=0").fetchone()[0]
            out.append(_r("learning", "Outcome checks", PASS if ev else WARN,
                          f"{ev} coins checked against their real price path (48 h) · {pend} waiting"))
            last = c.execute("SELECT ts, key, old, new FROM learning_log WHERE key != 'RESET' ORDER BY ts DESC LIMIT 1").fetchone()
            out.append(_r("learning", "Last change", PASS,
                          f"{last['key']} {last['old']:g} → {last['new']:g}, {(now - last['ts']) / 3600:.1f} h ago"
                          if last else "no settings changed yet"))
        except sqlite3.Error:
            out.append(_r("learning", "Outcome checks", WARN, "learning tables not created yet"))
        c.close()
    except sqlite3.Error as e:
        out.append(_r("learning", "Brain", FAIL, f"{type(e).__name__}: {e}"))
    if not s.learning_enabled:
        out.append(_r("learning", "Self-learning", WARN, "turned off (NORTHSTAR_LEARNING_ENABLED=false)"))
    return out


# ----------------------------------------------------------------------------- code
def check_code(timeout: float = 300) -> list[dict]:
    tests = ROOT / "tests"
    if not tests.is_dir():
        return [_r("code", "Test suite", SKIP, "tests folder not on this server")]
    try:
        import pytest  # noqa: F401
    except ImportError:
        return [_r("code", "Test suite", SKIP, "pytest not installed (pip install -r requirements.txt)")]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("NORTHSTAR_", "DISCORD"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    t = time.monotonic()
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=",
                            str(tests)], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return [_r("code", "Test suite", FAIL, f"did not finish within {timeout:.0f} s")]
    ms = (time.monotonic() - t) * 1000
    text = (p.stdout or "") + (p.stderr or "")
    summary = next((ln.strip("= ").strip() for ln in reversed(text.splitlines())
                    if re.search(r"\d+ (passed|failed|error)", ln)), "no summary")
    failed = re.findall(r"^FAILED (\S+)", text, re.M)[:8]
    status = PASS if p.returncode == 0 else FAIL
    detail = summary + ("\nFailed: " + ", ".join(f.split("::")[-1] for f in failed) if failed else "")
    return [_r("code", "Test suite", status, detail, ms)]


# ----------------------------------------------------------------------------- runner
async def run_async(s, db_path: str, providers=None, engine=None, include_code: bool = False) -> list[dict]:
    out = check_config(s)
    out += await check_discord(s)
    clients = []
    if providers is None:
        from .engine import build_providers
        providers, clients = build_providers(s, None)
    try:
        out += await check_providers(providers)
    finally:
        for c in clients:
            try:
                await c.close()
            except Exception:  # noqa: BLE001
                pass
    out += check_ai_and_events(s, db_path)
    out += check_database(db_path)
    out += check_engine(engine, db_path)
    out += check_learning(s, db_path)
    if include_code:
        out += await asyncio.to_thread(check_code)
    return out


def summary(results: list[dict]) -> dict:
    counts = {k: sum(1 for r in results if r["status"] == k) for k in (PASS, WARN, FAIL, SKIP)}
    return {"overall": FAIL if counts[FAIL] else (WARN if counts[WARN] else PASS), **counts}


def format_text(results: list[dict]) -> str:
    icon = {PASS: "✅", WARN: "⚠️", FAIL: "❌", SKIP: "⏭️"}
    lines, group = [], None
    for r in results:
        if r["group"] != group:
            group = r["group"]
            lines.append(f"\n[{group.upper()}]")
        ms = f" ({r['ms']} ms)" if r.get("ms") is not None else ""
        lines.append(f"  {icon[r['status']]} {r['name']}: {r['detail']}{ms}")
    sm = summary(results)
    lines.append(f"\nOVERALL: {sm['overall']}  ({sm[PASS]} pass, {sm[WARN]} warn, {sm[FAIL]} fail, {sm[SKIP]} skipped)")
    return "\n".join(lines)


class Runner:
    """One self-test at a time; keeps the last result for the dashboard."""

    def __init__(self):
        self.running = False
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.results: list[dict] = []
        self.error: Optional[str] = None

    def state(self) -> dict[str, Any]:
        return {"running": self.running, "started_at": self.started_at, "finished_at": self.finished_at,
                "results": self.results, "summary": summary(self.results) if self.results else None,
                "error": self.error}

    def start(self, run_fn) -> bool:
        """run_fn() -> list[dict], executed in a background thread."""
        import threading
        if self.running:
            return False
        self.running, self.started_at, self.error = True, time.time(), None

        def work():
            try:
                self.results = run_fn()
            except Exception as e:  # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}"
            finally:
                self.running, self.finished_at = False, time.time()
        threading.Thread(target=work, name="northstar-selftest", daemon=True).start()
        return True
