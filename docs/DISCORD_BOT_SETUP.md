# Creating and inviting the Discord bot

## 1. Create the application and bot

1. Go to https://discord.com/developers/applications and click **New Application**. Name it, for example "Northstar Alpha".
2. Open the **Bot** tab and click **Reset Token**, then copy the token. Put it in `.env` as `NORTHSTAR_DISCORD_TOKEN=...`. Never share it or commit it. If it ever leaks, reset it here.
3. Leave all **Privileged Gateway Intents** off. Northstar only sends and edits its own messages and needs none of them.

## 2. Invite it to your server

1. Open **OAuth2 → URL Generator**.
2. Scopes: tick **bot**.
3. Bot permissions: tick **View Channels**, **Send Messages**, **Embed Links**, and **Read Message History**. Read Message History lets Northstar check, after a restart, whether an alert was already posted, which prevents duplicates.
4. Open the generated URL, pick your Northstar server, and authorize.

## 3. Get the channel ID

1. In Discord, go to **User Settings → Advanced** and turn on **Developer Mode**.
2. Right-click the callout channel and choose **Copy Channel ID**. Put it in `.env` as `NORTHSTAR_DISCORD_CHANNEL_ID=...`.
3. Optional: do the same for a warnings channel and set `NORTHSTAR_DISCORD_WARNING_CHANNEL_ID`.

If the channel is private, add the bot's role to it under **Edit Channel → Permissions**.

## 4. Test

```bash
python -m northstar test-discord
```

You should see a grey "Northstar test message" in the channel. Then set `NORTHSTAR_DRY_RUN=false` and start `python -m northstar run`.

## What gets posted

- **Callout**: one embed per qualified token (see [EXAMPLE_EMBEDS.md](EXAMPLE_EMBEDS.md)).
- **Edits**: the original callout gains a "Performance since callout" section, updated at most every 5 minutes, and immediately on warnings or close.
- **Separate messages** only for milestones (2x, 3x, 5x, 10x) and serious warnings (liquidity collapse, creator selling after the callout, severe drawdown, holder concentration jump).

## Slash commands (v1.3.0)

When the normal `main.py` process starts with `NORTHSTAR_DISCORD_TOKEN` configured and `NORTHSTAR_DISCORD_ENABLED=true`, Northstar starts its slash-command client and syncs these commands:

- `/research token:` runs fresh research for a mint.
- `/check token:` displays the latest saved risk assessment.
- `/scan` shows the latest candidate queue.
- `/calls` lists recent recorded callouts.
- `/performance` shows recorded performance statistics.
- `/why token:` explains the latest saved decision.
- `/watchlist` shows your personal watchlist.
- `/watch_add token:` adds a mint to your personal watchlist.
- `/compare token1: token2:` compares latest saved assessments.
- `/deepresearch token:` runs fresh research (same full research pipeline as `/research`).
- `/northstar_status` (administrator only) shows provider health and command status.
- `/northstar_diagnostics` (administrator only) shows provider errors.
- `/northstar_settings` (administrator only) displays current thresholds (view-only).
- `/northstar_scan` (administrator only) displays the current queue; it does not force a new provider scan.

### Enable them in Discord

1. In the Discord Developer Portal, open your application → **Installation** or **OAuth2 → URL Generator**.
2. Select the `bot` and `applications.commands` scopes. Give the bot **View Channels**, **Send Messages**, and **Embed Links** permissions for the server/channels where it should respond.
3. Use the generated invite URL to add/update the bot in your server. You need permission to manage the server.
4. Set `NORTHSTAR_DISCORD_TOKEN` in Wispbyte's environment variables (or the existing `.env` file). Keep the token secret.
5. Set `NORTHSTAR_DISCORD_ENABLED=true`, save, and restart the server using `main.py`.
6. Wait for the log line saying slash commands were synced. In Discord type `/` and search for `research` or `northstar_status`.
7. If commands do not appear, confirm the `applications.commands` scope is installed, restart once, and inspect the startup logs for command-sync errors.

Commands that query data are ephemeral where appropriate, so private research output is visible only to the command user. Admin commands require Discord Administrator permission. The settings command is intentionally read-only; change thresholds in environment configuration and restart.

**Important implementation note:** the slash-command client currently uses a separate Discord gateway connection from the alert publisher. If your host or Discord configuration does not support both connections reliably, merge the command handlers into the publisher client before production. Do not expose the dashboard publicly without access controls.
