"""Command line interface.

  python -m northstar run                 continuous engine
  python -m northstar funnel [--hours 24] decision funnel: counts and exact reasons per stage
  python -m northstar audit [--json]      false rejections vs rugs called on the bot's own history
  python -m northstar health              config, database and live provider checks
  python -m northstar analyze <MINT>      research one token now (never posts to Discord)
  python -m northstar report              historical performance report
  python -m northstar candidates          recent decisions and their reasons
  python -m northstar test-discord        posts ONE clearly-labelled test embed to your channel
  python -m northstar init-db             create / verify the database
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from .config import Settings, get_settings
from .db import Database
from .logging_setup import setup_logging


def _db(s: Settings) -> Database:
    db = Database(Path(s.data_dir) / s.db_filename)
    db.open()
    return db


async def _run(s: Settings) -> int:
    from .engine import Engine, InstanceLock, build_providers, build_publisher, install_signal_handlers
    lock = InstanceLock(Path(s.data_dir) / "northstar.lock")
    try:
        lock.acquire()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if getattr(s, "db_vacuum_on_start", False):
        # v2.5.5: give space freed by the retention pass back to the disk, once, before the bot
        # opens the database (we hold the instance lock, so nothing else is using it)
        from . import retention as RT
        path = str(Path(s.data_dir) / s.db_filename)
        r = RT.vacuum_on_start(path)
        if r:
            print(f"[northstar] database shrunk: {r['before_bytes'] / 1e6:,.0f} MB -> "
                  f"{r['after_bytes'] / 1e6:,.0f} MB", flush=True)
    db = _db(s)
    try:
        from .dashboard import attach, start_dashboard
        start_dashboard(db.path, settings=s)
        providers, clients = build_providers(s, db)
        engine = Engine(s, db, providers, build_publisher(s), clients)
        attach(engine, asyncio.get_running_loop())
        install_signal_handlers(engine)
        if s.discord_enabled and s.discord_token:
            # Own thread + event loop, so engine load can never delay slash-command replies.
            from .discord_bot.commands import start_commands_thread
            start_commands_thread(s, str(db.path))
        await engine.run()
    finally:
        db.close()
        lock.release()
    return 0


async def _analyze(s: Settings, mint: str, as_json: bool) -> int:
    from .engine import build_providers
    from .research import Researcher
    db = _db(s)
    providers, clients = build_providers(s, db)
    try:
        out = await Researcher(s, db, providers).research(mint.strip(), force=True)
    finally:
        for c in clients:
            await c.close()
        db.close()
    if as_json:
        print(json.dumps(out.report, indent=2, default=str))
        return 0
    r = out.report
    print(f"\nMint:        {mint}")
    ident = r.get("identity") or {}
    print(f"Token:       ${ident.get('symbol', '?')} {ident.get('name', '')}")
    print(f"Pool:        {(r.get('pool') or {}).get('address')} — {(r.get('pool') or {}).get('reason')}")
    final = r.get("final") or {}
    print(f"Decision:    {out.action.value}" + (f" ({final['decision']})" if final.get("decision") else ""))
    print(f"Score:       {out.score if out.score is not None else 'not scored (stopped at a gate/prefilter)'}"
          + (f"   [{final.get('score_source')}]" if final.get("score_source") else ""))
    print(f"Risk:        {out.risk.label}")
    print(f"Confidence:  {out.confidence.value}")
    if final.get("decision"):
        print(f"Safety:      {final.get('safety')}    Entry timing: {final.get('early_entry')}    "
              f"Completeness: {float(final.get('completeness') or 0) * 100:.0f}%")
    print(f"Why class:   {final.get('reason_class') or '?'}")
    print("Reasons:")
    for x in out.reasons:
        print(f"  - {x}")
    u = r.get("unified") or {}
    if u.get("categories"):
        from .analysis.unified import category_text
        print("\nScore by category (points/weight, coverage):")
        print("  " + category_text(u).replace("`", "").replace("\n", "\n  "))
    if (r.get("score") or {}).get("components"):
        print(f"\nLegacy v2.3 opportunity score (NOT used for decisions): {(r.get('score') or {}).get('total')}")
    for c in (r.get("score") or {}).get("components", []):
        print(f"\n  [{c['name']}] {c['points']:.1f}/{c['max_points']:.0f}")
        for e in c["evidence"]:
            print(f"     + {e}")
        for p in c["penalties"]:
            print(f"     - {p}")
        for m in c["missing"]:
            print(f"     ? {m}")
    if r.get("flags"):
        print("\nFindings:")
        for f in r["flags"]:
            print(f"  [{f['severity']}/{f['status']}] {f['message']}")
    print("\nSources:", r.get("sources"))
    print("\nThis is research output, not a trade recommendation.")
    return 0


async def _health(s: Settings) -> int:
    from .engine import build_providers
    ok = True
    print("Configuration")
    print(f"  dry_run={s.dry_run} discord_enabled={s.discord_enabled}")
    print(f"  discord token set: {bool(s.discord_token)}  channel: {s.discord_channel_id or 'NOT SET'}")
    print(f"  rpc: {s.rpc_label}  helius key: {bool(s.helius_api_key)}")
    if not s.dry_run and s.discord_enabled and not (s.discord_token and s.discord_channel_id):
        print("  !! Discord token/channel missing (set them or NORTHSTAR_DRY_RUN=true)")
        ok = False
    for problem in s.config_problems():
        print(f"  !! {problem}")
        ok = False
    db = _db(s)
    print(f"\nDatabase: {db.path}  integrity: "
          f"{'RECOVERED — ' + db.recovered_from_corruption if db.recovered_from_corruption else 'ok'}")
    providers, clients = build_providers(s, db)
    print("\nLive provider checks")
    checks = []
    if providers.dex:
        checks.append(("dexscreener", providers.dex.latest_profiles()))
    if providers.rugcheck:
        checks.append(("rugcheck", providers.rugcheck.summary("So11111111111111111111111111111111111111112")))
    if providers.rpc:
        checks.append(("solana_rpc", providers.rpc.call("getSlot", [])))
    for name, coro in checks:
        t = time.monotonic()
        try:
            await coro
            print(f"  {name:<12} OK   ({(time.monotonic() - t) * 1000:.0f} ms)")
        except Exception as e:  # noqa: BLE001
            print(f"  {name:<12} FAIL {type(e).__name__}: {e}")
            ok = False
    for c in clients:
        await c.close()
    print("\nRecorded provider health")
    for r in db.provider_health():
        print(f"  {r['provider']:<18} {r['status']:<9} ok={r['ok_count']} err={r['err_count']} "
              f"used_today={r['budget_used']} last_error={r['last_error'] or '-'}")
    lock = Path(s.data_dir) / "northstar.lock"
    print(f"\nEngine lock file: {'present' if lock.exists() else 'absent'}")
    db.close()
    print("\nHEALTH:", "OK" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1


def _report(s: Settings, as_json: bool) -> int:
    from .performance import format_report, performance_report
    db = _db(s)
    rep = performance_report(db)
    db.close()
    print(json.dumps(rep, indent=2) if as_json else format_report(rep))
    return 0


def _candidates(s: Settings, limit: int) -> int:
    db = _db(s)
    rows = db.fetchall("SELECT mint, symbol, state, last_score, last_risk, reasons, updated_at FROM candidates "
                       "ORDER BY updated_at DESC LIMIT ?", (limit,))
    db.close()
    for r in rows:
        reasons = json.loads(r["reasons"] or "[]")
        print(f"{time.strftime('%H:%M:%S', time.localtime(r['updated_at']))} {r['state']:<17} "
              f"{(r['symbol'] or '?')[:10]:<10} score={r['last_score']} risk={r['last_risk']} {r['mint']}")
        if reasons:
            print(f"      {reasons[0][:140]}")
    return 0


async def _test_discord(s: Settings) -> int:
    from .discord_bot.publisher import DiscordPublisher
    if not (s.discord_token and s.discord_channel_id):
        print("Set NORTHSTAR_DISCORD_TOKEN and NORTHSTAR_DISCORD_CHANNEL_ID first.")
        return 1
    pub = DiscordPublisher(s.discord_token.get_secret_value(), s.discord_channel_id)
    await pub.start()
    try:
        ch, mid = await pub.publish({"title": "Northstar test message",
                                     "description": "Connectivity test. This is NOT a token callout.",
                                     "color": 0x95A5A6})
        print(f"Posted test message {mid} to channel {ch}")
    finally:
        await pub.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="northstar", description="Northstar Alpha Engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("health")
    a = sub.add_parser("analyze")
    a.add_argument("mint")
    a.add_argument("--json", action="store_true")
    r = sub.add_parser("report")
    r.add_argument("--json", action="store_true")
    c = sub.add_parser("candidates")
    c.add_argument("--limit", type=int, default=30)
    sub.add_parser("test-discord")
    st = sub.add_parser("selftest", help="check every part of Northstar (add --code to run the test suite)")
    st.add_argument("--code", action="store_true")
    sub.add_parser("init-db")
    au = sub.add_parser("audit", help="false rejections vs rugs called, measured on the bot's own history")
    au.add_argument("--json", action="store_true")
    fn = sub.add_parser("funnel", help="how many coins reached each stage, and why they stopped")
    fn.add_argument("--hours", type=float, default=24.0)
    fn.add_argument("--json", action="store_true")
    sh = sub.add_parser("shadow", help="live decisions vs the proposed scoring policy, with outcomes")
    sh.add_argument("--days", type=float, default=7.0)
    sh.add_argument("--policy", default=None)
    sh.add_argument("--json", action="store_true")
    dbm = sub.add_parser("db", help="database size, and the retention/compaction pass (dry run unless --apply)")
    dbm.add_argument("--apply", action="store_true", help="run the retention pass now")
    dbm.add_argument("--vacuum", action="store_true", help="also shrink the file (STOP the bot first)")
    dash = sub.add_parser("dashboard", help="run the read-only web dashboard")
    dash.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)
    s = get_settings()
    setup_logging(s.log_level, s.log_json and args.cmd == "run", s.secrets())
    try:
        if args.cmd == "run":
            return asyncio.run(_run(s))
        if args.cmd == "health":
            return asyncio.run(_health(s))
        if args.cmd == "analyze":
            return asyncio.run(_analyze(s, args.mint, args.json))
        if args.cmd == "report":
            return _report(s, args.json)
        if args.cmd == "candidates":
            return _candidates(s, args.limit)
        if args.cmd == "selftest":
            from . import selftest as ST
            db = _db(s)
            path = str(db.path)
            db.close()
            res = asyncio.run(ST.run_async(s, path, include_code=args.code))
            print(ST.format_text(res))
            return 1 if ST.summary(res)["overall"] == ST.FAIL else 0
        if args.cmd == "audit":
            from . import outcome_audit as OA
            db = _db(s)
            path = str(db.path)
            db.close()
            a = OA.audit(path, s)
            print(json.dumps(a, indent=2, default=str) if args.json else OA.format_audit(a))
            return 0
        if args.cmd == "funnel":
            from . import telemetry as T
            db = _db(s)
            try:
                sm = T.summary(db, args.hours)
            finally:
                db.close()
            print(json.dumps(sm, indent=2, default=str) if args.json else T.format_summary(sm))
            return 0
        if args.cmd == "shadow":
            from . import shadow_test as SH
            db = _db(s)
            try:
                SH.evaluate_due(db)
                sm = SH.summary(db, args.days, policy=args.policy)
                pols = [args.policy] if args.policy else (sm.get("policies") or [None])
                sms = [sm if p == sm.get("policy") else SH.summary(db, args.days, policy=p) for p in pols]
            finally:
                db.close()
            if args.json:
                print(json.dumps(sms if len(sms) > 1 else sm, indent=2, default=str))
            else:
                print("\n\n".join(SH.format_summary(x) for x in sms))
            return 0
        if args.cmd == "db":
            from . import retention as RT
            db = _db(s)
            path = str(db.path)
            try:
                print(RT.format_sizes(RT.table_sizes(path), RT.file_size(path)))
                if args.apply:
                    print("retention pass:", json.dumps(RT.maintain(db, s), default=str))
                else:
                    print("DRY RUN (nothing changed) — would remove:", json.dumps(RT.estimate(db, s), default=str))
                    if args.vacuum:
                        print("--vacuum ignored: it only runs together with --apply")
                    print("python main.py db --apply            runs the retention pass now")
                    print("python main.py db --apply --vacuum   also shrinks the file (stop the bot first)")
            finally:
                db.close()
            if args.apply and args.vacuum:
                from pathlib import Path
                try:
                    r = RT.vacuum(path, str(Path(s.data_dir) / "northstar.lock"))
                except RuntimeError as e:
                    print(f"vacuum skipped: {e}")
                    return 1
                print(f"vacuum: {r['before_bytes'] / 1e6:,.1f} MB -> {r['after_bytes'] / 1e6:,.1f} MB")
                print(RT.format_sizes(RT.table_sizes(path), RT.file_size(path)))
            return 0
        if args.cmd == "test-discord":
            return asyncio.run(_test_discord(s))
        if args.cmd == "init-db":
            db = _db(s)
            print(f"database ready at {db.path}")
            db.close()
            return 0
        if args.cmd == "dashboard":
            from .dashboard import DashboardServer, pick_port
            db = _db(s)
            port = args.port or pick_port(s)
            server = DashboardServer(db.path, s.dashboard_host, port, settings=s,
                                     password=s.dashboard_password.get_secret_value() if s.dashboard_password else "")
            print(f"Northstar dashboard listening on port {port}; press Ctrl+C to stop")
            try:
                server.serve_forever()
            finally:
                db.close()
            return 0
    except KeyboardInterrupt:
        print("interrupted")
        return 130
    return 1


if __name__ == "__main__":
    sys.exit(main())
