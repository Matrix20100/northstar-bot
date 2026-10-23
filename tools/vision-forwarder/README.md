# Northstar Vision Forwarder

A small Chrome extension that forwards **every post shown in your Uxento Vision panels**
(`app.uxento.io/vision`) to a Discord channel. The Northstar bot already reads tracker channels
(`NORTHSTAR_TWITTER_FEED_CHANNEL_IDS`, `northstar/social_feed.py`), so forwarded posts reach the
existing brain with no new pipeline. A contract address in a post is researched at once, and X
accounts, $TICKERs and repeated words count towards a coin's attention score.

## Why an extension (what was checked, 2026-10-23)

- Uxento has no public API, webhook or Discord export for Vision.
- Vision posts arrive in the page over Ably (`main.realtime.ably.net`). The `messages?...` requests
  are Ably channel **history** (`/channels/<name>/messages`), not a separate API.
- **X Tracker posts are encrypted in transit** (`CT_Tracker5`, AES with `iv` / `tag` / `data`) and
  only Uxento's page decrypts them. Decrypting them anywhere else would mean pulling Uxento's key out
  of its code and getting around a protection Uxento chose, so this tool does not do that.
- The raw `PumpCallouts` channel carries every callout (~100 a minute). Vision shows only those
  matching your filters.

So the forwarder reads what your own Vision page already shows. Each Vision panel is a
react-virtuoso list, and the list component holds the panel's full post list, already decrypted and
filtered. The forwarder reads that list, so posts scrolled out of view or arriving in a burst are not
missed. If that list ever cannot be reached (an Uxento update), it falls back to reading the cards on
screen. The popup then shows "screen fallback".

Feeds verified live: **X Tracker** (tweets, replies, quotes, retweets, pins, deletes, follow and
unfollow updates), **Fomo Trades** (buy/sell, USD amount, caller, coin) and **Fomo Thesis** (caller
text, coin). Any other panel you add to Vision is read the same way. An item of an unfamiliar shape
is still forwarded, with its text fields, and tagged "unrecognised post type".

## Install (Chrome, on the computer that keeps Vision open)

1. Get the folder `tools/vision-forwarder` from the repo, for example by downloading the repo ZIP
   from GitHub and unzipping it.
2. Chrome → `chrome://extensions` → switch on **Developer mode** (top right) → **Load unpacked** →
   choose the `vision-forwarder` folder.
3. In Discord: channel settings → **Integrations → Webhooks → New Webhook** in the channel the bot
   reads (or a new channel) → **Copy Webhook URL**.
4. Click the extension's icon → paste the URL → **Save** → **Send test post**.
5. Open `https://app.uxento.io/vision` and leave the tab open. The popup lists each panel and how
   many posts it sees.

Optional: send a panel to its own channel with one line per panel in "Per-feed webhooks", e.g.
`Fomo Trades = https://discord.com/api/webhooks/...`. Panel names are the titles shown in Vision.

## Northstar settings (Wispbyte)

```
NORTHSTAR_TWITTER_FEED_CHANNEL_IDS=<the channel id(s) the webhook posts into, comma-separated>
NORTHSTAR_TWITTER_FEED_SECONDS=5
```

The bot needs View Channel + Read Message History on that channel, and **Message Content Intent**
switched on (Discord Developer Portal → Bot → Privileged Gateway Intents). Both are also editable on
the dashboard (Settings).

## How delivery works

- Each post has a key (`feed:type:id`). The service worker keeps the last 20,000 forwarded keys in
  `chrome.storage.local`, so a page reload, an Ably reconnect or a Chrome restart never reposts.
- On the very first run, the backlog Vision loads from its history is recorded and not posted. After
  that, history posts not forwarded yet (missed while Chrome was closed) are posted, marked
  "catch-up".
- The queue survives restarts. Discord rate limits (429) are waited out exactly. Network errors and
  5xx are retried with back-off up to 60 s, forever. If Discord refuses an embed, the post is resent
  as plain text. If that is refused too, it is kept under "Refused by Discord", where **Retry failed**
  requeues it. A deleted or wrong webhook pauses sending and keeps the queue.
- `allowed_mentions` is empty, so a tweet saying `@everyone` never pings anyone.
- The webhook URL lives only in the extension's local storage and is never logged or shown in full.
  The extension does not read cookies, tokens or Uxento keys, sends nothing to Uxento, and changes
  nothing on the page.

## Limits

- It runs only while Chrome is open with a Vision tab. Nothing is captured while Chrome is closed.
  On reopening, Vision's history (about the last 50 posts per panel) is caught up.
- Chrome slows timers in background tabs. Posts are still all read (the full list is read on every
  page change), but a hidden tab can add seconds of delay. A tab in its own window, left visible, is
  fastest.
- Reading the page's list depends on Uxento's current page structure (react-virtuoso). An Uxento
  update can change it. The screen fallback then still forwards what is visible, and the popup says
  so.
- Check that forwarding Uxento content into your Discord fits Uxento's terms for your account,
  especially if the channel is shared with others.

## Tests

```
node --test tools/vision-forwarder/test/forwarder.test.js
python -m pytest tests/test_vision_forwarder.py     # the bot's reader understands what is forwarded
```
