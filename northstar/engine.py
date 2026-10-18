"""Engine wiring and the continuous run loop."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

from .alerts import AlertService
from .config import Settings
from .db import Database
from .discovery import Discovery
from .early_tracker import EarlyWatchTracker
from .discord_bot.publisher import DiscordPublisher, DryRunPublisher, Publisher
from .models import Action
from .monitor import Monitor
from .net import ProviderClient
from .performance import OutcomeTracker
from .providers.dexscreener import DexScreener
from .providers.pumpportal import PumpPortalStream
from .providers.rugcheck import RugCheck
from .providers.solana_rpc import SolanaRpc
from .research import Providers, Researcher

log = logging.getLogger("northstar.engine")


# --------------------------------------------------------------------------- single instance
class InstanceLock:
    """Cross-platform exclusive lock file preventing duplicate workers."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            raise RuntimeError(f"another Northstar instance is running (lock: {self.path})")
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()

    def release(self) -> None:
        if self._fh:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None


# --------------------------------------------------------------------------- factory
def build_providers(s: Settings, db: Optional[Database],
                    transport: Optional[httpx.AsyncBaseTransport] = None) -> tuple[Providers, list[ProviderClient]]:
    health = db.record_provider if db else None
    budget = db.budget_used_today if db else None
    clients: list[ProviderClient] = []

    def mk(name, base, rpm, daily, **kw) -> ProviderClient:
        c = ProviderClient(name, base, rpm, daily, s.http_timeout_seconds, s.http_max_retries,
                           health_cb=health, budget_cb=budget, transport=transport, **kw)
        clients.append(c)
        return c

    p = Providers()
    if s.enable_dexscreener:
        p.dex = DexScreener(mk("dexscreener", "https://api.dexscreener.com", s.dexscreener_slow_rpm,
                               s.dexscreener_daily_budget),
                            mk("dexscreener_pairs", "https://api.dexscreener.com", s.dexscreener_fast_rpm,
                               s.dexscreener_daily_budget))
    if s.enable_rugcheck:
        p.rugcheck = RugCheck(mk("rugcheck", "https://api.rugcheck.xyz", s.rugcheck_rpm, s.rugcheck_daily_budget))
    if s.enable_rpc:
        url = httpx.URL(s.effective_rpc_url)
        origin = f"{url.scheme}://{url.host}" + (f":{url.port}" if url.port else "")
        path = url.raw_path.decode() or "/"
        p.rpc = SolanaRpc(mk("solana_rpc", origin, s.rpc_rpm, s.rpc_daily_budget,
                             headers={"Content-Type": "application/json"}), path)
    from .providers.sol_price import SolUsdPrice
    p.sol_price = SolUsdPrice(p.dex, mk("coingecko", "https://api.coingecko.com", 20, 20_000))
    if s.enable_coingecko:
        from .providers.coingecko import CoinGeckoOnchain
        keyed = None
        if s.coingecko_api_key and s.coingecko_api_key.get_secret_value().strip():
            key = s.coingecko_api_key.get_secret_value().strip()
            if s.coingecko_plan == "pro":
                keyed = mk("coingecko_onchain", "https://pro-api.coingecko.com/api/v3", s.coingecko_rpm,
                           s.coingecko_daily_budget, headers={"x-cg-pro-api-key": key})
            else:
                keyed = mk("coingecko_onchain", "https://api.coingecko.com/api/v3", s.coingecko_rpm,
                           s.coingecko_daily_budget, headers={"x-cg-demo-api-key": key})
        keyless = mk("geckoterminal", "https://api.geckoterminal.com/api/v2", s.geckoterminal_rpm, 20_000,
                     headers={"Accept": "application/json;version=20230302"})
        p.coingecko = CoinGeckoOnchain(keyed, keyless, keyed_prefix="/onchain", keyless_prefix="")
    if s.enable_free_socials:
        from .providers.socials import FreeSocials
        p.socials = FreeSocials(timeout=min(8.0, float(s.http_timeout_seconds)), transport=transport)
        clients.append(p.socials)  # closed with the other clients
    return p, clients


