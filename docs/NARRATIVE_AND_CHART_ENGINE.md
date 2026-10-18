# Northstar 1.1 — Narrative and market-evidence upgrade

## What the engine now tries to answer

Northstar keeps three questions separate:

1. **Narrative hypothesis:** What theme does the name/profile suggest, is there a distinctive connection to a tracked trending meta, is there independent Reddit attention, and how crowded is the ticker/name space?
2. **Market evidence:** Does the available price-change, volume and transaction-count data support the thesis, and do Northstar's stored observations show extension, fading, drawdown, consolidation, or market-cap growth unsupported by liquidity?
3. **Risk and decision:** Do the existing security/holder/creator/wallet gates permit further consideration? A strong narrative or chart never overrides a failed risk gate or critical unknown.

## Narrative data and limitations

- Reads token name, symbol, DexScreener description, listed websites/social links, tracked trending-meta data, competing same-ticker/name tokens, and Reddit results when configured.
- Distinctive meta-word overlap is required before describing a possible trend match; generic overlap such as only `AI`, `meme`, `dog`, or `solana` is not enough.
- Produces a transparent narrative hypothesis, evidence items, attention direction, identity quality and narrative risks.
- Paid boosts are explicitly promotion, not organic demand.
- This is evidence-based rules analysis, not a general-purpose language model. It does not claim to verify claims in a description, prove a catalyst, or analyse X/Telegram unless a working provider is configured. A missing social feed is not proof of no community.

## Market / chart evidence

DexScreener's public API supplies price-change windows, volume windows and transaction counts, but does **not** provide a free OHLCV candle endpoint. Northstar therefore:

- uses provider 5m/1h/6h/24h price-change and volume windows;
- compares 1h transaction counts (buys versus sells, not unique wallets);
- computes return, observed maximum drawdown, recent path volatility, pullback and liquidity/market-cap changes from Northstar's own timestamped observations of the same pool;
- ignores observations from other pools;
- labels thin history as insufficient instead of inventing candle patterns;
- flags large extensions, sharp fading after a pump, vertical moves on relatively thin liquidity, and market-cap expansion not matched by liquidity expansion.

Snapshot-path measurements are **not candles** and cannot reveal intraperiod highs/lows between polls. They can miss short spikes and fast liquidity removals. A displayed drawdown is the largest drawdown observed between saved snapshots, not the true maximum drawdown.

## Decision policy

- Existing hard risk gates remain higher priority than opportunity score.
- Missing critical evidence blocks automatic publication.
- A high score cannot cancel critical/high risk, unsupported pool identity, or incomplete data.
- Chart/flow indicators are descriptive evidence, not a guarantee of future return.
- No win-rate claim is made. Thresholds require forward-testing against timestamped decisions and outcomes before any performance claim is credible.

## Safe deployment / testing

1. Keep `DRY_RUN=true` while validating provider responses and embed formatting.
2. Start with an empty or copied database only if historical snapshots are intentionally preserved; old snapshots improve path analysis but are not live proof of safety.
3. Confirm the bot's `data_dir` is persistent on the host, otherwise snapshot history resets after restart.
4. Check that every published report includes a valid mint and pool, fresh price/liquidity, risk gates, narrative evidence and a chart-history note.
5. Run the full test suite in the host environment after installing `pyproject.toml` dependencies. A local compile check alone is not a complete test.
