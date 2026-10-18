# Northstar web dashboard

Northstar now provides two interfaces:

- **Discord embeds:** compact alert cards for CALL/WATCH/REJECT/INSUFFICIENT DATA decisions, including the evidence and risk context the engine has actually collected.
- **Web dashboard:** a dark, minimal, responsive, read-only research terminal with candidate filters, token detail panels, decision reasoning, risk flags, explorer links, provider status, alert monitoring and observed performance summaries.

The dashboard uses Python's standard library only. No frontend package install, external CDN, API key or separate web framework is required. It reads the existing SQLite database and never signs transactions or trades.

## Wispbyte

The dashboard starts automatically with `python main.py` unless disabled. Wispbyte should expose the port assigned by its environment (`PORT`); the app checks `PORT` first, then `NORTHSTAR_DASHBOARD_PORT`, then defaults to `8080`.

Set these environment variables if needed:

```env
NORTHSTAR_DASHBOARD_ENABLED=true
NORTHSTAR_DASHBOARD_HOST=0.0.0.0
NORTHSTAR_DASHBOARD_PORT=8080
```

Open the web address/port shown by your Wispbyte service panel. The dashboard health endpoint is `/healthz`.

To disable the web server while leaving Discord callouts running, set `NORTHSTAR_DASHBOARD_ENABLED=false`.

For local development, run the standalone dashboard with `python -m northstar dashboard --port 8080`. Do not run a second dashboard process on the same port while the main bot has its dashboard enabled.

## Notes

- Dashboard is read-only; it cannot buy, sell, sign, or submit transactions.
- The dashboard displays only information saved by the engine. Empty panels mean the database has not recorded those data yet; they are not interpreted as safe/positive signals.
- Performance figures are observed mark-to-market measurements, not realized P&L or a guarantee of future accuracy. Low sample counts are explicitly cautioned against.
- The dashboard is intended for a private hosting URL. If you expose it publicly, restrict access using Wispbyte's access controls or a reverse proxy; it is read-only but contains your research and alert history.
