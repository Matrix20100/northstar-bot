# Troubleshooting

Start with `python -m northstar health`. It shows config problems, database state, a live check of every provider, and recorded provider errors.

| Symptom | Likely cause | Fix |
|---|---|---|
| `NORTHSTAR_DISCORD_TOKEN and ... are required` | Dry-run is off but Discord isn't configured | Fill both values in `.env`, or set `NORTHSTAR_DRY_RUN=true` |
| `another Northstar instance is running` | A second copy was started, or an old process is still alive | Stop the other process. The lock releases automatically when a process exits; if the PC crashed, just start again |
| `Discord login failed: LoginFailure` | Wrong or reset bot token | Reset the token in the developer portal and update `.env` |
| `no access to channel` / `forbidden` in logs | Bot lacks permissions in that channel | Grant View Channel, Send Messages, Embed Links and Read Message History ([DISCORD_BOT_SETUP.md](DISCORD_BOT_SETUP.md)) |
| Almost everything is `INSUFFICIENT_DATA` | Public RPC refusing or rate-limiting (`mint_account` / holder checks UNKNOWN) | Add a free Helius key; lower `NORTHSTAR_RESEARCH_CONCURRENCY` to 2 |
| Many `HTTP 429` warnings | Rate limits too high for your plan | Lower the matching `*_RPM` value. Northstar backs off automatically, but lower limits waste fewer calls |
| `daily budget … exhausted` | Daily budget reached | Raise `*_DAILY_BUDGET` if your plan allows, or reduce `SWAP_SAMPLE_SIGNATURES` / `FUNDING_TRACE_WALLETS` |
| `circuit open after repeated failures` | Provider down; requests paused for 2 minutes | Nothing. It retries automatically. Check the provider's status page if it persists |
| Runs for days with zero callouts | Working as designed when nothing qualifies, or the thresholds are stricter than the market allows | Check `python -m northstar candidates` for the blockers. Do not lower the threshold just to get alerts |
| `pumpportal stream error; reconnecting` | Websocket dropped | Automatic exponential reconnect. Persistent errors may mean a temporary PumpPortal ban (they lift hourly) |
| `database corrupt; quarantined and recreated` | Disk or power issue corrupted the SQLite file | The old file is kept as `northstar.db.corrupt-<time>`. History in it is not migrated automatically |
| Callout price shows `(UNAVAILABLE)` / state STALE | Pool returned no price; last valid price is kept, never zero | Usually a dead or delisted pool. Watch the warnings channel |
| Windows: `Activate.ps1 cannot be loaded` | PowerShell execution policy | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| Windows: Ctrl+C doesn't stop it | Console in Quick Edit mode | Click the window, press Esc, then Ctrl+C |

## Reading a decision

```bash
python -m northstar analyze <MINT>
```

Each score component lists `+` evidence, `-` penalties and `?` missing data. Findings are shown as `[SEVERITY/STATUS]`. A `REJECT` lists the exact gate that failed. `INSUFFICIENT_DATA` lists which critical check could not be resolved. Add `--json` for the full report, which is the same object stored in the database for every decision.

## Logs

JSON lines on stdout. Secrets (Discord token, API keys, `api-key=` URL parameters) are redacted before output. To raise verbosity: `NORTHSTAR_LOG_LEVEL=DEBUG`; for human-readable logs: `NORTHSTAR_LOG_JSON=false`.
