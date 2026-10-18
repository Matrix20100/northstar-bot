# Northstar pre-migration / INCOMPLETE fix

## What changed
- Known PumpPortal launches without a usable DexScreener pool now enter a dedicated pre-migration bonding-curve research path instead of stopping at `no usable pool`.
- Added a SQLite `curve_snapshots` table to persist provider-observed launch and trade events, with per-token and time-based retention bounds.
- Added transparent early activity metrics: trade count, unique buyers, buy/sell count ratio, SOL buy flow, and observed market-cap-in-SOL trend.
- Added a free-RPC fallback that samples recent transactions against the bonding-curve account and reads the documented Pump.fun account layout to calculate current market cap in SOL; layout/owner/discriminator are validated before use.
- Added mint-account verification and mint/freeze authority checks before early-watch alerts.
- Early alerts are labelled `PRE-MIGRATION` and `RISK: INCOMPLETE`; they are not standard low-risk calls or automatic buy instructions.
- Added an `early_signals` ledger that tracks entry/current market cap in SOL terms, peak multiple and trough multiple. `python -m northstar report` includes this separate early-watch section.
- Reduced the first research delay from 600 seconds to 20 seconds so new launches are examined earlier.
- Added opt-in PumpPortal token-trade subscriptions with a hard cap and TTL rotation. This feature is disabled by default because PumpPortal meters trade events.
- Kept the regular post-migration DEX research and callout pipeline intact.

## Important configuration
The free PumpPortal launch/migration streams can discover tokens, but reliable rolling trade and market-cap trend metrics require `subscribeTokenTrade` data. That stream is metered by PumpPortal (currently documented as 0.01 SOL per 10,000 events) and requires an API key with a linked wallet. Do not enable it unless you accept that cost.

To opt in, configure:

```env
NORTHSTAR_PUMPPORTAL_API_KEY=your_key
NORTHSTAR_ENABLE_PUMPPORTAL_TRADES=true
NORTHSTAR_MAX_PUMPPORTAL_TRADE_SUBSCRIPTIONS=100
NORTHSTAR_PUMPPORTAL_TRADE_SUBSCRIPTION_TTL_SECONDS=300
```

If trade streaming remains disabled, Northstar attempts a bounded recent-transaction sample and current on-chain curve-state read through the configured RPC. If that evidence is unavailable or incomplete, it keeps the token at `INSUFFICIENT_DATA` rather than inventing current activity or market-cap values.

## Verification
- Python compile check passed.
- Automated tests: 70 passed, 4 skipped. The execution environment did not have `base58` installed, so tests were run with a temporary local compatibility shim; the project still declares `base58` in `requirements.txt` for normal installs.
