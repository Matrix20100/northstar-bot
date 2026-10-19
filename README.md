# Northstar Alpha Engine

A read-only Solana memecoin research engine. It discovers new tokens, verifies them on-chain, rejects dangerous or poorly supported ones, scores the rest on a transparent 100-point scale, and posts only the strongest candidates to your Discord server. After a callout, it keeps monitoring the token.

> **What this is not.** Northstar does not trade, hold keys, or connect to a wallet. Its callouts are research signals, not trade recommendations. It cannot detect every rug, and nothing it reports is a guarantee. Its scoring weights are starting assumptions, not proven predictors. The built-in performance report exists so you can measure whether they work.

---

## How a token flows through Northstar

```
 discovery feeds ──► candidate (by mint) ──► cheap prefilters ──► on-chain + security gates
 (DexScreener profiles, CTOs,               (pool age, liquidity,   (mint/freeze authority, Token-2022
  boosts; pump.fun launches &                1h activity)            extensions, liquidity, LP, RugCheck,
  migrations via PumpPortal)                                         source consistency)
                                                                               │ any FAIL ──► REJECT (stop: no expensive calls)
                                                                               ▼
       holders (top-20, raw vs adjusted) ─ trade sample (unique buyers, trend, creator sells)
       launch sample (bundles) ─ wallet tracing (age, funders, clusters) ─ creator history
       market structure (extension, fading, snapshots) ─ narrative (metas, free socials, saturation)
                                                                               │
                                                                               ▼
                    100-pt opportunity score  +  separate risk level  +  confidence
                                                                               │
          FAIL gate ► REJECT · critical UNKNOWN ► INSUFFICIENT_DATA · ≥85 & all policy ► AUTO_POST
                                     ≥65 ► WATCHLIST (re-checked) · else REJECT
                                                                               │
                  AUTO_POST ► pre-flight re-check ► Discord embed ► monitoring (milestones,
                  warnings, embed edits) ► outcome tracking for EVERY decision ► `report`
```

Every value carries a status (`VERIFIED`, `OBSERVED`, `INFERRED`, `UNKNOWN`, `STALE`, or `UNAVAILABLE`). UNKNOWN never becomes zero risk, and a provider failure never becomes a clean result.

## How Northstar learns

Northstar is tuned to find the **biggest runners** and keeps getting better at it on its own:

1. **Records** every coin it researches (called or not) with ~45 numbers: money flow, buyers,
   holders, liquidity, chart, radar surge, CoinGecko trending, score parts.
2. **Checks what happened** 4 hours later on 1-minute candles (CoinGecko / GeckoTerminal), and
   follows every decision for 24 hours.
3. **The brain** (`northstar/brain.py`) learns which numbers come before a **runner** (2x before
   -40%). It is judged only on coins it has not seen (walk-forward AUC; 0.50 = coin flip) and gets
   a say in calls only once it is trusted. On the first real history it scored 0.66.
4. **The tuner** (`northstar/learning.py`) replays the trade plan on real paths with worst-case
   fills and 3% fees, and moves a setting one step at a time only when the change also holds up
   on the most recent coins: who gets called, the brain's odds bars, TP/stop/trail/time stop,
   rug-scalp plan, and what the radar counts as a surge (learned from surges *and* near-misses).
5. Every change is posted to Discord with the numbers behind it. `/learning` shows the state,
   `/learning_reset` undoes it.

**Never learned:** the rug and safety checks. A coin that fails or cannot confirm one is never
rescued by the brain.

## Quick start

```bash
# Python 3.11+
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # Windows: copy .env.example .env
python -m northstar health      # checks config, database and every provider live
python -m northstar analyze <MINT_ADDRESS>     # research one token now, never posts
python -m northstar run         # continuous engine (dry-run by default)
```

`.env.example` starts with `NORTHSTAR_DRY_RUN=true`, so nothing posts until you switch it off. In dry-run mode, alerts go to `data/dry_run_alerts.jsonl`. Run it that way for a day or two, read the decisions (`python -m northstar candidates`), and only then set `NORTHSTAR_DRY_RUN=false`.

- Windows step by step: [docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md)
- Creating and inviting the Discord bot: [docs/DISCORD_BOT_SETUP.md](docs/DISCORD_BOT_SETUP.md)
- Running 24/7: [docs/RUNNING_CONTINUOUSLY.md](docs/RUNNING_CONTINUOUSLY.md)

## Commands

| Command | What it does |
|---|---|
| `python -m northstar run` | Continuous engine: discovery, research workers, publishing, monitoring, outcome tracking |
| `python -m northstar health` | Config sanity, DB integrity, live check of each provider, recorded provider health |
| `python -m northstar analyze <MINT> [--json]` | Full research on one mint with evidence, penalties and missing data. Never posts |
| `python -m northstar candidates [--limit N]` | Recent decisions with their first reason |
| `python -m northstar report [--json]` | Historical performance of published, watchlisted and rejected candidates |
| `python -m northstar test-discord` | Posts one clearly labelled test message (not a callout) to check permissions |
| `python -m northstar init-db` | Create or verify the database |

## What you need

| Need | Cost | Without it |
|---|---|---|
| Discord bot token + channel ID | free | Dry-run only |
| Helius API key (free tier) | free | Public RPC: slow, strict limits, may refuse holder queries, which makes many tokens INSUFFICIENT_DATA |
| DexScreener, RugCheck, PumpPortal launch/migration feed | generally free/keyless endpoints where documented | PumpPortal token-trade streaming is metered and opt-in |

## Honest limits (read before trusting a callout)

