# Northstar combined update — 2026-10-15

This package combines the latest available pre-migration research and simplified embed/milestone build with the VIP/free delayed-delivery routing changes.

## Included
- PumpPortal launch/migration discovery and persistent bonding-curve snapshots.
- Separate pre-migration early-watch research path when a Pump.fun launch has no usable DEX pool.
- No fake safety clearance: missing holder/liquidity/creator evidence remains INCOMPLETE; pre-migration alerts are explicitly early-watch only.
- Rechecks for insufficient-data candidates and priority refresh after migration/trade events.
- Simplified Discord callout embeds, market-cap snapshot, live monitoring, and separate milestone messages.
- VIP/main-channel immediate callouts and delayed free-channel copies with configurable delay.
- Separate VIP and free milestone lists.
- SQLite persistence for scheduled free posts so they survive restarts.

## Important limitations
- The win rate is not guaranteed. Measure callout performance from the recorded outcomes before advertising a win-rate claim.
- Free-channel delay is alert routing only. This package does not implement payments, subscription verification, or automatic VIP role grants/revocations.
- PumpPortal trade streaming is metered and remains disabled unless explicitly enabled with the required key. Without trade samples, pre-migration research correctly remains incomplete rather than fabricating activity.
- The app requires correct channel IDs and Discord bot permissions. Do not overwrite your existing `.env` or `data/northstar.db` when updating a deployment.

## Wispbyte update
1. Stop the running bot.
2. Back up the existing `.env` and `data/` folder.
3. Upload/extract this ZIP so the `northstar-alpha-engine/` project files replace the old project files.
4. Keep the existing `.env` and database. Add any missing optional variables from `.env.example` to `.env`.
5. Set `NORTHSTAR_DISCORD_CHANNEL_ID` to the VIP channel, `NORTHSTAR_DISCORD_FREE_CHANNEL_ID` to the public/free channel, and configure the delay and milestone lists.
6. Reinstall dependencies from `requirements.txt` if the host environment changed, then start with `python -m northstar` from the project root.
7. Check logs for the build's PumpPortal connected message, discovery scan completed, and pre-migration research/early-watch messages.

## Diagnosing `no usable pool`
That message can still appear as a pool-selection diagnostic. For a candidate sourced from `pumpportal_new` and not `pumpportal_migration`, the current code should then enter the pre-migration branch. If logs still show only the generic `no usable pool` decision for a PumpPortal launch, verify the deployment actually contains the updated `northstar/research.py` and `northstar/discovery.py`, and that the candidate's stored `sources` includes `pumpportal_new`.
