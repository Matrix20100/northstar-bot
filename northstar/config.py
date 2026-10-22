"""Typed configuration loaded from environment variables / .env.

Every threshold here is a starting assumption, not a proven optimum. See docs/CONFIGURATION.md
for the trade-offs of each default.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NORTHSTAR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,  # blank lines copied from .env.example mean "use the default"
    )

    # ---------------------------------------------------------------- Discord
    discord_enabled: bool = True
    discord_token: Optional[SecretStr] = None
    discord_channel_id: Optional[int] = None  # legacy/default channel; use as VIP channel
    discord_free_channel_id: Optional[int] = None  # optional public funnel channel
    free_callout_delay_minutes: int = 15  # VIP gets the call immediately; free receives it later
    free_milestones: str = "2,3,5,10"  # free channel gets selected milestones only
    discord_warning_channel_id: Optional[int] = None  # defaults to main/VIP channel
    dry_run: bool = False  # True = never post; write embeds to data/dry_run_alerts.jsonl

    # ---------------------------------------------------------------- API keys
    helius_api_key: Optional[SecretStr] = None
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    pumpportal_api_key: Optional[SecretStr] = None
    # CoinGecko on-chain API (providers/coingecko.py). Without a key the free GeckoTerminal API is used.
    coingecko_api_key: Optional[SecretStr] = None
    # Web control panel (dashboard.py). Locked until a password is set.
    dashboard_enabled: bool = True
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8080              # Wispbyte's SERVER_PORT / PORT take priority
    dashboard_password: Optional[SecretStr] = None
    coingecko_plan: str = "demo"                 # demo | pro  (decides base URL and auth header)
    coingecko_rpm: int = 25                      # Demo allows 30/min; Pro plans allow far more
    coingecko_daily_budget: int = 300            # Demo = 10,000 calls/month; raise this on Pro
    geckoterminal_rpm: int = 9                   # free keyless API, ~10/min
    enable_coingecko: bool = True
    coingecko_peak_check_seconds: int = 300      # candle-high check per open call
    coingecko_trending_seconds: int = 300        # trending-pools attention feed

    # ---------------------------------------------------------------- Providers
    enable_dexscreener: bool = True
    enable_rugcheck: bool = True
    enable_rpc: bool = True
    enable_pumpportal: bool = True
    # Metered PumpPortal trade stream. Explicit opt-in: 0.01 SOL / 10,000 events.
    enable_pumpportal_trades: bool = False
    max_pumpportal_trade_subscriptions: int = 100
    pumpportal_trade_subscription_ttl_seconds: int = 300
    enable_jupiter: bool = True             # free, keyless: can you actually sell this coin?
    jupiter_rpm: int = 20                   # the keyless lite API throttles well below 50/min
    jupiter_sell_sizes_usd: str = "100,500,1000"
    enable_free_socials: bool = True  # token links, recycled-link check, public Telegram counts (no API key)
    social_reuse_days: int = 7  # lookback for 'same X/Telegram/website used by other tokens'

    # Rate limits (requests per minute). DexScreener documents 60/min for profile/boost/meta
    # endpoints; pair/token endpoints are faster but we stay conservative.
    dexscreener_slow_rpm: int = 50
    dexscreener_fast_rpm: int = 200
    rugcheck_rpm: int = 30
    rpc_rpm: int = 240

    # Daily request budgets (0 = unlimited). Protects free tiers.
    dexscreener_daily_budget: int = 150_000
    rugcheck_daily_budget: int = 10_000
    rpc_daily_budget: int = 200_000

    http_timeout_seconds: float = 15.0
    http_max_retries: int = 4

    # ---------------------------------------------------------------- Scheduling
    discovery_interval_seconds: int = 60
    research_concurrency: int = 4
    monitor_interval_seconds: int = 30  # exit signals need fast checks; moves happen in minutes
    outcome_interval_seconds: int = 120
    watchlist_recheck_seconds: int = 600
    insufficient_data_recheck_seconds: int = 900
    rejected_recheck_seconds: int = 3600  # only re-researched if market data changed
    # v2.5.5 (operator rule): a rejected coin is re-checked only when it has good numbers, a good
    # narrative and no high holder concentration (tracking_policy.py), and at most this many times
    # v2.5.7 launch shape (analysis/launch.py): reject (and stop watching) coins whose start was an
    # instant jump, a few giant coordinated buys, or that have already been dumped
    launch_check_enabled: bool = True
    launch_max_first_jump_x: float = 6.0    # price multiple reached in the first 2 trading minutes
    launch_max_drawdown_x: float = 0.35     # price at or under this share of its high since launch = dumped
    watchlist_max_rechecks: int = 12        # a watched coin is re-checked at most this many times
    rejected_max_rechecks: int = 6
    second_chance_min_liquidity_usd: float = 35_000.0  # "strong liquidity" for a re-check (rug rate fell above ~$33K on 21 Oct paths)
    insufficient_max_rechecks: int = 8      # missing data is retried, but not forever
    reject_tracking_sample_pct: float = 10.0  # % of other rejections still price-tracked 24 h (learning)
    candidate_retention_days: float = 7.0   # finished, unfollowed coins are forgotten after this; 0 = keep
    price_history_detail_hours: float = 24.0   # every price update of a callout is kept this long ...
    price_history_bucket_minutes: float = 5.0  # ... then only the high and low of each bucket; 0 = keep all
    market_snapshot_retention_hours: float = 48.0  # read for the last 24 h only (minimum 25)
    db_auto_shrink: bool = True             # give freed pages back to the disk each retention pass
    db_vacuum_on_start: bool = False        # shrink the file at startup when 20%+ (50 MB+) is free; needs free disk about the size of the data (off: hosting disk limits)
    discovered_max_age_hours: float = 6.0   # never-researched coins drop off the list after this; 0 = keep
    max_queue_size: int = 500
    new_token_initial_delay_seconds: int = 20  # early-stage check; missing data is rechecked
    # Pump.fun emits hundreds of launches an hour and almost all of them die within minutes with
    # no pool and no trades. Researching each one on a timer spends the whole RPC budget to
    # produce INSUFFICIENT DATA. Left false, a new launch waits in the candidate table until the
    # curve radar sees it reach the early band with real buy flow (one cheap DexScreener batch
    # call covers them all), and only then is it researched. True restores the old behaviour.
    research_new_launches_on_timer: bool = False
    min_curve_trade_events: int = 15
    min_curve_unique_buyers: int = 8
    min_curve_buy_sell_ratio: float = 1.35
    min_curve_buy_sol_5m: float = 2.0
    min_curve_early_score: float = 75.0
    max_curve_chase_pct: float = 400.0  # early-watch is not offered once the curve is up more than this since launch
    early_watch_channel: str = "free"  # free | vip | off - where pre-migration early watches are posted
    early_watch_refresh_seconds: int = 30  # how often open early-watch posts are re-read from the bonding curve
    min_verified_swaps: int = 15  # verified PumpSwap swaps needed before post-migration demand counts as VERIFIED

    # ---------------------------------------------------------------- Cheap filters
    max_token_age_hours: float = 72.0  # 0 disables
    prefilter_min_liquidity_usd: float = 5_000.0
    prefilter_min_h1_txns: int = 40

    # ---------------------------------------------------------------- Gates
    min_liquidity_usd: float = 25_000.0
    min_liquidity_to_mcap: float = 0.12
    max_price_age_seconds: int = 300
    allow_active_mint_authority: bool = False
    allow_active_freeze_authority: bool = False
    max_transfer_fee_bps: int = 100  # 1%
    max_top10_adjusted_pct: float = 25.0
    max_single_holder_adjusted_pct: float = 8.0
    max_creator_holding_pct: float = 5.0
    max_cluster_supply_pct: float = 10.0
    max_creator_sell_pct_of_supply: float = 2.0
    liquidity_drop_reject_pct: float = 35.0
    max_price_extension_h6_pct: float = 200.0  # +600% in 6h = extended
    max_source_price_discrepancy_pct: float = 25.0
    require_lp_lock_verified: bool = True
    reject_on_rugcheck_danger: bool = True

    # ---------------------------------------------------------------- Publication policy
    min_opportunity_score: float = 88.0  # a flawless token scores about 89 (social attention is capped at 4 of 25)
    watchlist_min_score: float = 75.0
    min_narrative_score: float = 18.0  # 0-25 scale (fraction of the narrative weight x 25), NOT 0-100
    min_narrative_evidence_items: int = 3
    max_allowed_risk: str = "LOW_DETECTED"  # strict mode: only low detected risk
    min_callout_market_cap: float = 0.0  # 0 = off; e.g. 250000 = only call coins already past $250K MC
    # Surge radar (radar.py): fast DexScreener scan that researches a coin the moment money flows in
    radar_enabled: bool = True
    radar_interval_seconds: int = 30
    radar_lookback_hours: float = 6.0        # watch coins discovered in the last N hours
    radar_max_tokens: int = 300              # 30 per DexScreener request -> 10 requests per scan
    radar_min_market_cap: float = 30_000.0
    radar_max_market_cap: float = 5_000_000.0
    radar_min_liquidity: float = 10_000.0
    radar_min_volume_m5: float = 3_000.0
    radar_volume_spike: float = 2.5          # 5-min volume vs the average 5 min of the rest of the hour
    radar_min_buys_m5: int = 12
    radar_buy_sell_ratio: float = 1.3
    radar_max_change_m5: float = 60.0        # skip if already up more than this in 5 min (vertical)
    radar_max_change_h1: float = 300.0       # skip if the big move already happened this hour
    radar_cooldown_minutes: float = 20.0
    radar_concurrency: int = 2               # surge researches run beside the normal queue
    min_volume_acceleration: float = 0.0  # 0 = off; 1.0 = 1h volume must be at least its 6h hourly average
    min_buy_sell_ratio_h1: float = 0.0    # 0 = off; 1.2 = at least 1.2 buys per sell in the last hour
    block_extended_moves: bool = True     # false = allow calls after a big move (the exit plan manages risk)
    min_confidence: str = "HIGH"  # strict mode requires high evidence confidence
    alert_cooldown_hours: float = 36.0
    max_daily_alerts: int = 2

    # ---------------------------------------------------------------- Score weights (sum 100)
    w_narrative: float = 25
    w_demand: float = 20
    w_liquidity: float = 20
    w_holders: float = 15
    w_momentum: float = 10
    w_wallet_quality: float = 5
    w_evidence: float = 5

    # ------------------------------------------------- Unified decision engine (analysis/unified.py)
    # One score, one risk class, one decision. Category weights sum to 100 and are starting
    # assumptions, NOT fitted coefficients: there is not enough resolved history to fit them
    # (see docs/DECISION_ENGINE.md). Every threshold here is inspectable and overridable by env.
    unified_enabled: bool = True
    u_w_safety: float = 18          # only checks that actually RAN and came back clean earn these
    u_w_liquidity: float = 20       # real depth and what a sell would cost
    u_w_holders: float = 16         # distribution, fresh wallets, connected clusters
    u_w_dev: float = 10             # developer history and behaviour
    u_w_structure: float = 20       # demand quality and price/volume structure
    u_w_narrative: float = 16       # attention, catalysts, AI narrative read
    u_strong_min: float = 68.0      # STRONG OPPORTUNITY needs at least this
    u_watch_min: float = 48.0       # below this it is not worth watching
    u_min_completeness: float = 0.55  # below this the decision cannot be STRONG
    u_model_tilt: float = 12.0      # most the learning brain may move the score, either way
    # at most this many of the points a STRONG call needs may come from narrative. v2.4 shipped
    # 20 while narrative was worth at most 16, so the rule could never bind; 10 makes it real.
    u_narrative_cap: float = 10.0
    u_thin_category_floor: float = 0.5  # coverage below this dampens a category instead of passing it
    # v2.5 (docs/DECISION_ENGINE.md "v2.5"): safety is the veto layer; clean safety checks no
    # longer add score points. true = the v2.4 formula (safety category earns score points).
    u_score_includes_safety: bool = False
    # true = v2.4 behaviour: too few narrative evidence items REJECTS the coin. false (default):
    # it caps the decision at WATCH and lowers confidence — missing optional data is not danger.
    u_narrative_blocks: bool = False
    # exit cost at the largest Jupiter-tested sell size that filled (default sizes 100/500/1000)
    u_exit_impact_warn_pct: float = 5.0    # serious warning: caps the decision at WATCH
    u_exit_impact_block_pct: float = 15.0  # this much impact disqualifies a call
    # price collapse (drops, positive numbers): any one of these marks a dump in progress
    collapse_h1_drop_pct: float = 50.0
    collapse_h6_drop_pct: float = 60.0
    collapse_h24_drop_pct: float = 75.0

    # ---------------------------------------------------------------- Research depth
    swap_sample_signatures: int = 150
    funding_trace_wallets: int = 12
    funding_trace_hops: int = 2             # 2 = also trace who funded the funders (dev -> A -> holder)
    funding_trace_funders: int = 10         # max funders traced on the second hop
    wallet_age_lookups: int = 12
    fresh_wallet_age_hours: float = 24.0

    # ---------------------------------------------------------------- Monitoring
    milestones: str = "1.5,2,3,5,10"
    # Entry / exit plan posted with every call (see trade_plan.py). Multiples are of the call MC.
    trade_plan_enabled: bool = True
    plan_max_entry_pct: float = 25.0       # don't buy more than this % above the call MC
    # Runner plan (tuned for the biggest peaks): bank a third at 2x, a third at 4x, let the last
    # third ride with a wide trailing stop. The learner keeps retuning these from real outcomes.
    plan_tp1: float = 2.0
    plan_tp1_sell_pct: int = 33
    plan_tp2: float = 4.0
    plan_tp2_sell_pct: int = 33
    plan_stop: float = 0.65                # exit at -35%
    plan_trail_pct: float = 40.0           # after TP1: exit if it falls this % from its high
    plan_time_stop_minutes: float = 45.0   # exit if it has not reached plan_time_stop_multiple by then
    plan_time_stop_multiple: float = 1.2
    # Self-learning (learning.py): records every researched coin, scores decisions on real price paths
    # from CoinGecko, and retunes the selection + plan settings automatically.
    learning_enabled: bool = True
    learning_interval_hours: float = 6.0
    learning_min_samples: int = 20
    learning_days: float = 14.0
    learning_announce: bool = False         # post learning changes to Discord (off: they go to the log only)
    # Learning brain: a model trained on Northstar's own history (see northstar/brain.py)
    brain_enabled: bool = True
    brain_days: float = 21.0                # history window the brain learns from
    brain_min_samples: int = 150            # evaluated coins before the brain gets a say in calls
    brain_win_x: float = 2.0                # a "runner" reaches this multiple ...
    brain_loss_x: float = 0.6               # ... before falling to this one
    brain_min_auc: float = 0.58             # walk-forward AUC needed to be trusted (0.50 = coin flip)
    brain_min_probability: float = 0.0      # learned: calls need at least these odds (0 = no veto yet)
    brain_promote_probability: float = 1.01 # learned: odds that can lift a coin held back only by the
                                            # hand-written score/narrative/confidence bars (>1 = off)
    # Shadow testing (shadow_test.py): record what a PROPOSED decision policy would have done next
    # to every live decision, with outcomes, for `python main.py shadow`. Record-only: it never
    # changes a live decision, a callout or a setting.
    shadow_test_enabled: bool = True
    shadow_test_rules: str = "lp_unverified_warn,holder_gate_warn;pool_age_5m;liquidity_50k,pool_age_5m;deep_liquidity"  # ";" = separate policies
    shadow_test_overrides: str = ""         # JSON of unified-engine (u_*) settings to try, e.g. {"u_watch_min": 45}
    shadow_test_dedupe_minutes: float = 30.0
    shadow_test_deep_research: bool = True  # finish analysing coins whose only failed gates the LP rule
                                            # would soften (their live decision stays REJECT)
    radar_learning: bool = True             # record near-surges so the radar thresholds can be learned
    # RUG SCALP calls (scalp.py): momentum coins that FAIL rug checks but can still be traded.
    # Posted with a loud RUG banner and this much tighter plan. Coins that can trap you
    # (mint/freeze authority, transfer fees, LP that can be pulled, liquidity already being pulled)
    # are never posted, not even as a scalp.
    # v2.5.2 (operator decision, 21 Oct): off by default. On the live history (17-21 Oct) rug scalps
    # were 37 of 41 calls and lost about 7% per call on their real price paths with the learned
    # scalp plan (10 wins of 28, worst -97%). true = post them again.
    scalp_enabled: bool = False
    scalp_min_market_cap: float = 50_000.0
    scalp_min_liquidity: float = 15_000.0
    scalp_require_surge: bool = True        # only when the radar sees money flowing in right now
    scalp_max_entry_pct: float = 10.0
    scalp_tp1: float = 1.3
    scalp_tp1_sell_pct: int = 50
    scalp_tp2: float = 1.6
    scalp_tp2_sell_pct: int = 50            # 50 + 50 = all out at TP2: never hold a rug
    scalp_stop: float = 0.85                # -15%
    scalp_trail_pct: float = 20.0
    scalp_time_stop_minutes: float = 10.0
    scalp_time_stop_multiple: float = 1.15
    # ---------------------------------------------------------------- Early narrative calls
    # Bonding-curve coins at 30-50k that ride a LIVE narrative (judged by Claude against launch waves,
    # news, trending searches and Wikipedia). Posted to VIP tagged EARLY; a copy goes to the free
    # channel once a call passes early_call_free_at.
    early_call_enabled: bool = True
    early_call_min_mc: float = 30_000
    early_call_max_mc: float = 50_000
    early_call_min_ai_score: int = 70
    early_call_min_trades: int = 10         # last 5 min, verified pump.fun trades
    early_call_min_buyers: int = 8
    early_call_min_buy_sell_ratio: float = 1.2
    early_call_min_buy_sol_5m: float = 1.0
    early_call_max_per_hour: int = 0        # 0 = no limit (post every coin that qualifies)
    early_call_free_at: float = 1.5         # post a copy to the free channel at this multiple
    early_call_track_hours: float = 24.0
    early_max_entry_pct: float = 20.0
    early_tp1: float = 2.0
    early_tp1_sell_pct: int = 40
    early_tp2: float = 4.0
    early_tp2_sell_pct: int = 30
    early_stop: float = 0.6
    early_trail_pct: float = 40.0
    early_time_stop_minutes: float = 30.0
    early_time_stop_multiple: float = 1.15
    # curve radar: finds bonding-curve coins entering the early-call band
    curve_radar_enabled: bool = True
    curve_radar_seconds: int = 120
    curve_radar_hours: float = 3.0          # look at launches from the last N hours
    curve_radar_max_tokens: int = 2400
    curve_radar_min_buys_m5: int = 12
    # ---------------------------------------------------------------- AI narrative judge (Claude)
    ai_enabled: bool = True                 # active only when a key is set (or provider=local)
    # anthropic | groq | gemini | openrouter | cerebras | mistral | github | local | custom
    # The free ones (groq, gemini, openrouter, cerebras, mistral, github) need no card.
    ai_provider: str = "groq"
    ai_key: Optional[SecretStr] = None              # key for the chosen provider
    anthropic_api_key: Optional[SecretStr] = None   # kept for older .env files
    ai_base_url: str = ""                   # override the provider's default URL (custom/local)
    ai_model: str = ""                      # override the provider's default model
    ai_max_calls_per_hour: int = 120
    ai_daily_budget_usd: float = 1.0
    ai_recheck_minutes: float = 30.0
    ai_price_input_per_mtok: float = 0.0    # for the spend cap; free tiers are 0
    ai_price_output_per_mtok: float = 0.0
    # ---------------------------------------------------------------- live event feeds (free)
    events_news: bool = True
    events_trends: bool = True
    events_wikipedia: bool = True
    events_coingecko: bool = True           # trending coins + hot sectors (uses the CoinGecko key)
    events_news_seconds: int = 600
    liquidity_collapse_pct: float = 50.0
    severe_drawdown_pct: float = 50.0
    max_tracking_hours: float = 72.0
    early_watch_rug_close_x: float = 0.3   # end an early watch once it trades at or under this x of the call price (0 = off)
    embed_edit_min_seconds: int = 300

    # ---------------------------------------------------------------- Storage / logs
    data_dir: str = "data"
    discovery_retention_days: float = 4.0   # 0 = keep forever (the table grows ~70 MB/day)
    snapshot_retention_days: float = 7.0    # research snapshots no alert/outcome still needs
    curve_retention_hours: float = 6.0      # bonding-curve trade events (read for the last 5 minutes only); 0 = 7 days
    snapshot_compact_hours: float = 24.0    # slim prefilter-rejection reports older than this (reasons kept); 0 = off
    tracking_poll_limit: int = 600          # open decision-tracking rows price-checked per outcome pass (round-robin)
    db_filename: str = "northstar.db"
    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("reject_tracking_sample_pct")
    @classmethod
    def _sample_pct(cls, v: float) -> float:
        return min(100.0, max(0.0, float(v)))

    @field_validator("rejected_max_rechecks", "insufficient_max_rechecks")
    @classmethod
    def _rechecks(cls, v: int) -> int:
        return max(0, int(v))

    @field_validator("curve_retention_hours", "snapshot_compact_hours", "discovered_max_age_hours",
                     "candidate_retention_days", "price_history_detail_hours", "price_history_bucket_minutes",
                     "market_snapshot_retention_hours")
    @classmethod
    def _retention_hours(cls, v: float) -> float:
        # a negative age means "off", never "delete everything"
        return max(0.0, float(v))

    @field_validator("early_watch_rug_close_x")
    @classmethod
    def _rug_close(cls, v: float) -> float:
        # 0 (or negative) = off; never at or above the call price, which would close every watch
        return min(0.9, max(0.0, float(v)))

    @field_validator("tracking_poll_limit")
    @classmethod
    def _poll_limit(cls, v: int) -> int:
        # SQLite treats a negative LIMIT as "no limit": one pass would then price-check every open
        # row at once. 0/negative -> the default; capped at 1200 (40 DexScreener requests a pass,
        # shared with research on the 200 rpm pairs limiter).
        v = int(v)
        return 600 if v <= 0 else min(v, 1200)

    @field_validator("max_allowed_risk")
    @classmethod
    def _risk(cls, v: str) -> str:
        v = str(v).strip().upper()  # tolerate stray spaces from .env editing
        if v not in {"LOW_DETECTED", "MODERATE"}:
            raise ValueError("max_allowed_risk must be LOW_DETECTED or MODERATE")
        return v

    @field_validator("coingecko_plan", mode="before")
    @classmethod
    def _cg_plan(cls, v):
        v = str(v or "demo").strip().lower()
        if v not in ("demo", "pro"):
            raise ValueError("coingecko_plan must be demo or pro")
        return v

    @field_validator("early_watch_channel", mode="before")
    @classmethod
    def _early_channel(cls, v):
        v = str(v or "free").strip().lower()
        if v not in ("free", "vip", "off"):
            raise ValueError("early_watch_channel must be free, vip or off")
        return v

    @field_validator("min_confidence")
    @classmethod
    def _conf(cls, v: str) -> str:
        v = str(v).strip().upper()  # tolerate stray spaces from .env editing
        if v not in {"LOW", "MEDIUM", "HIGH"}:
            raise ValueError("min_confidence must be LOW, MEDIUM or HIGH")
        return v

    @property
    def milestone_list(self) -> list[float]:
        return sorted(float(x) for x in self.milestones.split(",") if x.strip())

    @property
    def free_milestone_list(self) -> list[float]:
        return sorted(float(x) for x in self.free_milestones.split(",") if x.strip())

    @property
    def weights(self) -> dict[str, float]:
        return {
            "narrative": self.w_narrative,
            "demand": self.w_demand,
            "liquidity": self.w_liquidity,
            "holders": self.w_holders,
            "momentum": self.w_momentum,
            "wallet_quality": self.w_wallet_quality,
            "evidence": self.w_evidence,
        }

    @property
    def effective_rpc_url(self) -> str:
        """Helius RPC when a key is set, else the configured (public) RPC."""
        if self.helius_api_key:
            return f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key.get_secret_value()}"
        return self.solana_rpc_url

    @property
    def rpc_label(self) -> str:
        """Log-safe RPC description (never contains the key)."""
        return "helius-rpc" if self.helius_api_key else self.solana_rpc_url

    def ai_secret(self) -> str:
        """The key for the chosen AI provider (NORTHSTAR_ANTHROPIC_API_KEY still works for anthropic)."""
        for k in (self.ai_key, self.anthropic_api_key if self.ai_provider == "anthropic" else None):
            if k is not None and k.get_secret_value():
                return k.get_secret_value()
        return ""

    @field_validator("ai_provider", mode="before")
    @classmethod
    def _ai_provider(cls, v):
        from .ai_judge import PRESETS
        v = str(v or "groq").strip().lower()
        if v not in PRESETS:
            raise ValueError(f"ai_provider must be one of: {', '.join(sorted(PRESETS))}")
        return v

    def config_problems(self) -> list[str]:
        """Settings that make part of the pipeline impossible or inconsistent.

        Reported loudly at startup; never auto-corrected, because thresholds are the owner's call.
        """
        problems: list[str] = []
        if self.min_narrative_score > 25:
            needed = self.min_narrative_score * self.w_narrative / 25
            problems.append(
                f"NORTHSTAR_MIN_NARRATIVE_SCORE={self.min_narrative_score:g} is on a 0-25 scale; it requires "
                f"{needed:.1f} narrative points but the narrative component can award at most "
                f"{self.w_narrative:g}. No token can ever AUTO_POST until this is 25 or lower.")
        total = sum(self.weights.values())
        if abs(total - 100) > 1e-6:
            problems.append(f"score weights sum to {total:g}, not 100 (NORTHSTAR_W_*); scores are not on a 100-point scale")
        if self.min_opportunity_score > 100:
            problems.append(f"NORTHSTAR_MIN_OPPORTUNITY_SCORE={self.min_opportunity_score:g} exceeds 100; AUTO_POST is impossible")
        if self.watchlist_min_score > self.min_opportunity_score:
            problems.append("NORTHSTAR_WATCHLIST_MIN_SCORE is above NORTHSTAR_MIN_OPPORTUNITY_SCORE")
        attention_max = 4.0 if self.enable_free_socials else 0.0
        narrative_max = 5 + 7 + 5 + attention_max
        if self.min_narrative_score <= 25 and self.min_narrative_score > narrative_max:
            problems.append(
                f"NORTHSTAR_MIN_NARRATIVE_SCORE={self.min_narrative_score:g} cannot be reached: with the social "
                f"sources enabled the narrative sub-scores add up to at most {narrative_max:g}/25 "
                "(clarity 5 + timeliness 7 + distinctness 5 + attention). No token can AUTO_POST.")
        # a missing AI key is not a configuration problem: it just turns early narrative calls off
        # (the self-test reports it), so only a provider that cannot work at all is flagged here
        if self.ai_provider == "custom" and not self.ai_base_url:
            problems.append("NORTHSTAR_AI_PROVIDER=custom needs NORTHSTAR_AI_BASE_URL")
        if self.trade_plan_enabled:
            from .trade_plan import plan_values, valid_values
            for profile, env in (("standard", "PLAN"), ("scalp", "SCALP")):
                if not valid_values(plan_values(self, profile)):
                    problems.append(f"trade plan levels must satisfy 0 < NORTHSTAR_{env}_STOP < 1 < "
                                    f"1 + {env}_MAX_ENTRY_PCT/100 < {env}_TP1 <= {env}_TP2")
        if self.trade_plan_enabled and max(self.monitor_interval_seconds, self.early_watch_refresh_seconds) > 60:
            problems.append("exit signals are checked only every "
                            f"{max(self.monitor_interval_seconds, self.early_watch_refresh_seconds)}s; "
                            "set NORTHSTAR_MONITOR_INTERVAL_SECONDS and NORTHSTAR_EARLY_WATCH_REFRESH_SECONDS "
                            "to 30 so exits are not posted minutes late")
        if self.min_curve_early_score > 100:
            problems.append(f"NORTHSTAR_MIN_CURVE_EARLY_SCORE={self.min_curve_early_score:g} exceeds 100; early watches are impossible")
        if (self.early_watch_channel == "free" and not self.discord_free_channel_id
                and self.discord_channel_id and self.discord_enabled and not self.dry_run):
            problems.append("NORTHSTAR_EARLY_WATCH_CHANNEL=free but NORTHSTAR_DISCORD_FREE_CHANNEL_ID is not set; "
                            "early watches will not be posted (set the free channel, or use vip/off)")
        return problems

    def secrets(self) -> list[str]:
        out = []
        for s in (self.discord_token, self.helius_api_key, self.pumpportal_api_key,
                  self.coingecko_api_key, self.dashboard_password, self.anthropic_api_key,
                  self.ai_key):
            if s is not None and s.get_secret_value():
                out.append(s.get_secret_value())
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
