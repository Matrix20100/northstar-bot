# Northstar 1.1.0 — first narrative/chart intelligence pass

- Added narrative thesis, catalyst-evidence list, narrative-risk list, attention direction, and identity-quality status to narrative reports.
- Tightened trending-meta matching: distinctive overlap is preferred; a direct name/slug category match is described as a hypothesis, not a verified catalyst.
- Expanded market structure with same-pool snapshot return, observed drawdown, path volatility, liquidity/market-cap change, buy/sell transaction-count ratio, transaction imbalance, volume acceleration, and entry-quality label.
- Added momentum penalties for sell-dominant transaction counts, large observed drawdown, extreme volume spikes, and market-cap growth unsupported by liquidity growth.
- Updated Discord callouts to show why the token was selected, chart/flow evidence, and data limitations.
- Added synthetic tests and documentation explaining the absence of free OHLCV candles and the limits of snapshot-path analysis.

## Validation status

Python source compiles and focused synthetic market, narrative and embed smoke checks passed in the build environment. The complete pytest suite could not be run here because the build environment lacks `base58` and `discord.py` and network access prevents installing project dependencies. Run the full test suite after installing `pyproject.toml` dependencies on the new host.