- **Social coverage is the free checks only:** the token's own X/Telegram/website links, links recycled from other recent tokens, and public Telegram member counts. X posts, followers and engagement are not analysed because there is no free, permitted API, and the embed says so.
- **No candle data.** DexScreener's free API has no OHLC data. Chart structure uses window price changes plus Northstar's own snapshots, so pullback and consolidation detection improves only after a token has been watched a few times.
- **No 15-minute volume.** DexScreener provides m5/h1/h6/h24 only. The 15m field is shown as unavailable rather than invented.
- **Holder data is top-20 only.** Both RPC and RugCheck expose only the 20 largest accounts.
- **Demand and bundle metrics come from samples.** Default: about 150 recent swaps, plus the first 40 swaps for pools under 24h old. Embeds state the sample size.
- **Cluster analysis finds relationships, not identities.** Shared funders can be exchanges. Strength is labelled CONFIRMED, STRONG or WEAK, and nothing claims two wallets are the same person.
- **Default thresholds are guesses.** Use `report` after a few hundred decisions. The engine never changes thresholds on its own.

The full source list, verified endpoints and per-source limitations are in [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).

## Project layout

```
northstar/
  config.py            typed settings (env / .env), all thresholds
  models.py            Datum (value+status+source+ts), states, flags, gates
  net.py               rate limits, cache, coalescing, retries/backoff, budgets, circuit breaker
  db.py                SQLite (WAL, transactions, corruption quarantine, outbox for alerts)
  providers/           dexscreener, rugcheck, solana_rpc, pumpportal, coingecko, socials
  analysis/            identity, security, holders, wallets, creator, market, narrative, scoring, decision
  research.py          per-candidate pipeline (cheap -> expensive)
  discovery.py         multi-feed discovery, dedup by mint
  alerts.py            publishing with duplicate protection + restart recovery
  monitor.py           post-callout tracking, milestones, warnings, embed edits
  performance.py       outcome tracking for all decisions + report
  engine.py            wiring, loops, workers, instance lock, shutdown
  discord_bot/         embeds + publishers (Discord / dry-run)
tests/                 73 tests (69 mock unit/integration + 4 opt-in live)
docs/                  setup, configuration, data sources, troubleshooting, examples
```

## Tests

```bash
pytest -q                                   # mock-based suite (no network)
NORTHSTAR_LIVE_TESTS=1 pytest -m live -q    # optional real-API schema checks; never posts to Discord
```

Mock tests are not live validation. [docs/TESTING.md](docs/TESTING.md) shows what each kind proves.


## Strict callout profile (quality over quantity)

The default profile is intentionally conservative: opportunity score >= 88/100, HIGH evidence confidence, LOW_DETECTED risk only, at least three independent narrative evidence items, >= $25,000 liquidity, >= 12% liquidity/market-cap ratio, verified LP lock/burn status, stricter holder/creator/cluster limits, and at most two alerts per day. Tokens that fail critical checks or have unresolved critical data should not be auto-posted. They may be held for research or rejected.

**A 70–80% win rate is a target to measure, not a guarantee.** These thresholds have not been statistically calibrated on a sufficiently large, out-of-sample dataset. A token can still collapse after a callout, liquidity can disappear, and price quotes do not guarantee executable exits. Define a win before evaluating the system (for example: reaching +20% before hitting -20% within 24 hours, after estimated fees/slippage), log every eligible alert including rejected candidates, and evaluate on chronological holdout data. Do not loosen gates simply to increase alert count.


### Pre-migration research

Northstar supports a separate Pump.fun bonding-curve path for known PumpPortal launches before a
DEX pool exists. Early-watch alerts require recent trade observations and on-chain mint/authority
checks, and are labelled `RISK: INCOMPLETE` because holder/cluster and post-migration liquidity checks
are not yet complete. Metered PumpPortal trade streaming is disabled by default; see
`docs/CONFIGURATION.md` before enabling it.

## Free + VIP Discord callout channels

Set the existing `NORTHSTAR_DISCORD_CHANNEL_ID` to your **VIP channel**. VIP receives each qualifying call immediately. Optionally set `NORTHSTAR_DISCORD_FREE_CHANNEL_ID` to your public/free channel; it receives a delayed copy after `NORTHSTAR_FREE_CALLOUT_DELAY_MINUTES` (default 15). The delay is stored in SQLite so a restart does not lose scheduled free posts. Free milestone updates are limited to `NORTHSTAR_FREE_MILESTONES` (default `2,3,5,10`); VIP receives every milestone in `NORTHSTAR_MILESTONES` (default `1.5,2,3,5,10`).

Example `.env` settings:

```env
NORTHSTAR_DISCORD_CHANNEL_ID=YOUR_VIP_CHANNEL_ID
NORTHSTAR_DISCORD_FREE_CHANNEL_ID=YOUR_FREE_CHANNEL_ID
NORTHSTAR_FREE_CALLOUT_DELAY_MINUTES=15
NORTHSTAR_MILESTONES=1.5,2,3,5,10
NORTHSTAR_FREE_MILESTONES=2,3,5,10
```

The bot account must have permission to view and send embeds in both channels. This routes callouts and milestones; it does **not** verify SOL payments, manage subscriptions, or grant/revoke Discord roles. Those should be connected through a separate verified payment/role workflow rather than trusting a screenshot or a user's claim of payment.


## Web dashboard

Northstar also includes a read-only, responsive dark dashboard alongside the compact Discord embeds. It starts automatically with the bot on the hosting `PORT` (or `NORTHSTAR_DASHBOARD_PORT`, default `8080`). See [`docs/DASHBOARD.md`](docs/DASHBOARD.md) for setup and privacy notes. Set `NORTHSTAR_DASHBOARD_ENABLED=false` to disable it.
