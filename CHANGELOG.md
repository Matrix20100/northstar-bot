# Changelog

## 2026-10-18 — full audit

Verification
- Pump.fun bonding-curve trades are decoded from the program's TradeEvent and checked against the trader's token balance (`analysis/pump_trades.py`). Live: ~90% of in-window transactions verified, 0 decode errors.
- NEW: PumpSwap swaps (≈94% of post-migration pools) are decoded from the program's BuyEvent/SellEvent, bound to the selected pool and checked against the trader's token balance (`analysis/pumpswap_trades.py`). Before this, no post-migration swap was ever verified, so demand stayed INFERRED and capped at ~2/20 points. Trade stats are VERIFIED only when at least `MIN_VERIFIED_SWAPS` swaps verify; other DEXes stay INFERRED.
- Creator-dump findings from verified swaps are labelled CONFIRMED/VERIFIED; the post-callout creator-sell warning no longer says "VERIFIED on-chain" for undecoded candidates.
- Pump.fun curves quoted in a token other than SOL (newer curve accounts record a quote mint) are reported as INSUFFICIENT_DATA instead of having their reserves read as SOL.
- Event layouts checked against the official IDLs (pump-fun/pump-public-docs: pump.json, pump_amm.json).

Fixes
- Early-watch posts are tracked and edited every 2 minutes from the on-chain curve (`early_tracker.py`); deleted posts stop being tracked.
- Discord: the publisher retries transient connection failures (e.g. DNS) instead of crashing the process; a rejected token still fails immediately.
- Slash commands: `/check` always failed (defer then send_message), `/why` always failed (selected a non-existent column), `/research` always showed price/liquidity UNKNOWN and never used the AUTO_POST colour. The command client can no longer take the engine down.
- `AlertService.publish` returned an id even when delivery failed; it now returns None until delivered.
- Callout footer again carries "Research signal, not a trade recommendation. Not rug-proof."
- Market structure: "INSUFFICIENT_HISTORY" is reported when the selected pool has fewer than two stored snapshots (was "POTENTIALLY_CONSTRUCTIVE" from provider windows alone).
- Dashboard: token panel showed "$—" for price/liquidity/market cap; small prices rounded to "$0"; DB handle leaked on errors.
- Database retention: discovery events and old research snapshots are pruned hourly (they grew ~150 MB/day with no cleanup).
- Startup/health report `CONFIGURATION PROBLEM` for settings that make posting impossible.

Tests
- Stale fixtures updated to model PumpSwap swaps; one wrong expectation corrected (drawdown 4.85%, not ≥10%).
- New: `test_pumpswap_trades.py`, `test_publisher_start.py`, `test_retention.py`, config-problem tests. 155 tests pass (4 live tests are opt-in); mutation checks confirm each verification guard is covered.

## 1.0.0 — 2026-10-15

Initial standalone build. No prior Northstar repository was available.

- Discovery: DexScreener profiles, community takeovers and boosts; PumpPortal launches and migrations; deduplication by mint.
- Security gates: on-chain mint verification, mint/freeze authorities, Token-2022 extensions (fees, hooks, permanent delegate, default-frozen, non-transferable, pausable), liquidity floors and ratios, liquidity-removal detection, LP lock/burn/bonding/CLMM status, RugCheck cross-checks.
- Research: top-20 holders (raw vs adjusted), on-chain swap sampling, launch-bundle detection, wallet age and funder tracing, linked-wallet clusters with evidence strength, creator history, market structure, narrative intelligence.
- Transparent 100-point score with a separate risk level and confidence; strict decision precedence.
- Discord publishing (outbox pattern, pre-flight re-check, restart recovery), embed edits, milestone and warning alerts.
- Outcome tracking for all decisions, plus a survivorship-aware performance report.
- 69 mock tests and 4 opt-in live tests.

Defects found and fixed during build (each reproduced by a test first):
- Creator check returned no UNKNOWN flag when no creator could be identified, so a missing creator looked clean (`test_missing_creator_history`).
- A blank value copied from `.env.example` (for example `NORTHSTAR_DISCORD_CHANNEL_ID=`) crashed settings loading (`test_env_example_loads_with_blank_values`).

## Unreleased — tracker-style Discord callouts
- Reworked callout embeds to lead with a compact four-cell summary: call market cap, current market cap, performance, and multiplier.
- Added clear Northstar Auto-Callout / Solana attribution and a LIVE label.
- Updated the summary metrics whenever monitoring refreshes the original embed, while retaining detailed market, risk, and data-quality sections below.
- Initial callouts intentionally show +0.0% and 1.0x as the baseline; no gains are fabricated.


## Strict-quality profile update
- Tightened default publication policy to score >= 92, HIGH confidence, LOW_DETECTED risk only, three independent narrative evidence items, and max two alerts/day.
- Raised liquidity/holder/creator/cluster thresholds and require verified LP lock/burn status.
- This is a conservative filter profile, not a claim of 70–80% validated win rate. Validate with recorded outcomes before making performance claims.


## Unreleased — pre-migration incomplete-data path
- Added persisted PumpPortal bonding-curve observations, separate from DEX pool snapshots.
- Routed known PumpPortal launches with no usable DEX pool into a pre-migration research path instead of immediately stopping at `no usable pool`.
- Added a separate early-watch signal based on observed recent trade activity and market-cap-in-SOL trend; it is never represented as a standard low-risk callout.
- Added a validated Pump.fun bonding-curve account decoder and a bounded free-RPC recent transaction sample fallback, so early research does not require metered trade subscriptions when the configured RPC can supply the data.
- Added explicit, disabled-by-default opt-in for metered PumpPortal token-trade subscriptions, with a hard subscription cap.
- Added persistent early-signal entry/current market cap (SOL), peak multiple and minimum multiple tracking.
- Reduced the initial research delay from 10 minutes to 20 seconds; candidates with insufficient evidence remain blocked and are rechecked.

## Discovery reliability hotfix — 2026-10-15

- Added a PumpPortal quiet-stream watchdog: if a connected socket delivers no valid mint events for 180 seconds, the client reconnects and resubscribes instead of staying silently connected forever.
- Added periodic quiet-stream diagnostics, reconnect counts, event counters, last-event age, and heartbeat reporting so an idle feed can be distinguished from a stopped discovery loop.
- Discovery now logs every scan, including scans that return zero new candidates. This makes provider/feed stalls visible in Wispbyte logs.
- Reconnect backoff resets after actual valid events rather than merely after a socket connection, avoiding rapid reconnect loops when the upstream is stale.

## v1.3.0 — Discord slash commands
- Added initial Discord slash-command suite for research, quick check, scan queue, calls, performance, decision explanations, personal watchlist, comparison, deep research, and admin status/diagnostics/settings.
- Commands use existing SQLite research/callout history and configured research providers.
- Admin commands enforce Discord Administrator permissions. Settings are view-only; no runtime threshold changes are applied.
- Added Discord setup documentation and Wispbyte enablement steps.
