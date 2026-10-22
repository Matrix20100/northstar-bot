# Configuration and trade-offs

Every setting is an environment variable with the `NORTHSTAR_` prefix, read from the process environment or `.env`. Blank values mean "use the default". None of the defaults is proven optimal; each one is a starting point. Change one at a time and measure with `python -m northstar report`.

## Publication policy

| Setting | Default | Trade-off |
|---|---|---|
| `MIN_OPPORTUNITY_SCORE` | 88 | Higher means fewer, better-supported alerts. A flawless token scores about 89, because social attention is capped at 4 of the 25 narrative points. |
| `WATCHLIST_MIN_SCORE` | 65 | Watchlist tokens are re-researched every `WATCHLIST_RECHECK_SECONDS` (twice as often at ≥75). |
| `MAX_ALLOWED_RISK` | MODERATE | `LOW_DETECTED` is stricter: one HIGH finding then blocks posting. |
| `MIN_CONFIDENCE` | MEDIUM | `HIGH` requires ≥80% evidence completeness plus narrative confidence. |
| `MIN_NARRATIVE_SCORE` / `MIN_NARRATIVE_EVIDENCE_ITEMS` | 18 / 3 | Blocks tokens with momentum but no narrative evidence. **`MIN_NARRATIVE_SCORE` is on a 0–25 scale** (it is multiplied by `W_NARRATIVE / 25`); a value above 25 makes AUTO_POST impossible and is reported at startup as a CONFIGURATION PROBLEM. Narrative evidence items come from a trending-meta match, a distinct ticker, and a Telegram with 500+ public members. |
| `ALERT_COOLDOWN_HOURS` | 24 | Prevents re-alerting the same mint. |
| `MAX_DAILY_ALERTS` | 5 | A cap, never a quota. There is no minimum, and thresholds never drop because the bot has been quiet. |

## Mandatory gates (failure = REJECT, regardless of score)

| Setting | Default | Notes |
|---|---|---|
| `MIN_LIQUIDITY_USD` | 15,000 | Absolute floor. |
| `MIN_LIQUIDITY_TO_MCAP` | 0.08 | Scales with token size, so a $5M token with $20k liquidity fails. |
| `ALLOW_ACTIVE_MINT_AUTHORITY` / `ALLOW_ACTIVE_FREEZE_AUTHORITY` | false | When set to true, an active authority becomes a HIGH flag instead of a gate failure. |
| `MAX_TRANSFER_FEE_BPS` | 100 (1%) | The worst of the current and scheduled Token-2022 fee is used. |
| `MAX_TOP10_ADJUSTED_PCT` | 40 | Adjusted = excludes verified pool vaults, burn addresses and known infrastructure. |
| `MAX_SINGLE_HOLDER_ADJUSTED_PCT` | 12 | |
| `MAX_CREATOR_HOLDING_PCT` | 10 | |
| `MAX_CLUSTER_SUPPLY_PCT` | 20 | Applies to CONFIRMED/STRONG linked-wallet clusters and launch-slot bundles. |
| `MAX_CREATOR_SELL_PCT_OF_SUPPLY` | 2 | Measured within the swap sample. |
| `LIQUIDITY_DROP_REJECT_PCT` | 35 | Versus the highest liquidity Northstar has observed for that pool. |
| `MAX_PRICE_AGE_SECONDS` | 300 | Price must be fresh and the pool must have recent trades. |
| `MAX_SOURCE_PRICE_DISCREPANCY_PCT` | 25 | DexScreener vs RugCheck price disagreement. |
| `REJECT_ON_RUGCHECK_DANGER` | true | Any RugCheck `danger`-level risk rejects the token. |
| `REQUIRE_LP_LOCK_VERIFIED` | false | When true, unknown LP status blocks posting. Many legitimate pump.fun and CLMM pools would then never qualify. |

Gates whose data is unavailable become UNKNOWN. A critical UNKNOWN leads to INSUFFICIENT_DATA and a later re-check, never to a pass.

## Cheap prefilters (run before any expensive call)

`MAX_TOKEN_AGE_HOURS` (72, 0 disables), `PREFILTER_MIN_LIQUIDITY_USD` (5,000), `PREFILTER_MIN_H1_TXNS` (20). A rejected token is only re-researched when its market data changes.

## Score weights (sum 100)

Narrative 25, demand 20, liquidity 20, holders 15, momentum 10, wallet quality 5, evidence 5. Each component lists its evidence, penalties and missing data. Unavailable data earns zero points in its component; it is never averaged away.

## Cost controls

Per-provider requests/minute (`*_RPM`) and daily budgets (`*_DAILY_BUDGET`). Research depth: `SWAP_SAMPLE_SIGNATURES` (150), `FUNDING_TRACE_WALLETS` (12). On the public RPC, one full research costs roughly 200–300 RPC calls. Lower these or add a Helius key if you see `BudgetExceeded` or many 429s.

## Monitoring

`MILESTONES` (2,3,5,10), `LIQUIDITY_COLLAPSE_PCT` (50), `SEVERE_DRAWDOWN_PCT` (50), `MAX_TRACKING_HOURS` (72), `EMBED_EDIT_MIN_SECONDS` (300; warnings and closes edit immediately).


## Pre-migration Pump.fun research (new)

