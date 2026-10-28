# Northstar — working notes for Claude Code

Northstar is the operator's Solana memecoin bot. It discovers new tokens, researches them (on-chain + market
data), posts calls to Discord, follows every call with a trade plan, and runs paper trading. It also has
opt-in real-money trading from a bot-only wallet (test mode by default). A password-protected web dashboard
runs on the same process.

**Start here:** `docs/HANDOFF.md` has the current state, open bugs (with leads) and the operator's backlog.
Older design notes are in `docs/` (ARCHITECTURE, DECISION_ENGINE, LIVE_TRADING, WISPBYTE_SETUP, ...) and
`CHANGELOG.md` (newest first; every version explains why it changed).

## Run and test

```bash
pip install -r requirements.txt          # Python 3.11 (production); 3.13 also works
python main.py                           # = python -m northstar.cli run (needs a .env)
python -m pytest -q                      # offline suite, no network, no keys (~1 min)
node --test tools/vision-forwarder/test/forwarder.test.js   # the Chrome extension's tests
```

Keep pytest's logging plugin on (don't pass `-p no:logging`): the dashboard self-test runs the suite that way, and
it turns a bad log format string into a failure (`tests/test_log_formats.py` guards this).

## Deployment (Wispbyte)

- Host: Wispbyte, Python 3.11, **1 GiB RAM, 1 vCPU, 10 GiB disk**. Keep memory use small.
- On every restart the server **auto-pulls `main`** from GitHub (`git fetch --depth 1 ... && git checkout -f FETCH_HEAD`),
  runs `pip install -U --prefix .local -r requirements.txt`, then `python main.py`. Untracked files on the server survive.
- Secrets live only in the server's `.env` (git-ignored): `NORTHSTAR_DISCORD_TOKEN`, `NORTHSTAR_HELIUS_API_KEY`,
  CoinGecko, PumpPortal and AI keys, and `NORTHSTAR_TRADE_WALLET_PRIVATE_KEY`. The operator edits `.env` himself.
- Dashboard settings are stored in the database and override `.env` (logged as `dashboard settings applied: {...}`).
  Learned settings from the learning loop also override `.env` (logged as `learning: applied learned settings`).
- Data: `data/northstar.db` (SQLite, ~500 MB). `data/tracked_wallets.json` (1,436 Axiom wallets) is on the server only.

## How changes are delivered (operator's rules)

- **Never push to `main`.** Make a branch (`vX.Y.Z-short-name`), push it, open a PR. The operator merges and restarts.
- Bump `VERSION`, add a `CHANGELOG.md` entry that quotes what the operator asked for, and add tests (`tests/test_<topic>_vXYZ.py`).
- Deliver complete, working files. Run the full suite before pushing.
- Talk to the operator in plain language. Money in **USD**. Times: he is in Africa/Johannesburg (UTC+2).

## Hard rules

- **Never fabricate** API data, token history, win rates or test results. No win-rate or profit claims without data
  from the bot's own records; say "too few to tell" when it is.
- Keep **UNKNOWN / FAILED / PASS** distinct everywhere. Unknown is never treated as safe or as good.
- **Never handle secrets.** Never ask for, print, log or commit keys or the wallet private key. Logs are redacted
  (`logging_setup.py`); the dashboard can never show or edit `trade_wallet_private_key`.
- `data/tracked_wallets.json` and any database must never be committed (the repo is **public**).
- Real-money trading stays opt-in: `trade_enabled=False` and `trade_test_mode=True` by default. Claude never runs trades.
- Respect provider rate limits (Helius credits ran out once and took the bot down). Use `net.py` buckets/circuit breakers.

## Code map (`northstar/`)

| Area | Files |
|---|---|
| Entry, loop | `cli.py`, `engine.py` (the main loop: discovery → research queue → decisions → calls → tracking → learning) |
| Settings | `config.py` (pydantic, `NORTHSTAR_` env prefix), `operator_settings.py` (dashboard overrides) |
| Data sources | `providers/` (DexScreener, Solana RPC/Helius, RugCheck, Jupiter, CoinGecko, PumpPortal websocket), `net.py` (rate limits) |
| Discovery | `discovery.py`, `radar.py` (surge radar), `early_calls.py` (curve radar + runner calls + call tracker) |
| Research | `research.py` (one report per coin), `analysis/` (security, holders, launch bundles/snipers, wallets, LP, `unified.py` decision) |
| Rugs | `rugs.py` (confirmed rugs are blocked for good; flags), `analysis/verdict.py`, `rugmodel.py` |
| Calls & Discord | `alerts.py` (standard calls), `early_calls.py` (runner calls), `monitor.py`, `discord_bot/` (embeds, publisher, slash commands incl. VIP roles) |
| Plans & exits | `trade_plan.py` (the plan posted to Discord), `exits.py` (live exit engine), `conviction.py` (A/B/C grade → stop, size, dip-hold) |
| Paper & live | `paper.py` (accounts "calls" and "brain"), `live.py` + `solana_tx.py` (PumpPortal local tx, signed locally) |
| Learning | `brain.py` (runner model), `rugmodel.py` (rug model), `learning.py`, `training_store.py`, `shadow_test.py`, `curve_learn.py`, `teach.py` |
| Dashboard | `dashboard.py` + `dashboard.html` (single file, vanilla JS), `selftest.py` |
| Other | `social_feed.py` (Uxento Vision posts from Discord), `wallet_flow.py`, `retention.py` (DB pruning), `db.py` |

Two decision paths exist and are easy to confuse: the **standard research** path (`research.py` → `analysis/unified.py`,
shown as REJECT/WATCH/... on the dashboard) and the **runner call** path for bonding-curve coins (`early_calls.py`).
A coin can show REJECT in one and still be called by the other.

## Gotchas

- `tests/conftest.py` sets old trade-plan env vars; tests build `Settings(_env_file=None, ...)`.
- `tests/test_commands.py` uses a fake discord module: keep `hasattr` guards in `discord_bot/commands.py`.
- `commands.py` at the repo root is a stray upload (not imported anywhere). The live file is `northstar/discord_bot/commands.py`.
- Pump.fun curve market cap is computed from the curve account (`research.Researcher._parse_pump_curve_account`).
