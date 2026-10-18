# Northstar 1.2.0 — dual-interface UI

- Added a responsive, minimalist dark web dashboard alongside the existing compact Discord alert embeds.
- Dashboard shows candidate states and scores, token detail panels, saved decision reasons, risk flags, explorer links, provider health, tracked alerts, and observed performance summaries.
- Dashboard is read-only and uses the Python standard library; no additional runtime dependency or external frontend CDN is required.
- The dashboard starts with the bot by default and uses the hosting `PORT` when available. It can be disabled with `NORTHSTAR_DASHBOARD_ENABLED=false`.
- Added Wispbyte instructions and endpoint smoke tests.
- Performance UI explicitly warns that observed mark-to-market outcomes are not realized profit and small samples do not prove a reliable win rate.

Validation: Python compile passed; direct HTTP smoke test passed for `/`, `/healthz`, and `/api/overview`. Full pytest could not run in this environment because the existing test dependency `base58` is not installed.
