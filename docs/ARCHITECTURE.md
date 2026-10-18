# Architecture and implementation plan

## Starting point

No existing Northstar repository was available, so this is a standalone project with clean module boundaries, as the spec allows. If an older Northstar codebase turns up, the integration points are the provider classes (`northstar/providers/`) and the `Publisher` protocol (`northstar/discord_bot/publisher.py`).

## Implementation plan (as executed)

1. Verify real APIs and schemas: DexScreener, RugCheck, Solana RPC, Helius, PumpPortal, CoinGecko. See [DATA_SOURCES.md](DATA_SOURCES.md).
2. Core: typed config, provenance-carrying data model, SQLite with transactions, shared HTTP layer.
3. Discovery from four DexScreener feeds plus the PumpPortal websocket, deduplicated by mint.
4. Security gates: mint account, authorities, Token-2022 extensions, liquidity/LP, RugCheck cross-checks.
5. Research: holders, swap sampling, launch bundles, wallet tracing and clusters, creator history, market structure, narrative.
6. Scoring (100 points), a separate risk level, confidence, and the decision policy.
7. Discord publishing with an outbox pattern, pre-flight re-check and restart recovery.
8. Post-callout monitoring, milestones, warnings and embed edits.
9. Outcome tracking for every decision, plus a performance report.
10. Tests (mock + optional live), docs, packaging.

## Module boundaries

| Layer | Modules | Depends on |
|---|---|---|
| Transport | `net.py` | httpx |
| Providers | `providers/*` | `net`, `models` |
| Pure analysis | `analysis/*` | `models`, `config` (no I/O, except `wallets.sample_trades`, `wallets.trace_wallets` and `creator.analyze_creator`, which take provider objects) |
| Orchestration | `research.py`, `discovery.py` | providers, analysis, `db` |
| Output | `discord_bot/*`, `alerts.py`, `monitor.py` | `db`, `Publisher` protocol |
| Evaluation | `performance.py` | `db`, DexScreener |
| Runtime | `engine.py`, `cli.py` | everything |

Analysis functions take plain inputs and return dataclasses with `flags` and `gates`, which is why the whole decision path can be tested without a network.

## Candidate state machine

```
DISCOVERED ─► RESEARCHING ─► REJECTED ─────────► (re-check only if market data changes; never for invalid/old)
                      │──► INSUFFICIENT_DATA ─► (re-check after 15 min)
                      │──► WATCHLIST ─────────► (re-check every 10 min; 5 min if score ≥ 75)
                      └──► QUALIFIED ─► PUBLISHED ─► WARNING ─► CLOSED (after MAX_TRACKING_HOURS)
                                              └──► STALE (no valid price for 30 min) ─► CLOSED
```

Every transition stores its reasons (`candidates.reasons`), and every research run stores its full evidence report (`research_snapshots.report`).

## Decision precedence

1. Any gate **FAIL** → REJECT. A score of 100 cannot override this.
2. Any critical gate **UNKNOWN** → INSUFFICIENT_DATA. A provider failure is never treated as a clean result.
3. Risk CRITICAL or ELEVATED → REJECT.
4. Score ≥ 85, risk allowed, confidence ≥ MEDIUM, narrative sufficient, not extended, no cooldown, under the daily cap → **AUTO_POST**.
5. Score ≥ 65 → WATCHLIST.
6. Otherwise → REJECT.

The risk level is computed only from findings and gates: CRITICAL if any critical finding or failed gate; ELEVATED for 2+ HIGH; INCOMPLETE if a critical check is unknown; MODERATE for 1 HIGH or 3+ MEDIUM; otherwise LOW_DETECTED. LOW_DETECTED is always displayed with "NOT RUG-PROOF".

## Concurrency and reliability

- One asyncio process: a discovery loop, a scheduler, N research workers, a monitor loop, an outcome loop, an alert-retry loop, and the PumpPortal stream.
- Loops await each iteration before sleeping, so no iteration ever overlaps itself. Exceptions are logged and the loop continues.
- An in-memory queued set plus the `RESEARCHING` state prevents two workers from researching the same mint.
- A file lock prevents two engine processes.
- Every alert row is written as `PENDING` inside the same transaction that checks the cooldown, before Discord is called (outbox pattern). After a crash or timeout, `resume_pending` searches channel history for the mint before deciding whether to send.
- SQLite runs in WAL mode with `BEGIN IMMEDIATE` transactions. A corrupt file is quarantined, never deleted.
- Look-ahead safety: research reads only market history stored *before* the current decision, and outcomes are written to separate tables later.