def build_publisher(s: Settings) -> Publisher:
    data = Path(s.data_dir)
    if s.dry_run or not s.discord_enabled:
        return DryRunPublisher(data / "dry_run_alerts.jsonl", s.discord_channel_id or 0)
    if not s.discord_token or not s.discord_channel_id:
        raise SystemExit("NORTHSTAR_DISCORD_TOKEN and NORTHSTAR_DISCORD_CHANNEL_ID are required "
                         "(or set NORTHSTAR_DRY_RUN=true)")
    return DiscordPublisher(s.discord_token.get_secret_value(), s.discord_channel_id)


# --------------------------------------------------------------------------- engine
class Engine:
    def __init__(self, s: Settings, db: Database, providers: Providers, publisher: Publisher,
                 clients: Optional[list[ProviderClient]] = None):
        self.s, self.db, self.p, self.pub = s, db, providers, publisher
        self.clients = clients or []
        self.researcher = Researcher(s, db, providers)
        self.discovery = Discovery(s, db, providers.dex)
        self.alerts = AlertService(s, db, publisher, providers.dex)
        self.monitor = Monitor(s, db, publisher, providers.dex, providers.rpc, getattr(providers, "coingecko", None))
        self.early_tracker = EarlyWatchTracker(s, db, publisher, providers.rpc, providers.sol_price)
        self.outcomes = OutcomeTracker(db, providers.dex) if providers.dex else None
        from .radar import SurgeRadar
        self.radar = (SurgeRadar(s, db, providers.dex, getattr(providers, "coingecko", None))
                      if (s.radar_enabled and providers.dex) else None)
        self._inflight: set[str] = set()
        self._radar_slots = asyncio.Semaphore(max(1, s.radar_concurrency))
        self._radar_tasks: set[asyncio.Task] = set()
        from . import learning
        self.learning = learning
        learning.init_schema(db)
        # values from .env/defaults, kept so /learning_reset can restore them
        self._tunable_defaults = {t.key: getattr(s, t.key) for t in learning.TUNABLES}
        self._learned_seen: dict = {}
        if s.learning_enabled:
            applied = learning.apply_learned(db, s)
            self._learned_seen = dict(applied)
            if applied:
                log.info("learning: applied learned settings %s", applied)
        self.stop = asyncio.Event()
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=s.max_queue_size)
        self._queued: set[str] = set()
        self._pp_last_health = 0.0
        self._pumpportal_stream = None
        self.stats = {"researched": 0, "auto_post": 0, "watchlist": 0, "rejected": 0, "insufficient": 0,
                      "errors": 0, "started_at": time.time()}

    # ---------------------------------------------------------------- lifecycle
    async def run(self) -> None:
        for problem in self.s.config_problems():
            log.warning("CONFIGURATION PROBLEM: %s", problem)
        recovered = self.db.recover_after_restart()
        if recovered:
            log.info("restart recovery: re-queued %d candidates interrupted mid-research", recovered)
        await self.pub.start()
        await self.alerts.resume_pending()
        tasks = [asyncio.create_task(self._loop("discovery", self.s.discovery_interval_seconds, self._discover)),
                 asyncio.create_task(self._loop("scheduler", 5, self._schedule)),
                 asyncio.create_task(self._loop("monitor", self.s.monitor_interval_seconds, self.monitor.run_once)),
                 asyncio.create_task(self._loop("early-watch-tracker", self.s.early_watch_refresh_seconds,
                                                self._track_early_watch)),
                 asyncio.create_task(self._loop("alert-retry", 120, self.alerts.resume_pending)),
                 asyncio.create_task(self._loop("free-channel-delivery", 15, self.alerts.publish_due_free_posts)),
                 asyncio.create_task(self._loop("heartbeat", 300, self._heartbeat)),
                 asyncio.create_task(self._loop("db-retention", 3600, self._prune))]
        if self.radar:
            tasks.append(asyncio.create_task(self._loop("surge-radar", self.s.radar_interval_seconds, self._radar)))
        if self.s.learning_enabled:
            tasks.append(asyncio.create_task(self._loop("learning-sync", 60, self._learning_sync)))
            if getattr(self.p, "coingecko", None) is not None:
                tasks.append(asyncio.create_task(self._loop("learning-evaluate", 300, self._learning_evaluate)))
                tasks.append(asyncio.create_task(self._loop("learning-tune", self.s.learning_interval_hours * 3600,
                                                            self._learning_tune)))
        if self.outcomes:
            tasks.append(asyncio.create_task(self._loop("outcomes", self.s.outcome_interval_seconds,
                                                        self.outcomes.run_once)))
        tasks += [asyncio.create_task(self._worker(i)) for i in range(self.s.research_concurrency)]
        if self.s.enable_pumpportal:
            key = self.s.pumpportal_api_key.get_secret_value() if self.s.pumpportal_api_key else None
            stream = PumpPortalStream(self.discovery.on_pumpportal, key, self._pp_health,
                                      enable_trade_stream=self.s.enable_pumpportal_trades,
                                      max_trade_subscriptions=self.s.max_pumpportal_trade_subscriptions,
                                      trade_subscription_ttl_seconds=self.s.pumpportal_trade_subscription_ttl_seconds)
            self._pumpportal_stream = stream
            tasks.append(asyncio.create_task(stream.run(self.stop), name="pumpportal"))
        log.info("northstar engine running (dry_run=%s, rpc=%s)", self.s.dry_run, self.s.rpc_label)
        try:
            await self.stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.shutdown()

    async def shutdown(self) -> None:
        for c in self.clients:
            await c.close()
        try:
            await self.pub.close()
        except Exception:  # noqa: BLE001
            log.exception("publisher close failed")
        log.info("northstar engine stopped cleanly")

    def request_stop(self) -> None:
        self.stop.set()

    def _pp_health(self, name: str, ok: bool, err: Optional[str]) -> None:
        # the stream is a firehose: record success at most once a minute
        if ok and time.time() - self._pp_last_health < 60:
            return
        self._pp_last_health = time.time()
        self.db.record_provider(name, ok, err)

    # ---------------------------------------------------------------- loops
    async def _loop(self, name: str, interval: float, fn) -> None:
        """Run fn, then sleep. Iterations never overlap; exceptions never kill the loop."""
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.stats["errors"] += 1
                log.exception("loop %s iteration failed", name)
            remaining = max(1.0, interval - (time.monotonic() - started))
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass

    async def _discover(self) -> None:
        counts = await self.discovery.run_once()
        total = sum(counts.values())
        # Log zero-result scans too: otherwise a feed returning nothing is indistinguishable
        # from a discovery loop that has silently stopped running.
        log.info("discovery scan completed: new_candidates=%d feeds=%s pumpportal_events=%s",
                 total, counts, self.discovery.pumpportal_events)

    async def _schedule(self) -> None:
        free = self.s.max_queue_size - self.queue.qsize()
        if free <= 0:
            return
        for c in self.db.due_candidates(limit=min(free, 100)):
            if c["mint"] in self._queued:
                continue
            self._queued.add(c["mint"])
            await self.queue.put((-c["priority"], c["next_research_at"] or 0, c["mint"]))

    async def _worker(self, idx: int) -> None:
        while not self.stop.is_set():
            _, _, mint = await self.queue.get()
            try:
                await self.process(mint)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.stats["errors"] += 1
                log.exception("research failed for %s", mint)
                self.db.set_state(mint, "INSUFFICIENT_DATA",
                                  next_research_at=time.time() + self.s.insufficient_data_recheck_seconds,
                                  reasons=["internal error during research (see logs)"])
            finally:
                self._queued.discard(mint)
                self.queue.task_done()

    # ---------------------------------------------------------------- self-learning
    async def _learning_sync(self) -> None:
        """Apply learned values written by the tuner or cleared by /learning_reset (commands thread)."""
        rows = {r["key"]: r["value"] for r in self.db.fetchall("SELECT key, value FROM learned_settings")}
        if rows == self._learned_seen:
            return
        for t in self.learning.TUNABLES:
            default = self._tunable_defaults[t.key]
            val = rows.get(t.key, default)
            if isinstance(default, int) and not isinstance(default, bool):
                val = int(val)
            setattr(self.s, t.key, val)
        self._learned_seen = rows
        log.info("learning: live settings now %s", {k: getattr(self.s, k) for k in rows} or "defaults (reset)")

    async def _learning_evaluate(self) -> None:
        if not getattr(self, "_backfilled", False):
            self._backfilled = True
            added = await asyncio.to_thread(self.learning.backfill_from_snapshots, self.db, self.s.learning_days)
            if added:
                log.info("learning: seeded %d past decisions from the database for evaluation", added)
        n = await self.learning.evaluate_due(self.db, self.p.coingecko, limit=20)
        if n:
            log.info("learning: evaluated %d coins against their real price path", n)

    async def _learning_tune(self) -> None:
        if not getattr(self, "_tune_started", False):
            self._tune_started = True        # first tick right after start: give evaluation time first
            return
        changes = await asyncio.to_thread(self.learning.tune, self.db, self.s, self.s.learning_days,
                                          self.s.learning_min_samples)
        self._learned_seen = {r["key"]: r["value"] for r in self.db.fetchall("SELECT key, value FROM learned_settings")}
        if not changes or not self.s.learning_announce:
            return
        info = self.learning.summary(self.db, self.s, self.s.learning_days)
        lines = [self.learning.fmt_change(c) for c in changes]
        lines.append(f"\nBased on {info['evaluated']} coins scored against their real price path "
                     f"(simulated with fees, worst-case fills). Undo anytime with /learning_reset.")
        embed = {"title": "🧠 Northstar learned something", "description": "\n".join(lines)[:4000],
                 "color": 0x9B59B6, "footer": {"text": "Self-learning · settings change only within safe rails"}}
        try:
            await self.pub.publish(embed, self.s.discord_warning_channel_id)
        except Exception:  # noqa: BLE001
            log.warning("could not announce learning changes", exc_info=True)

    def _record_shadow(self, mint: str, out, verdict, surge) -> None:
        if not self.s.learning_enabled or out.pair is None:
            return
        if out.score is None and not surge and not verdict:
            return
        if (out.report.get("lifecycle") == "PRE_MIGRATION_BONDING_CURVE"):
            return
        called = out.action == Action.AUTO_POST or bool(verdict)
        call_type = "RUG_SCALP" if verdict else ("STANDARD" if out.action == Action.AUTO_POST else None)
        try:
            self.learning.record(self.db, mint, out.report, out.pair, out.action.value, call_type, called, surge)
        except Exception:  # noqa: BLE001 - learning must never break research
            log.warning("could not record shadow for %s", mint, exc_info=True)

    async def _radar(self) -> None:
        for surge in await self.radar.run_once():
            if surge.mint in self._inflight:
                continue
            task = asyncio.create_task(self._radar_research(surge.mint))
            self._radar_tasks.add(task)
            task.add_done_callback(self._radar_tasks.discard)

    async def _radar_research(self, mint: str) -> None:
        async with self._radar_slots:
            started = time.monotonic()
            log.info("surge %s: researching now (skipping the queue)", mint)
            try:
                await self.process(mint)
                cand = self.db.get_candidate(mint) or {}
                log.info("surge %s: research finished in %.0fs -> %s", mint, time.monotonic() - started,
                         cand.get("last_action") or cand.get("state"))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.stats["errors"] += 1
                log.exception("surge research failed for %s", mint)

    async def process(self, mint: str) -> None:
        if mint in self._inflight:      # the radar and a queue worker can pick the same coin
            return
        self._inflight.add(mint)
        try:
            await self._process(mint)
        finally:
            self._inflight.discard(mint)

    async def _process(self, mint: str) -> None:
        cand = self.db.get_candidate(mint)
        if not cand or cand["state"] in ("PUBLISHED", "WARNING", "CLOSED", "QUALIFIED", "STALE"):
            return
        prev_state = cand["state"]
        self.db.set_state(mint, "RESEARCHING")
        out = await self.researcher.research(mint, cand)
        self.stats["researched"] += 1
        now = time.time()
        if out.skipped_unchanged:
            self.db.set_state(mint, prev_state, next_research_at=now + self.s.rejected_recheck_seconds)
            return
        common = dict(last_research_at=now, research_count=cand["research_count"] + 1, last_data_hash=out.data_hash,
                      last_action=out.action.value, last_score=out.score, last_risk=out.risk.value,
                      last_confidence=out.confidence.value, reasons=out.reasons[:20],
                      pool_address=out.pair.pair_address if out.pair else None)
        if out.pair:
            common.update(name=out.pair.base_name, symbol=out.pair.base_symbol)
        surge = self.radar.last_surges.get(mint) if self.radar else None
        if surge is not None and time.time() - self.radar._last_trigger.get(mint, 0) < 900:
            out.report["surge"] = {**surge.to_dict(), "line": surge.line()}
        trend = self.radar.trending.get(mint) if self.radar else None
        if trend:
            out.report["trending"] = {**trend, "line": f"🔥 Trending #{trend['rank']} on CoinGecko/GeckoTerminal "
                                                         "(Solana, last 5 min)"}
        verdict = None
        if out.action == Action.REJECT and out.pair is not None:
            from .scalp import check as scalp_check
            verdict = scalp_check(out.report, out.pair, self.s, out.report.get("surge"))
        self._record_shadow(mint, out, verdict, out.report.get("surge"))
        if verdict:
            # Rug with strong momentum: posted ONLY as a clearly labelled scalp with a tight plan.
            out.report["call_type"] = "RUG_SCALP"
            out.report["scalp"] = verdict
            self.stats["scalp"] = self.stats.get("scalp", 0) + 1
            self.db.set_state(mint, "QUALIFIED", next_research_at=None, **common)
            log.info("RUG SCALP %s: %s | rug: %s", mint, verdict["momentum"], "; ".join(verdict["failed_checks"]))
            if await self.alerts.publish(out) is None:
                self.db.set_state(mint, "REJECTED", next_research_at=now + self.s.rejected_recheck_seconds)
        elif out.action == Action.AUTO_POST:
            self.stats["auto_post"] += 1
            self.db.set_state(mint, "QUALIFIED", next_research_at=None, **common)
            await self.alerts.publish(out)
        elif out.action == Action.WATCHLIST:
            self.stats["watchlist"] += 1
            if (out.report.get("pre_migration") or {}).get("qualified_early_watch"):
                await self.alerts.publish_early_watch(out)
            # high-potential candidates are re-checked more often
            interval = self.s.watchlist_recheck_seconds / (2 if (out.score or 0) >= 75 else 1)
            self.db.set_state(mint, "WATCHLIST", next_research_at=now + interval, **common)
        elif out.action == Action.INSUFFICIENT_DATA:
            self.stats["insufficient"] += 1
            self.db.set_state(mint, "INSUFFICIENT_DATA",
                              next_research_at=now + self.s.insufficient_data_recheck_seconds, **common)
        else:
            self.stats["rejected"] += 1
            permanent = any(r.startswith(("invalid mint", "not a valid SPL mint")) or "pool age" in r
                            for r in out.reasons)
            self.db.set_state(mint, "REJECTED",
                              next_research_at=None if permanent else now + self.s.rejected_recheck_seconds, **common)
        log.info("decision %s %s score=%s risk=%s", mint, out.action.value, out.score, out.risk.value,
                 extra={"ctx": {"mint": mint, "action": out.action.value, "score": out.score,
                                "risk": out.risk.value, "reasons": out.reasons[:5]}})

    async def _track_early_watch(self) -> None:
        counts = await self.early_tracker.run_once()
        if counts["open"]:
            log.info("early-watch tracker: %s", counts)

    async def _prune(self) -> None:
        removed = await asyncio.to_thread(self.db.prune_old_data, self.s.discovery_retention_days,
                                          self.s.snapshot_retention_days)
        if any(removed.values()):
            log.info("database retention: removed %s", removed)

    async def _heartbeat(self) -> None:
        h = {r["provider"]: r["status"] for r in self.db.provider_health()}
        log.info("heartbeat stats=%s providers=%s queue=%d", self.stats, h, self.queue.qsize())


def install_signal_handlers(engine: Engine) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, engine.request_stop)
        except (NotImplementedError, RuntimeError):  # Windows: KeyboardInterrupt handled in cli
            pass
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(engine.request_stop))