Northstar now keeps a separate PumpPortal bonding-curve observation stream and routes known
PumpPortal launches without a DEX pool through an early-stage analyzer. It does not invent USD
market cap or curve graduation percentage. Early-watch posts are explicitly labelled
`PRE-MIGRATION` and `RISK: INCOMPLETE`; holder concentration, wallet clusters, and post-migration
liquidity/LP status are not represented as cleared. This is not a standard low-risk callout.

| Setting | Default | Notes |
|---|---:|---|
| `NEW_TOKEN_INITIAL_DELAY_SECONDS` | 20 | First research attempt after launch; no longer waits 10 minutes. |
| `MIN_CURVE_TRADE_EVENTS` | 15 | Minimum observed buys/sells in the rolling 5-minute sample. |
| `MIN_CURVE_UNIQUE_BUYERS` | 8 | Distinct observed buyer wallets in that sample. |
| `MIN_CURVE_BUY_SELL_RATIO` | 1.35 | Buy-count / sell-count floor. |
| `MIN_CURVE_BUY_SOL_5M` | 2.0 | Observed buy-side SOL flow in the rolling sample. |
| `MIN_CURVE_EARLY_SCORE` | 75 | Activity heuristic threshold; not a validated prediction. |
| `ENABLE_PUMPPORTAL_TRADES` | false | Explicit opt-in to metered PumpPortal trade streaming. |
| `MAX_PUMPPORTAL_TRADE_SUBSCRIPTIONS` | 100 | Hard cap on simultaneously tracked launches. |
| `PUMPPORTAL_TRADE_SUBSCRIPTION_TTL_SECONDS` | 300 | Rotate out old token subscriptions to keep the feed focused on recent launches. |

**Important cost/data limitation:** PumpPortal's `subscribeNewToken` and `subscribeMigration` feeds are
free, but live `subscribeTokenTrade` messages are metered (currently documented as 0.01 SOL per
10,000 messages and require an API key with a linked wallet). With metered streaming disabled, Northstar
first tries a bounded recent-transaction sample from the configured Solana RPC and reads the documented
Pump.fun bonding-curve account layout for current market cap in SOL. That fallback depends on the RPC
provider returning recent signatures and transaction data within its rate limits. If the curve account
layout, current reserves, or trade sample cannot be verified, the candidate remains `INSUFFICIENT_DATA`.
Do not enable the metered stream unless you accept the cost. Verify current terms with PumpPortal.


## Verification, tracking and storage (added 2026-10-18)

| Setting | Default | Notes |
|---|---|---|
| `MIN_VERIFIED_SWAPS` | 15 | Post-migration demand counts as VERIFIED only when at least this many swaps in the sample were decoded from PumpSwap swap events and matched to the trader's token balance. Otherwise the sample stays INFERRED (demand earns at most ~2/20 points). Other DEXes (Meteora, Raydium, Orca) are not decoded and stay INFERRED. |
| `MAX_CURVE_CHASE_PCT` | 400 | Early watches are not offered once the bonding-curve market cap is up more than this since launch. |
| `EARLY_WATCH_REFRESH_SECONDS` | 120 | How often open early-watch posts are re-read from the bonding curve and edited. |
| `DISCOVERY_RETENTION_DAYS` | 4 | Discovery events older than this are deleted unless the mint has an alert or early-watch record. 0 keeps everything (~70 MB/day). |
| `SNAPSHOT_RETENTION_DAYS` | 7 | Research snapshots older than this are deleted unless an alert, a pending outcome or open tracking needs them; the latest snapshot per mint is always kept. |
| `CURVE_RETENTION_HOURS` | 6 | Bonding-curve trade events older than this are deleted (research reads only the last 5 minutes). 0 = keep the old 7-day backstop only. Negative = 0. |
| `SNAPSHOT_COMPACT_HOURS` | 24 | Prefilter-rejection reports older than this lose their provider metadata; reasons, decision and every market number are kept. 0 = off. Negative = 0. |
| `TRACKING_POLL_LIMIT` | 600 | Open decisions price-checked per outcome pass (called coins first, then never-checked, then least recently checked). 1–1200; 0 or negative = 600. See docs/DATABASE.md. |
| `REJECTED_MAX_RECHECKS` | 6 | A rejected coin is re-checked only with good numbers, a good narrative and no high holder concentration (tracking_policy.py), and at most this many times. 0 = never. |
| `INSUFFICIENT_MAX_RECHECKS` | 8 | Coins with missing data are retried at most this many times. |
| `REJECT_TRACKING_SAMPLE_PCT` | 10 | % of the other rejected coins still price-tracked for 24 h, so false rejections can be measured. Calls and watches are always tracked. 0–100. |
| `DISCOVERED_MAX_AGE_HOURS` | 6 | Coins never researched leave the watch list after this; new information brings them back. 0 = keep. |
| `SHADOW_TEST_RULES` | 3 policies | Proposals recorded next to live decisions (`python main.py shadow`), separated by `;`. Default: `lp_unverified_warn,holder_gate_warn;deep_liquidity;deep_liquidity,pool_age_1h`. |

At startup Northstar logs `CONFIGURATION PROBLEM:` for settings that make posting impossible (narrative threshold above 25, weights not summing to 100, scores above 100). `python -m northstar health` reports the same list.
