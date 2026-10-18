# Fresh Wispbyte deployment

This ZIP includes a root `main.py` for hosts that require a main file.

1. Create a new **Python** server on Wispbyte.
2. Upload and extract `northstar-narrative-chart-engine-v1.2.0.zip` so `main.py`, `pyproject.toml`, `requirements.txt`, and the `northstar/` folder are in the server's project root. Do not leave them nested two folders deep.
3. Set the startup file to `main.py`, or use startup command `python -m northstar run`.
4. Install dependencies from `requirements.txt` (or run `pip install -e .` if the host supports project installs).
5. Add the environment variables from `.env.example` in Wispbyte's environment-variable panel. Do not upload or share a real `.env` file publicly.
6. Set `NORTHSTAR_DRY_RUN=true` first. Configure the Discord token and channel ID, then start the server and inspect startup/provider logs.
7. Verify discovery, data-provider health, research reports and dry-run alerts before setting `NORTHSTAR_DRY_RUN=false`. Do not turn on paid PumpPortal trade subscriptions unless you deliberately accept the provider's metered usage and have completed its account setup.
8. Ensure `NORTHSTAR_DATA_DIR` (or the configured `data_dir`) points to persistent storage. SQLite snapshots are what allow Northstar to analyse its own observed price path across repeated scans.
9. Run `python -m northstar health`, `python -m northstar candidates`, and `python -m northstar report` for diagnostics after startup.

If Wispbyte says it cannot find the main file, check that `main.py` is at the actual server root, not inside `northstar-alpha-engine/northstar-alpha-engine/`.


## Dashboard UI

The web dashboard starts alongside the bot by default. Wispbyte should supply `PORT`; if not, Northstar uses `NORTHSTAR_DASHBOARD_PORT` (default `8080`). Make sure your service exposes that port in its web/port settings. Dashboard startup is logged as `Northstar dashboard listening on ...`. The dashboard is read-only. To disable it, set `NORTHSTAR_DASHBOARD_ENABLED=false`. See `docs/DASHBOARD.md`.
