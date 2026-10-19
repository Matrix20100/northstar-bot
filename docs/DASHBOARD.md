# Northstar control panel

A password-protected web dashboard served by the bot itself (stdlib only: no extra packages,
no CDN, nothing to install). Open your server's **Web URL** in a browser (on Wispbyte:
Console page → Server details → Web URL).

## Setup

```env
NORTHSTAR_DASHBOARD_PASSWORD=pick-a-long-password
```

Restart the bot. Without a password the panel stays **locked** (it shows how to unlock it and
serves no data), because the server sits on a public address.

The port is taken from Wispbyte's `SERVER_PORT` (or `PORT`), then `NORTHSTAR_DASHBOARD_PORT`,
then 8080. `NORTHSTAR_DASHBOARD_ENABLED=false` turns it off.

## Tabs

- **Overview** – bot running/uptime, live/dry-run, research in the last hour, calls in 24 h,
  the learning brain's state, latest decisions with reasons, decision mix, data-source health.
- **Watching** – the research queue (what is waiting and why it was found), radar surges and
  near-misses (last 6 h), CoinGecko trending coins, early watches.
- **Calls** – every VIP call and rug scalp: called-at and current market cap, peak, low, plan
  events (TP1, TP2, trailing exit, stop, time stop, danger) and status.
- **Learning** – the brain: trusted or not, accuracy on unseen coins, which signals push runner
  odds up or down; what it has checked; every tunable setting with its learned value, your
  .env value and the range it may try; the full change log.
- **Settings** – every setting, grouped and searchable, marked *learned*, *from .env* or
  *default*. Keys and tokens only ever show "set" / "not set".
- **Self-test** – one button checks settings, Discord login and every channel, DexScreener,
  RugCheck, Solana RPC, CoinGecko, the database, the running bot, the learning, and (optional)
  runs the full code test suite. Nothing is posted, nothing is changed. Same checks from a
  shell: `python main.py selftest --code`.

Click any coin to open its research: market numbers, learned runner odds and why, the reasons
for the decision, every safety check, and links to DexScreener, Axiom, RugCheck and Solscan.

## Security

- Session cookie is HttpOnly and SameSite=Strict; cross-site posts are refused.
- 5 wrong passwords from one address lock that address out for 10 minutes.
- The database is opened read-only; the panel cannot trade, sign, post or change settings.
- Wispbyte's Web URL is plain `http://`, so the password travels unencrypted. Use a password
  you don't use anywhere else.
