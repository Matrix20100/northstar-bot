"""Engine wiring and the continuous run loop."""
from __future__ import annotations

import asyncio
import json
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
from . import telemetry as T
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
        # the same CoinGecko key also serves the crypto-narrative feeds (trending + hot categories)
        from .providers.coingecko import CoinGeckoMarkets
        markets = keyed or mk("coingecko_markets", "https://api.coingecko.com/api/v3", 10, 5_000)
        p.coingecko_markets = CoinGeckoMarkets(markets)
    if s.enable_jupiter:
        from .providers.jupiter import Jupiter
        p.jupiter = Jupiter(mk("jupiter", "https://lite-api.jup.ag", s.jupiter_rpm, 50_000))
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
        # early narrative calls: live attention feed + Claude judge + curve radar + tracker
        from .ai_judge import AIJudge
        from .early_calls import CurveRadar, EarlyCaller, EarlyCallTracker
        from .events import EventFeeds
        self.events = EventFeeds(s, db, cg_markets=getattr(providers, "coingecko_markets", None))
        self.judge = AIJudge(s, db)
        self.early = EarlyCaller(s, db, publisher, self.events, self.judge)
        self.early_calls = EarlyCallTracker(s, db, publisher, providers.rpc, providers.dex, providers.sol_price)
        self.curve_radar = (CurveRadar(s, db, providers.dex)
                            if s.early_call_enabled and s.curve_radar_enabled and providers.dex is not None else None)
        self.outcomes = OutcomeTracker(db, providers.dex) if providers.dex else None
        if self.outcomes:
            self.outcomes.poll_limit = int(getattr(s, "tracking_poll_limit", 600) or 600)
            self.outcomes.poll_interval = float(getattr(s, "outcome_interval_seconds", 120) or 120)
        from .radar import SurgeRadar
        self.radar = (SurgeRadar(s, db, providers.dex, getattr(providers, "coingecko", None))
                      if (s.radar_enabled and providers.dex) else None)
        from .attention import Attention
        self.attention = Attention(s, self.events, self.radar)
        self.early.attention = self.attention
        if self.curve_radar is not None:
            self.curve_radar.attention = self.attention
        self.attention.db = db                  # v4.2.0: Twitter feed + learned theme numbers in attention
        self.twitter = None
        try:
            from . import social_feed as SF
            from .analysis.identity import is_valid_mint_format
            raw_tok = getattr(s, "discord_token", None)
            tok = (raw_tok.get_secret_value() if hasattr(raw_tok, "get_secret_value") else raw_tok) or None
            if SF.channels(s):
                self.twitter = SF.TwitterFeed(s, db, tok, is_mint=is_valid_mint_format)
        except Exception:  # noqa: BLE001 - optional feature
            log.warning("Twitter feed could not start", exc_info=True)
        # v3.5.0: tracked wallets' live trades (PumpPortal account stream) -> money flow
        self.wallet_flow = None
        if getattr(s, "wallet_watch_enabled", True) and getattr(s, "tracked_wallets_enabled", True):
            try:
                from pathlib import Path
                from .analysis import holdings as HO
                from .wallet_flow import WalletFlow
                path = Path(getattr(s, "tracked_wallets_file", "") or "")
                wallets = HO.load_tracked_wallets(path) if path.is_file() else []
                if wallets:
                    self.wallet_flow = WalletFlow(s, db, wallets)
                    self.discovery.wallet_flow = self.wallet_flow
                    self.discovery.on_wallet_signal = self._on_wallet_signal
                    self.attention.flow = self.wallet_flow
            except Exception:  # noqa: BLE001 - optional feature
                log.warning("wallet watch could not start", exc_info=True)
        self._inflight: set[str] = set()
        self._manual: dict[str, tuple] = {}      # mint -> (outcome, researched_at) from the dashboard
        self._manual_busy: set[str] = set()
        self._radar_slots = asyncio.Semaphore(max(1, s.radar_concurrency))
        self._radar_tasks: set[asyncio.Task] = set()
        from . import brain, learning
        self.learning = learning
        self.brain_mod = brain
        learning.init_schema(db)
        self.brain = brain.latest(db) if s.brain_enabled else None
        from .analysis.context import Heat
        self._heat = Heat()
        self._brain_trusted = bool(self.brain and self.brain.trusted)
        if self.brain is not None:
            log.info("brain: %s", brain.describe(self.brain))
        from . import rugmodel
        self.rug_mod = rugmodel
        self.rug = rugmodel.latest(db) if getattr(s, "rug_model_enabled", True) else None
        if self.rug is not None:
            log.info("%s", rugmodel.describe(self.rug))
        # values from .env/defaults, kept so /learning_reset can restore them
        self._tunable_defaults = {t.key: getattr(s, t.key) for t in learning.TUNABLES}
        self._learned_seen: dict = {}
        if s.learning_enabled:
            applied = learning.apply_learned(db, s)
            self._learned_seen = dict(applied)
            if applied:
                log.info("learning: applied learned settings %s", applied)
        # v4.1.0: values set on the dashboard beat .env, defaults and learned values
        self._operator_keys: set = set()
        from . import brain as _B
        _B.DEAD_MCAP = float(getattr(s, "dead_mcap_usd", _B.DEAD_MCAP) or 0)     # v4.3.0
        try:
            from . import operator_settings as OS
            applied_op = OS.apply(db, s)
            self._operator_keys = set(applied_op)
            if applied_op:
                log.info("dashboard settings applied: %s", applied_op)
        except Exception:  # noqa: BLE001
            log.warning("dashboard settings could not be applied", exc_info=True)
        try:
            from . import curve_learn as CL
            CL.ensure(db)
            floor = CL.apply_saved(db, s)
            if floor is not None:
                log.info("curve learner: runner score floor %.0f (learned)", floor)
        except Exception:  # noqa: BLE001 - learning must never stop the start
            log.debug("curve learner start failed", exc_info=True)
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
        if not getattr(self.s, "research_new_launches_on_timer", False):
            dropped = self.db.unschedule_stale_launches()
            if dropped:
                log.info("queue: released %d queued launches that had never been researched; "
                         "the curve radar brings back the ones that start trading", dropped)
        if recovered:
            log.info("restart recovery: re-queued %d candidates interrupted mid-research", recovered)
        try:
            n = self.db.backfill_creator_stats()
            if n:
                log.info("creator history: %d creator wallets counted from the stored launch feed", n)
        except Exception:  # noqa: BLE001 - statistics must never stop the bot
            log.warning("creator history backfill failed", exc_info=True)
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
        if getattr(self.s, "paper_enabled", True):
            tasks.append(asyncio.create_task(self._loop("paper-trader", 15, self._paper)))
        if self.twitter is not None:
            if self.twitter.enabled():
                tasks.append(asyncio.create_task(self._loop("twitter-feed", float(self.s.twitter_feed_seconds),
                                                            self._twitter_feed)))
            else:
                log.warning("Twitter feed is OFF: reading the J7 / uxento channel needs the Discord bot token "
                            "(NORTHSTAR_DISCORD_TOKEN)")
        if getattr(self.s, "curve_learn_enabled", True):
            tasks.append(asyncio.create_task(self._loop("curve-learner", float(self.s.curve_learn_minutes) * 60,
                                                        self._curve_learn)))
        if getattr(self.s, "cleanup_enabled", True):
            tasks.append(asyncio.create_task(self._loop("training-cleanup", 86400, self._cleanup)))
        self._tasks = tasks
        if getattr(self.s, "watchdog_enabled", True):
            tasks.append(asyncio.create_task(self._loop("watchdog", 60, self._watchdog)))
        if self.radar:
            tasks.append(asyncio.create_task(self._loop("surge-radar", self.s.radar_interval_seconds, self._radar)))
        if self.s.early_call_enabled:
            tasks.append(asyncio.create_task(self._loop("event-feeds", 60, self._refresh_attention)))
            tasks.append(asyncio.create_task(self._loop("early-call-tracker", self.s.early_watch_refresh_seconds,
                                                        self._track_early_calls)))
            if self.curve_radar is not None:
                tasks.append(asyncio.create_task(self._loop("curve-radar", self.s.curve_radar_seconds,
                                                            self._curve_radar)))
            if not self.judge.enabled and not getattr(self.s, "runner_calls_enabled", True):
                log.warning("early narrative calls need NORTHSTAR_ANTHROPIC_API_KEY (Claude) - AI judge is off")
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
                                      trade_subscription_ttl_seconds=self.s.pumpportal_trade_subscription_ttl_seconds,
                                      account_keys=self._wallet_keys)
            if self.wallet_flow is not None:
                if key:
                    tasks.append(asyncio.create_task(self._loop("wallet-watch", 60, self._wallet_watch)))
                else:
                    log.warning("wallet watch is OFF: streaming what your %d tracked wallets buy needs "
                                "NORTHSTAR_PUMPPORTAL_API_KEY (a PumpPortal API key whose linked wallet holds "
                                "at least 0.02 SOL; metered at 0.01 SOL per 10,000 trades)",
                                len(self.wallet_flow.names))
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
        """Run fn, then sleep. Iterations never overlap; exceptions never kill the loop.
        v3.3.0: each iteration's start and end are recorded for the watchdog (self-repair)."""
        beats = self.__dict__.setdefault("_beats", {})
        try:
            asyncio.current_task()._ns_loop = name          # so the watchdog can find this loop's task
        except (AttributeError, RuntimeError):
            pass
        self.__dict__.setdefault("_loop_specs", {})[name] = (interval, fn)
        while not self.stop.is_set():
            started = time.monotonic()
            beats[name] = {"start": started, "end": None, "interval": interval}
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.stats["errors"] += 1
                log.exception("loop %s iteration failed", name)
            beats[name]["end"] = time.monotonic()
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
        try:
            from . import operator_settings as OS
            operator = OS.keys(self.db)
        except Exception:  # noqa: BLE001
            operator = set()
        for t in self.learning.TUNABLES:
            if t.key in operator:
                continue                  # v4.1.0: a value set on the dashboard is never overwritten
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
            filled = await asyncio.to_thread(self.learning.backfill_vectors, self.db)
            if filled:
                log.info("brain: rebuilt the numbers for %d earlier coins from saved research", filled)
        kept = await asyncio.to_thread(self.brain_mod.store_tracking_vectors, self.db)
        if kept > 50:
            log.info("brain: stored the numbers for %d tracked decisions", kept)
        now = time.time()
        if now - getattr(self, "_learning_pruned", 0) > 6 * 3600:
            self._learning_pruned = now
            # learning rows are kept as long as the learner and the brain read them (v2.5.5; was 45 days)
            keep = max(float(self.s.learning_days), float(self.s.brain_days), 7.0)
            removed = await asyncio.to_thread(self.learning.prune, self.db, now, keep)
            if any(removed.values()):
                log.info("learning: removed old learning rows %s", removed)
        n = await self.learning.evaluate_due(self.db, self.p.coingecko, limit=20)
        if n:
            log.info("learning: evaluated %d coins against their real price path", n)
        if getattr(self.s, "shadow_test_enabled", True):
            try:
                from . import shadow_test as SH
                k = await asyncio.to_thread(SH.evaluate_due, self.db, now)
                if k:
                    log.info("shadow test: attached outcomes to %d recorded decisions", k)
                if now - getattr(self, "_shadow_pruned", 0) > 6 * 3600:
                    self._shadow_pruned = now
                    await asyncio.to_thread(SH.prune, self.db, now)
            except Exception:  # noqa: BLE001 - housekeeping must never break the bot
                log.debug("shadow test evaluation failed", exc_info=True)
        if self.s.radar_learning:
            r = await self.learning.evaluate_radar_due(self.db, self.p.coingecko, limit=10)
            if r:
                log.info("learning: evaluated %d radar observations against their real price path", r)

    async def _learning_tune(self) -> None:
        if not getattr(self, "_tune_started", False):
            self._tune_started = True        # first tick right after start: give evaluation time first
            return
        changes = await asyncio.to_thread(self.learning.tune, self.db, self.s, self.s.learning_days,
                                          self.s.learning_min_samples)
        self._learned_seen = {r["key"]: r["value"] for r in self.db.fetchall("SELECT key, value FROM learned_settings")}
        was_trusted = self._brain_trusted
        if self.s.brain_enabled:
            self.brain = self.brain_mod.latest(self.db)
            self._brain_trusted = bool(self.brain and self.brain.trusted)
        if getattr(self.s, "rug_model_enabled", True):
            self.rug = self.rug_mod.latest(self.db)
        trust_changed = self._brain_trusted != was_trusted
        if changes or trust_changed:
            log.info("learning: %s%s", "; ".join(self.learning.fmt_change(c) for c in changes),
                     (" | brain is now " + ("TRUSTED" if self._brain_trusted else "not trusted")) if trust_changed else "")
        if not (changes or trust_changed) or not self.s.learning_announce:
            return
        info = self.learning.summary(self.db, self.s, self.s.learning_days)
        lines = [self.learning.fmt_change(c) for c in changes]
        if trust_changed:
            lines.insert(0, ("**The learning brain is now TRUSTED** — it beats a coin flip on coins it has not seen, "
                             "so its odds now count in calls." if self._brain_trusted else
                             "**The learning brain lost its say** — its recent predictions fell below the bar, so "
                             "calls use the normal rules until it earns trust back."))
        if self.brain is not None:
            lines.append("\n🧠 " + self.brain_mod.describe(self.brain))
            sig = ", ".join(f"{'+' if c > 0 else '−'} {name}" for name, c in self.brain.importance(5))
            if sig:
                lines.append(f"Strongest signals it found: {sig}")
        lines.append(f"\nBased on {info['evaluated']} coins scored against their real price path "
                     f"(simulated with fees, worst-case fills). Undo anytime with /learning_reset.")
        embed = {"title": "🧠 Northstar learned something", "description": "\n".join(lines)[:4000],
                 "color": 0x9B59B6, "footer": {"text": "Self-learning · settings change only within safe rails"}}
        try:
            await self.pub.publish(embed, self.s.discord_warning_channel_id)
        except Exception:  # noqa: BLE001
            log.warning("could not announce learning changes", exc_info=True)

    def _record_shadow(self, mint: str, out, verdict, surge, brain_p: Optional[float] = None) -> None:
        if not self.s.learning_enabled or out.pair is None:
            return
        if out.score is None and not surge and not verdict:
            return
        if (out.report.get("lifecycle") == "PRE_MIGRATION_BONDING_CURVE"):
            return
        called = out.action == Action.AUTO_POST or bool(verdict)
        call_type = "RUG_SCALP" if verdict else ("STANDARD" if out.action == Action.AUTO_POST else None)
        try:
            self.learning.record(self.db, mint, out.report, out.pair, out.action.value, call_type, called, surge,
                                 brain_p=brain_p)
        except Exception:  # noqa: BLE001 - learning must never break research
            log.warning("could not record shadow for %s", mint, exc_info=True)

    def _record_shadow_test(self, out, since: float) -> None:
        """Live vs proposed decision for this coin (shadow_test.py). Record-only, never raises."""
        if not getattr(self.s, "shadow_test_enabled", True):
            return
        try:
            from . import shadow_test as SH
            row = self.db.fetchone("SELECT id FROM shadow_calls WHERE mint=? AND ts>=? ORDER BY id DESC LIMIT 1",
                                   (out.mint, since)) if self.s.learning_enabled else None
            SH.record(self.db, out, self.s, shadow_call_id=row["id"] if row else None)
        except Exception:  # noqa: BLE001 - shadow testing must never break research
            log.debug("shadow test failed for %s", out.mint, exc_info=True)

    POLICY_PREFIXES = ("alert cooldown", "daily alert cap", "market cap")
    SOFT_BLOCKERS = ("score ", "confidence ", "narrative score ", "only ")

    def _apply_brain(self, out) -> Optional[float]:
        """Give the coin its learned odds, then let the unified engine re-decide with them.

        The brain is a BOUNDED tilt inside the unified score (at most `u_model_tilt` points) and
        only when it is trusted. It can lower a score and it can lift one, but it cannot clear a
        veto, invent evidence or move the risk class. When the unified engine is switched off the
        older behaviour (veto below a minimum, promote past soft blockers) still applies.
        """
        m = self.brain
        if (m is None or not self.s.brain_enabled or out.pair is None
                or out.report.get("lifecycle") == "PRE_MIGRATION_BONDING_CURVE"):
            return None
        try:
            v = self.brain_mod.vector(out.report)
            p = m.predict(v)
            ups, downs = m.reasons(v)
        except Exception:  # noqa: BLE001 - the brain is advisory; research results stand without it
            log.warning("brain could not score %s", out.mint, exc_info=True)
            return None
        base = round(m.positives / m.n, 3) if m.n else None
        info = {"p": round(p, 3), "trusted": m.trusted, "auc": m.auc, "n": m.n, "ups": ups, "downs": downs,
                "base": base, "win_x": self.s.brain_win_x, "loss_x": self.s.brain_loss_x}
        out.report["brain"] = info

        u = out.report.get("unified")
        if isinstance(u, dict) and u.get("version"):
            return self._rescore_unified(out, u, p, info, base)

        if not m.trusted:
            return p
        decision = out.report.get("decision") or {}
        blockers = decision.get("blockers") or []
        if out.action == Action.AUTO_POST and p < self.s.brain_min_probability:
            reason = f"🧠 learned odds {p:.0%} below the learned minimum {self.s.brain_min_probability:.0%}"
            out.action = Action.WATCHLIST
            info["vetoed"] = True
        elif (out.action in (Action.WATCHLIST, Action.REJECT) and blockers and p >= self.s.brain_promote_probability
              and p >= self.s.brain_min_probability and all(b.startswith(self.SOFT_BLOCKERS) for b in blockers)):
            reason = (f"🧠 promoted by the learning brain: {p:.0%} learned odds "
                      f"(≥ {self.s.brain_promote_probability:.0%}); held back only by: " + "; ".join(blockers))
            out.action = Action.AUTO_POST
            info["promoted"] = True
        else:
            return p
        out.reasons = [reason] + list(out.reasons)
        out.report["decision"] = {**decision, "action": out.action.value,
                                  "reasons": [reason] + list(decision.get("reasons") or [])}
        out.report["final_action"] = out.action.value
        out.report["reasons"] = out.reasons
        log.info("brain: %s %s", out.mint, reason)
        return p

    def _refresh_unified(self, out) -> None:
        """Re-run the unified engine now that live context (ticker crowding, heat) is attached.

        v2.4 replaced report["unified"] here but left out.action alone, so the bot could act on
        the pre-refresh decision while the report showed the refreshed one. The action is now
        re-derived from the final unified result in `_finalize` (northstar/final.py).
        """
        if not getattr(self.s, "unified_enabled", True) or not isinstance(out.report.get("unified"), dict):
            return
        if getattr(self, "rug", None) is not None:
            rm = self.rug_mod.score(self.rug, out.report)    # the rug brain's odds (v2.6.0)
            if rm:
                out.report["rug_model"] = rm
        try:
            from .analysis import unified as U
            before = out.report["unified"].get("decision")
            out.report["unified"] = U.evaluate(out.report, self.s)
            after = out.report["unified"].get("decision")
            if before != after:
                self._note(out, f"live context (ticker crowding, market heat) moved this from {before} to {after}")
        except Exception:  # noqa: BLE001
            log.debug("unified refresh failed for %s", out.mint, exc_info=True)

    @staticmethod
    def _note(out, text: str) -> None:
        out.report.setdefault("final_notes", []).append(text)

    def _finalize(self, out) -> None:
        """One source of truth: action, score, risk and confidence from the final unified result,
        written onto the outcome and back into the stored snapshot."""
        from .final import apply_final

        if not apply_final(out, out.report.get("final_notes")):
            return
        out.report["final"]["stage"] = "final"
        if out.snapshot_id and out.pair is not None and out.action.value != "REJECT":
            try:   # a coin rejected at research but lifted later (brain/AI) is always price-tracked
                self.db.schedule_outcomes(out.snapshot_id, out.mint, out.action.value, out.score,
                                          out.pair.price_usd, out.pair.liquidity_usd)
            except Exception:  # noqa: BLE001
                log.debug("could not schedule tracking for %s", out.mint, exc_info=True)
        if out.snapshot_id:
            try:
                self.db.update_research_final(out.snapshot_id, out.action.value, out.score,
                                              out.risk.value, out.confidence.value, out.report)
            except Exception:  # noqa: BLE001 - the decision stands even if the row update fails
                log.warning("could not update the stored snapshot for %s", out.mint, exc_info=True)

    async def _apply_ai_narrative(self, out, force: bool = False) -> Optional[dict]:
        """Ask the AI judge about coins that are already worth calling or watching.

        The AI contributes capped narrative evidence (see unified.apply_ai): it can lift a WATCH
        to a call or talk one down, but it can never clear a veto and a STRONG call still has to
        stand up without most of its narrative points. Coins that are already rejected are never
        sent, so the budget is spent only where the answer can change something.
        """
        u = out.report.get("unified")
        if not isinstance(u, dict) or not u.get("version"):
            return None
        from .analysis import unified as U

        if not getattr(self.judge, "enabled", False):
            return None
        # a manual deep look asks regardless; the automatic path only spends where it can matter
        if not force and u.get("decision") not in (U.STRONG, U.WATCH):
            return None
        curve = out.report.get("lifecycle") == "PRE_MIGRATION_BONDING_CURVE"
        if curve and not force:
            return None          # the early-call path judges those with its own prompt
        ident = out.report.get("identity") or {}
        m = out.report.get("market") or {}
        tr = out.report.get("trades") or {}
        pm = out.report.get("pre_migration") or {}

        def val(x):
            return x.get("value") if isinstance(x, dict) else x

        coin = {"name": ident.get("name"), "symbol": ident.get("symbol"),
                "description": ident.get("description"),
                "socials": ident.get("socials") or "unknown",
                "stage": "pump.fun bonding curve" if curve else "trading on a DEX pool",
                "market_cap_usd": pm.get("market_cap_usd_current") if curve else val(m.get("market_cap")),
                "flow": f"{tr.get('buys', 0)} buys / {tr.get('sells', 0)} sells, "
                        f"{tr.get('unique_buyers', 0)} unique buyers in the sample",
                "ticker_crowding": (out.report.get("context") or {}).get("ticker_crowding_6h")}
        try:
            feed = self.events.snapshot()
            related = self.events.related(ident.get("name"), ident.get("symbol"),
                                          ident.get("description"))
            ai = await self.judge.judge(out.mint, coin, feed, related)
        except Exception:  # noqa: BLE001 - the judge must never break research
            log.warning("AI narrative read failed for %s", out.mint, exc_info=True)
            return None
        try:
            new = U.apply_ai(u, out.report, ai, self.s)
        except Exception:  # noqa: BLE001
            log.warning("could not fold the AI read into the decision for %s", out.mint, exc_info=True)
            return None
        out.report["unified"] = new
        before, after = u.get("decision"), new.get("decision")
        if before == after or not ai:
            return ai
        reason = (f"🤖 AI narrative read ({ai.get('verdict')}"
                  + (f" {ai['score']:.0f}/100" if isinstance(ai.get("score"), (int, float)) else "")
                  + f") moved this from {before} to {after}")
        self._note(out, reason)
        log.info("ai: %s %s", out.mint, reason)
        return ai

    def _rescore_unified(self, out, u: dict, p: float, info: dict, base: Optional[float]) -> float:
        """Re-run the unified decision with the brain's odds folded in as a bounded tilt."""
        from .analysis import unified as U

        m = self.brain
        try:
            new = U.apply_model(u, out.report, p, bool(m and m.trusted), baseline=base,
                                auc=getattr(m, "auc", None), s=self.s)
        except Exception:  # noqa: BLE001
            log.warning("could not fold the brain into the unified decision for %s", out.mint, exc_info=True)
            return p
        out.report["unified"] = new
        info["tilt"] = (new.get("model") or {}).get("tilt")
        before, after = u.get("decision"), new.get("decision")
        if before == after:
            return p
        reason = (f"🧠 learning brain moved this from {before} to {after}: {p:.0%} learned odds "
                  f"vs a {(base or 0) * 100:.0f}% base rate ({info['tilt']:+.1f} points)")
        self._note(out, reason)
        info["moved"] = {"from": before, "to": after}
        log.info("brain: %s %s", out.mint, reason)
        return p

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
        rescan = self.__dict__.get("_rescan")
        if rescan and mint in rescan and getattr(self, "curve_radar", None) is not None:
            rescan.discard(mint)
            try:      # v4.0.0: a rescan judges fresh numbers and an updated pulse pattern
                await self.curve_radar.snapshot([mint])
            except Exception:  # noqa: BLE001
                log.debug("rescan snapshot failed for %s", mint, exc_info=True)
        cand = self.db.get_candidate(mint)
        if not cand or cand["state"] in ("PUBLISHED", "WARNING", "CLOSED", "QUALIFIED", "STALE", "IGNORED", "DROPPED"):
            return
        prev_state = cand["state"]
        self.db.set_state(mint, "RESEARCHING")
        out = await self.researcher.research(mint, cand)
        self.stats["researched"] += 1
        now = time.time()
        if out.skipped_unchanged:
            # nothing changed since the last look: a rejected coin is not looked at again (v2.5.5)
            self.db.set_state(mint, prev_state, next_research_at=None if prev_state == "REJECTED"
                              else now + self.s.rejected_recheck_seconds)
            return
        if out.not_ready:
            # no pool and no trades: there is nothing to decide, so nothing is recorded. A coin
            # known only from the launch feed waits for the curve radar; anything a DexScreener
            # feed or a graduation also found is looked at again on the normal recheck.
            self.stats["not_ready"] = self.stats.get("not_ready", 0) + 1
            try:
                sources = json.loads(cand["sources"] or "[]")
            except (TypeError, ValueError):
                sources = []
            # v3.3.1: a coin known only from the launch feed or the curve radar waits for the radar
            # too. The radar re-queues it the moment it trades in the band again. Before, every coin
            # the radar had ever found was re-checked hourly forever, about one RPC call a second
            # on the live bot (nearly all on dead coins).
            launch_only = sources and all(s in ("pumpportal_new", "curve_radar") for s in sources)
            T.record_outcome(self.db, out, now)
            self.db.set_state(mint, prev_state,
                              next_research_at=None if launch_only
                              else now + self.s.rejected_recheck_seconds)
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
        try:
            from .analysis import context as CTX
            out.report["context"] = CTX.build(self.db, mint, out.report, cand, now, self._heat)
            self._heat.add(out.report, now)
        except Exception:  # noqa: BLE001 - context is optional evidence
            log.debug("context failed for %s", mint, exc_info=True)
        self._refresh_unified(out)
        brain_p = self._apply_brain(out)
        await self._apply_ai_narrative(out)
        self._finalize(out)
        self._brain_paper(out)
        # candidate state must carry the FINAL values, not the pre-brain/AI ones
        common.update(last_action=out.action.value, last_score=out.score, last_risk=out.risk.value,
                      last_confidence=out.confidence.value, reasons=out.reasons[:20])
        early_called = None
        if out.report.get("lifecycle") == "PRE_MIGRATION_BONDING_CURVE" and self.s.early_call_enabled:
            try:
                srcs = json.loads((cand or {}).get("sources") or "[]") if cand else []
                early_called = await self.early.consider(out, sources=srcs)
            except Exception:  # noqa: BLE001 - an early-call problem must never stop research
                log.warning("early-call check failed for %s", mint, exc_info=True)
            if out.report.get("runner") and out.snapshot_id:
                try:      # keep the runner score, attention and blockers with the stored research
                    self.db.update_research_final(out.snapshot_id, out.action.value, out.score, out.risk.value,
                                                  out.confidence.value, out.report)
                except Exception:  # noqa: BLE001
                    log.debug("could not store the runner result for %s", mint, exc_info=True)
            if early_called:
                self.stats["runner_calls"] = self.stats.get("runner_calls", 0) + 1
                self.db.set_state(mint, "PUBLISHED", next_research_at=None, **common)
                return
            # v3.4.0: a high scorer whose blockers can still clear is checked a few more times;
            # everything else is dropped from tracking (the curve watch keeps measuring it)
            try:
                follow = self.early.follow_up(mint, out.report, now)
            except Exception:  # noqa: BLE001
                follow = None
                log.debug("runner follow-up failed for %s", mint, exc_info=True)
            if follow is not None:
                kind, why = follow
                if kind == "RECHECK":
                    self.stats["runner_rechecks"] = self.stats.get("runner_rechecks", 0) + 1
                    row = self.db.fetchone("SELECT checks FROM runner_followups WHERE mint = ?", (mint,))
                    delay = self.early.recheck_delay(row["checks"] if row else 1)
                    log.info("runner %s: rescanning in %ds (%s)", mint, int(delay), why)
                    self.__dict__.setdefault("_rescan", set()).add(mint)
                    self.db.set_state(mint, "WATCHLIST", **{**common, "next_research_at": now + delay})
                else:
                    self.stats["runner_dropped"] = self.stats.get("runner_dropped", 0) + 1
                    log.info("runner %s: dropped from tracking (%s)", mint, why)
                    self.db.set_state(mint, "DROPPED", **{**common, "next_research_at": None,
                                                          "reasons": [f"dropped from tracking: {why}"]
                                                          + list(common.get("reasons") or [])[:19]})
                return
        verdict = None
        if out.action == Action.REJECT and out.pair is not None:
            from .scalp import check as scalp_check
            verdict = scalp_check(out.report, out.pair, self.s, out.report.get("surge"))
        t_shadow = time.time()
        self._record_shadow(mint, out, verdict, out.report.get("surge"), brain_p)
        self._record_shadow_test(out, t_shadow)
        if verdict:
            # Rug with strong momentum: posted ONLY as a clearly labelled scalp with a tight plan.
            out.report["call_type"] = "RUG_SCALP"
            out.report["scalp"] = verdict
            self.stats["scalp"] = self.stats.get("scalp", 0) + 1
            self.db.set_state(mint, "QUALIFIED", next_research_at=None, **common)
            log.info("RUG SCALP %s: %s | rug: %s", mint, verdict["momentum"], "; ".join(verdict["failed_checks"]))
            posted = await self.alerts.publish(out)
            T.record_publish(self.db, mint, posted is not None, getattr(self.alerts, "last_hold", None),
                             out.score, "RUG_SCALP")
            if posted is None:
                self.db.set_state(mint, "REJECTED", next_research_at=now + self.s.rejected_recheck_seconds)
        elif out.action == Action.AUTO_POST:
            self.stats["auto_post"] += 1
            self.db.set_state(mint, "QUALIFIED", next_research_at=None, **common)
            posted = await self.alerts.publish(out)
            T.record_publish(self.db, mint, posted is not None, getattr(self.alerts, "last_hold", None),
                             out.score)
        elif out.action == Action.WATCHLIST:
            self.stats["watchlist"] += 1
            if (out.report.get("pre_migration") or {}).get("qualified_early_watch") and not early_called:
                await self.alerts.publish_early_watch(out)
            # high-potential candidates are re-checked more often, but not forever (v2.5.7): a coin
            # that has not become a call after `watchlist_max_rechecks` looks leaves the watch list
            interval = self.s.watchlist_recheck_seconds / (2 if (out.score or 0) >= 75 else 1)
            again = common["research_count"] < int(getattr(self.s, "watchlist_max_rechecks", 12) or 0)
            # STALE leaves the watch list; a surge or a new listing brings the coin back
            self.db.set_state(mint, "WATCHLIST" if again else "STALE",
                              next_research_at=now + interval if again else None, **common)
        elif out.action == Action.INSUFFICIENT_DATA:
            self.stats["insufficient"] += 1
            # missing data is retried, but not forever (v2.5.5)
            retry = common["research_count"] < int(getattr(self.s, "insufficient_max_rechecks", 8) or 0)
            self.db.set_state(mint, "INSUFFICIENT_DATA",
                              next_research_at=now + self.s.insufficient_data_recheck_seconds if retry else None,
                              **common)
        else:
            self.stats["rejected"] += 1
            # v2.5.5 operator rule: a coin that fails is not followed again, unless it has good
            # numbers, a good narrative and no high holder concentration (tracking_policy.py)
            from .tracking_policy import second_chance
            again, why = second_chance(out.report, self.s)
            again = again and common["research_count"] < int(getattr(self.s, "rejected_max_rechecks", 6) or 0)
            out.report.setdefault("tracking", {})["recheck"] = why if again else f"not re-checked: {why}"
            self.db.set_state(mint, "REJECTED",
                              next_research_at=now + self.s.rejected_recheck_seconds if again else None, **common)
        T.record_outcome(self.db, out, now)
        final = out.report.get("final") or {}
        log.info("decision %s %s score=%s risk=%s class=%s", mint, out.action.value, out.score, out.risk.value,
                 final.get("reason_class"),
                 extra={"ctx": {"mint": mint, "action": out.action.value, "score": out.score,
                                "risk": out.risk.value, "reason_class": final.get("reason_class"),
                                "reasons": out.reasons[:5]}})

    # ---------------------------------------------------------------- manual (dashboard)
    MANUAL_TTL = 900      # a manual call must rest on research from the last 15 minutes

    async def untrack(self, mint: str, reason: str = "", by: str = "dashboard") -> dict:
        """Stop following a coin by hand (db.untrack); undone by `retrack`."""
        from .analysis.identity import is_valid_mint_format

        mint = (mint or "").strip()
        if not is_valid_mint_format(mint):
            return {"ok": False, "error": "That is not a Solana mint address."}
        res = self.db.untrack(mint, reason, by)
        log.info("untracked %s by %s%s: %s", mint, by, f" ({reason})" if reason else "", res)
        return {"ok": True, **res}

    async def retrack(self, mint: str) -> dict:
        ok = self.db.retrack((mint or "").strip())
        return {"ok": ok, "error": None if ok else "That coin was not untracked."}

    async def manual_research(self, mint: str) -> dict:
        """Research one token on request, the same way the bot would, and never post it.

        It runs the full pipeline — security, holders, bubblemap, exit check, unified decision,
        learning brain — and asks the AI judge even when the automatic path would not have, since
        someone asked for the deep look. The result is kept for 15 minutes so it can be called.
        """
        from .analysis.identity import is_valid_mint_format

        mint = (mint or "").strip()
        if not is_valid_mint_format(mint):
            return {"ok": False, "error": "That is not a Solana mint address."}
        if mint in self._manual_busy or mint in self._inflight:
            return {"ok": False, "error": "Already being researched. Try again in a few seconds."}
        self._manual_busy.add(mint)
        self._inflight.add(mint)
        try:
            self.db.upsert_discovery(mint, "manual", 10.0)
            cand = self.db.get_candidate(mint)
            out = await self.researcher.research(mint, cand, force=True)
            now = time.time()
            if not out.not_ready:
                try:
                    from .analysis import context as CTX
                    out.report["context"] = CTX.build(self.db, mint, out.report, cand, now, self._heat)
                except Exception:  # noqa: BLE001 - context is optional evidence
                    log.debug("context failed for %s", mint, exc_info=True)
                self._refresh_unified(out)
                self._apply_brain(out)
                await self._apply_ai_narrative(out, force=True)
                self._finalize(out)
                if out.pair is not None:
                    self.db.set_state(mint, cand["state"] if cand and cand["state"] not in
                                      ("RESEARCHING",) else "WATCHLIST",
                                      next_research_at=now + self.s.watchlist_recheck_seconds,
                                      last_research_at=now, last_action=out.action.value,
                                      last_score=out.score, last_risk=out.risk.value,
                                      last_confidence=out.confidence.value, reasons=out.reasons[:20],
                                      pool_address=out.pair.pair_address,
                                      name=out.pair.base_name, symbol=out.pair.base_symbol)
            self._manual[mint] = (out, now)
            for k in [k for k, (_, ts) in self._manual.items() if now - ts > self.MANUAL_TTL]:
                self._manual.pop(k, None)
            log.info("manual research %s -> %s", mint, out.report.get("final_action") or out.action.value)
            return {"ok": True, "token": self._manual_view(mint, out, now)}
        except Exception as e:  # noqa: BLE001 - report the failure to the panel, keep the bot running
            log.warning("manual research failed for %s", mint, exc_info=True)
            return {"ok": False, "error": f"Research failed: {type(e).__name__}: {str(e)[:160]}"}
        finally:
            self._manual_busy.discard(mint)
            self._inflight.discard(mint)

    def _manual_view(self, mint: str, out, now: float) -> dict:
        """The same shape the dashboard's coin drawer reads from /api/token."""
        ident = out.report.get("identity") or {}
        return {"mint": mint, "state": out.report.get("final_action") or out.action.value,
                "symbol": ident.get("symbol"), "name": ident.get("name"),
                "last_score": out.score, "last_risk": out.risk.value,
                "last_confidence": out.confidence.value, "reasons": out.reasons,
                "sources": ["manual"], "updated_at": now, "report": out.report,
                "snapshot": {"action": out.report.get("final_action") or out.action.value,
                             "score": out.score, "risk": out.risk.value,
                             "confidence": out.confidence.value, "ts": now},
                "manual": {"researched_at": now, "callable": out.pair is not None and not out.not_ready,
                           "expires_in": self.MANUAL_TTL}}

    async def manual_call(self, mint: str, note: str = "", free_copy: bool = False,
                          override: bool = False) -> dict:
        """Post a call the owner decided on, through the same publisher as every other call."""
        entry = self._manual.get((mint or "").strip())
        if not entry or time.time() - entry[1] > self.MANUAL_TTL:
            return {"ok": False, "error": "Research this token first. A call needs research from "
                                          "the last 15 minutes."}
        out, _ = entry
        if out.not_ready or out.pair is None:
            return {"ok": False, "error": "Nothing to call yet: this token has no pool with a usable price."}
        u = out.report.get("unified") if isinstance(out.report.get("unified"), dict) else {}
        vetoes = list(u.get("hard_vetoes") or [])
        if vetoes and not override:
            return {"ok": False, "needs_override": True, "vetoes": vetoes[:3],
                    "error": "Northstar found a hard veto on this token. Confirm to post it anyway."}
        out.report["call_type"] = "MANUAL"
        out.report["manual"] = {"note": (note or "").strip()[:500], "at": time.time(),
                                "override": bool(vetoes), "vetoes": vetoes[:3],
                                "free_copy": bool(free_copy), "decision": u.get("decision")}
        alert_id = await self.alerts.publish(out, manual=True)
        if alert_id is None:
            return {"ok": False, "error": self.alerts.last_hold or "The call was not posted."}
        self._manual.pop(mint, None)                      # one call per research
        self.stats["manual_calls"] = self.stats.get("manual_calls", 0) + 1
        if self.s.learning_enabled:
            try:
                self.learning.record(self.db, mint, out.report, out.pair, out.action.value, "MANUAL", True,
                                     out.report.get("surge"),
                                     brain_p=(out.report.get("brain") or {}).get("p"))
            except Exception:  # noqa: BLE001 - learning must never block a call
                log.warning("could not record manual call for %s", mint, exc_info=True)
        log.info("manual call %s posted as alert %s%s", mint, alert_id,
                 " over a hard veto" if vetoes else "")
        return {"ok": True, "alert_id": alert_id, "dry_run": bool(self.s.dry_run),
                "free_copy": bool(free_copy and self.s.discord_free_channel_id),
                "free_delay_minutes": self.s.free_callout_delay_minutes}

    async def _refresh_attention(self) -> None:
        await self.events.refresh()
        await self.attention.refresh()

    # ---------------------------------------------------------------- v3.5.0 wallet flow
    def _wallet_keys(self) -> list:
        wf = getattr(self, "wallet_flow", None)
        if wf is None or wf.paused:
            return []
        return wf.keys()

    async def _wallet_watch(self) -> None:
        """Every minute: a new UTC day unmutes and unpauses; muted bots and a spent budget unsubscribe."""
        wf, stream = self.wallet_flow, getattr(self, "_pumpportal_stream", None)
        if wf is None or stream is None:
            return
        wf._roll_day(time.time())
        await stream.sync_accounts()
        now = time.time()
        if now - getattr(self, "_flow_logged", 0) >= 1800:
            self._flow_logged = now
            top = wf.top(now, 60, 3)
            log.info("wallet watch: %s; top flow (1h): %s", wf.status(),
                     "; ".join(f"{t['symbol'] or t['mint'][:6]} {t['buyers']} buyers {t['net_sol']:+.1f} SOL"
                               for t in top) or "none")

    async def _twitter_feed(self) -> None:
        """v4.2.0: read the J7 / uxento Discord channel; a posted contract address is researched now."""
        sigs = await self.twitter.poll()
        if not getattr(self.s, "twitter_feed_research", True):
            return
        for sig in sigs:
            await self._on_wallet_signal({**sig, "source": "twitter_feed"})

    async def _on_wallet_signal(self, sig: dict) -> None:
        """Tracked wallets piled into a coin (or the Twitter feed posted its contract): research it now
        (every safety check still applies)."""
        mint = sig["mint"]
        cand = self.db.get_candidate(mint)
        if cand and cand["state"] in ("PUBLISHED", "WARNING", "CLOSED", "QUALIFIED", "IGNORED", "DROPPED"):
            return
        source = sig.get("source") or "wallet_flow"
        key = "twitter_signals" if source == "twitter_feed" else "wallet_signals"
        self.stats[key] = self.stats.get(key, 0) + 1
        log.info("%s %s: %s - researching now", "twitter feed" if source == "twitter_feed" else "wallet flow",
                 mint, sig["why"])
        self.discovery.ingest(mint, source, payload={k: sig.get(k) for k in ("why", "buyers", "buy_sol", "account")
                                                     if sig.get(k) is not None})
        if self.curve_radar is not None:
            await self.curve_radar.snapshot([mint])
        if mint in self._inflight:
            return
        task = asyncio.create_task(self._radar_research(mint))
        self._radar_tasks.add(task)
        task.add_done_callback(self._radar_tasks.discard)

    async def _curve_radar(self) -> None:
        for mint in await self.curve_radar.run_once():
            if mint in self._inflight:
                continue
            task = asyncio.create_task(self._radar_research(mint))
            self._radar_tasks.add(task)
            task.add_done_callback(self._radar_tasks.discard)

    async def _track_early_calls(self) -> None:
        counts = await self.early_calls.run_once()
        if counts["open"]:
            log.info("early-call tracker: %s", counts)

    def _brain_trader(self):
        if getattr(self, "brain_paper", None) is None:
            from .paper import BrainTrader
            self.brain_paper = BrainTrader(self.s, self.db, getattr(self.p, "dex", None))
        return self.brain_paper

    def _brain_paper(self, out) -> None:
        """The learning brain's own paper account: it buys the coins it rates highly (never posts)."""
        try:
            res = self._brain_trader().consider(out)
            if res == "bought":
                b = out.report.get("brain") or {}
                log.info("paper[brain]: bought %s at %.0f%% runner odds", out.mint, 100 * (b.get("p") or 0))
        except Exception:  # noqa: BLE001 - paper trading must never break research
            log.debug("brain paper trade failed for %s", out.mint, exc_info=True)

    # ---------------------------------------------------------------- v3.3.0 always-on learning
    async def _curve_learn(self) -> None:
        """Every 30 min: move the runner score floor toward what real curve outcomes reward."""
        if not getattr(self, "_curve_learn_started", False):
            self._curve_learn_started = True       # first tick at start: let outcomes arrive first
            return
        from . import curve_learn as CL
        change = await asyncio.to_thread(CL.learn, self.db, self.s)
        if change and self.s.learning_announce:
            text = (f"Runner score floor **{change['from']:.0f} → {change['to']:.0f}**, learned from "
                    f"{change['coins']} curve coins that passed every safety check (estimated per trade "
                    f"{CL._pct(change['mean_from'])} → {CL._pct(change['mean_to'])}).")
            try:
                await self.pub.publish({"title": "🧠 Northstar learned something", "description": text,
                                        "color": 0x9B59B6}, self.s.discord_warning_channel_id)
            except Exception:  # noqa: BLE001
                log.debug("could not announce curve learning", exc_info=True)

    async def _cleanup(self) -> None:
        """A minute after start, then daily: training data keeps only real, fully observed outcomes."""
        if not getattr(self, "_cleanup_waited", False):
            self._cleanup_waited = True
            await asyncio.sleep(60)
        from . import cleanup as CU
        c = await asyncio.to_thread(CU.run, self.db)
        if any(v for k, v in c.items() if k not in ("examples_left",)):
            log.info("%s", CU.describe(c))

    async def _watchdog(self) -> None:
        """Self-repair: restart a background loop whose iteration has hung (never one that is just slow
        by design: the limit is the larger of `watchdog_stuck_minutes` and 3x the loop's own interval)."""
        now = time.monotonic()
        limit_min = float(getattr(self.s, "watchdog_stuck_minutes", 15.0)) * 60
        for name, b in list(getattr(self, "_beats", {}).items()):
            if name == "watchdog" or b.get("end") is not None and b["end"] >= b["start"]:
                continue
            limit = max(limit_min, 3 * float(b.get("interval") or 0))
            if now - b["start"] < limit:
                continue
            spec = getattr(self, "_loop_specs", {}).get(name)
            task = next((t for t in getattr(self, "_tasks", []) if getattr(t, "_ns_loop", None) == name), None)
            if spec is None or task is None:
                continue
            log.error("self-repair: loop %s stuck for %.0f min - restarting it", name, (now - b["start"]) / 60)
            self.stats["self_repairs"] = self.stats.get("self_repairs", 0) + 1
            task.cancel()
            new = asyncio.create_task(self._loop(name, spec[0], spec[1]))
            new._ns_loop = name
            self._tasks.append(new)

    async def _paper(self) -> None:
        from .paper import PaperTrader, repair
        if getattr(self, "paper", None) is None:
            self.paper = PaperTrader(self.s, self.db)
            repair(self.db, self.s)              # v3.3.2: a run broken by a bad price quote restarts clean
        counts = self.paper.run_once()
        if counts["opened"] or counts["closed"]:
            log.info("paper trader (calls): %s", counts)
        b = await self._brain_trader().run_once()
        if b["closed"]:
            log.info("paper trader (brain): %s", b)

    async def _track_early_watch(self) -> None:
        counts = await self.early_tracker.run_once()
        if counts["open"]:
            log.info("early-watch tracker: %s", counts)

    async def _prune(self) -> None:
        from . import retention as RT

        res = await asyncio.to_thread(RT.maintain, self.db, self.s)
        if getattr(self, "wallet_flow", None) is not None:
            from . import wallet_flow as WF
            res["wallet_trades_removed"] = await asyncio.to_thread(WF.prune, self.db)
        if getattr(self, "twitter", None) is not None:
            from . import social_feed as SF
            res["twitter_mentions_removed"] = await asyncio.to_thread(SF.prune, self.db)
        if any(v for v in (res.get("pruned") or {}).values()) \
                or any(isinstance(v, int) and v for v in res.values()):
            log.info("database retention: %s", res)

    async def _heartbeat(self) -> None:
        h = {r["provider"]: r["status"] for r in self.db.provider_health()}
        log.info("heartbeat stats=%s providers=%s queue=%d", self.stats, h, self.queue.qsize())
        early = getattr(self, "early", None)
        line = early.runner_summary() if early is not None and hasattr(early, "runner_summary") else None
        if line:
            log.info("%s", line)


def install_signal_handlers(engine: Engine) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, engine.request_stop)
        except (NotImplementedError, RuntimeError):  # Windows: KeyboardInterrupt handled in cli
            pass
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(engine.request_stop))
