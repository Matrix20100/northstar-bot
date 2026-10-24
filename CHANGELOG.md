# Changelog

Every version contains everything from the versions before it. Each release folder is built from
the complete code (`python scripts/make_release.py`), never from a partial patch.

## v4.5.0 - No rugs in the queue, the Watching tab or the radar; the brain obeys the risk system

Operator: "stop even allowing rug coins to show up", "stop tracking coins on the watchlist for super
long", "the brain chose to trade a coin that was so obviously a rug", "the uxento bot cannot keep up".

- **Confirmed rugs are blocked for good** (`northstar/rugs.py`, new state RUGGED). Three cases:
  - died: back at $5K after a $10K+ high;
  - liquidity pulled: down 90% from the high seen, which was $5K+;
  - dead pool: under $1,000 liquidity.

  A blocked coin is never researched or shown again and is remembered 30 days. Its research stays, so
  the learners keep it as a rug. The surge radar blocks such coins on sight, without research. Coins
  that are only flagged (mint authority, RugCheck danger, rug odds 60%+) can still be re-checked, but
  never show on the Watching tab.
- **Research queue**:
  - A pump.fun trade only brings forward a coin that is already scheduled, at most once a minute.
    Before, every trade queued any coin within 2 s, rejected and dead ones included: ~30,000 "not ready"
    researches a day.
  - Watch list: a coin leaves it after 2 hours (`watchlist_max_hours`), or once its market cap is
    under $8K (`watchlist_min_mcap_usd`).
- **Watching tab**: only live coins that passed safety, with why each is watched. Surges, trending
  coins and early watches are filtered the same way, and rugs blocked and flagged coins hidden are
  counted.
- **Rug scalps** post only in `NORTHSTAR_DISCORD_SCALP_CHANNEL_ID`, never in VIP. With no scalp channel
  set, scalps are not posted. Their follow-ups go to the same channel.
- **The brain's paper account obeys the risk system**:
  - no buy at HIGH/CRITICAL risk, on a failed launch/holder check, on any rug flag, or on a
    risk-threshold reject;
  - it buys on runner odds x (1 - rug odds);
  - the rug brain's odds and the risk level are offered to the runner brain as signals, kept only if
    they improve its accuracy.
- **Vision forwarder 1.1.0**: up to 5 posts per Discord message (waits at most 4 s to fill a bundle,
  stays under Discord's 6,000-character limit). The bot reads each post in a bundle separately.

## v4.4.0 - Uxento Vision forwarder

`tools/vision-forwarder`: a Chrome extension that forwards every post shown in your Uxento Vision
panels (X Tracker, Fomo Trades, Fomo Thesis and any panel added later) to a Discord webhook. The bot
reads that channel through the existing tracker feed (`NORTHSTAR_TWITTER_FEED_CHANNEL_IDS`), so a
posted contract address is researched at once and accounts, tickers and words feed attention. No
new pipeline, no change to scoring. See `tools/vision-forwarder/README.md` for why it reads the page
(Vision's X Tracker data is encrypted in transit; Uxento has no API) and how to install it.

- Duplicate-proof (20,000 remembered post IDs), first-run backlog skipped, catch-up after Chrome was
  closed, Discord rate limits and outages retried, refused posts kept and retryable, no @mentions.
- `tests/test_vision_forwarder.py`: the bot's Discord reader parses forwarded posts (CA, account,
  ticker) and researches the CA. Extension tests: `node --test tools/vision-forwarder/test/`.

## v4.3.0 - Dead coins are read as rugs, closed and learned from

A coin that was judged or called at $10K+ and falls to $5K or lower (`dead_mcap_usd`, editable on the
dashboard under Self-learning, 0 = off) is dead, whatever multiple that is. The 0.3x rule still
applies on top. Coins judged near launch value (under $10K) are not affected: pump.fun coins start
around $5K.

- Runner calls close as soon as the coin is dead instead of staying OPEN for 24 hours. Nothing is
  posted (only good news goes out); "DEAD" is recorded.
- VIP and scalp calls close as rugged the same way.
- Paper trading sells what is left at the dead price (exit "coin died"), on both accounts.
- Learning: the rug brain, the runner brain and the 24h labels use the dead line. A 2x that then died
  is a loss, not a runner. Coins already in training memory are relabelled once.
- Curve watch: a dead coin finishes tracking and counts as "fell to 0.3x" in the curve learner,
  blocked-rule and narrative tables.

## v4.2.1 - Learning tab shows the real numbers

- Rug brain and training memory showed "not trained yet" / "0 coins" on the dashboard although the
  bot had trained them. The dashboard's read-only connection could not run the "create table" step
  those reads started with; they now skip it there.
- "Coins recorded" and "Checked against real price" showed 0: the "what predicts a rug" table reused
  the same variable and overwrote the counts. Renamed.
- Narratives: coins recorded before v4.2.0 have no stored theme and all showed as "other". They are
  now classified from their ticker when read.
- Blocked table: one row per rule. "5-min volume $5,340 under $7,500" and "$6,100 under $7,500" are
  now one row, "5-min volume under $7,500" (same for market cap, age, extreme rejects, trap checks).

## 4.2.0 — 2026-10-23 — Twitter attention from J7 / uxento, narratives, two calls an hour

Operator: "The bot must have twitter trackers like J7 or uxento to understand exactly where all
attention is", "narrative must be analyzed and understood", "try and call out a coin at least
twice an hour". J7 / uxento post into the operator's Discord; no paid AI.

- **Twitter tracker feed (`social_feed.py`).**
  - Every 20 s the bot reads the Discord channels in `NORTHSTAR_TWITTER_FEED_CHANNEL_IDS` (where
    J7 / uxento post) through Discord's API with its own token. It parses each post, embeds
    included, for contract addresses, $TICKERs, the X account, and words. The first read starts
    from now and never replays the channel's history.
  - **Research now:** a contract address posted in the feed is researched at once (all safety
    checks still apply; `NORTHSTAR_TWITTER_FEED_RESEARCH`).
  - **Attention:** its contract posted by 1 / 2+ accounts = 35 / 50 points; its $TICKER posted =
    15; its name being a word many tracked tweets use in the last hour = 20.
  - **Problems are spelled out** in the log and on the dashboard: no access to the channel,
    wrong id, or "Message Content Intent" switched off (Discord then hands bots empty text).
- **Narratives (`themes.py`).**
  - **Theme:** every coin entering the early band is sorted into a theme (animal, politics,
    celebrity, AI/tech, crypto culture, events/news, food, internet culture, other) and stored
    with its outcome.
  - **Learned points:** per theme over the last week, how often coins hit 2x and how often they
    fell to 0.3x. A theme that beats the average adds up to +15 attention points; one that does
    worse costs up to 10. A theme needs 15 finished coins before it counts.
  - **Live matching:** the coin's words are matched against live attention: launch waves, news,
    Google Trends, trending coins, and the Twitter feed.
- **Two calls an hour.** With fewer than 2 runner calls in the last hour, the score bar is
  lowered by 2.5 points per missing call (at most 5; never under the curve learner's minimum of
  35). Traps and extreme cases are never relaxed. Settings: `NORTHSTAR_RUNNER_TARGET_CALLS_PER_HOUR`,
  `NORTHSTAR_RUNNER_PACING_MAX_RELIEF`.
- **Several problems at once still reject.** Checked against the live log: under 4.0's pure
  penalties, $CITY (20% bundle + insiders faking activity + crashed to 14% of its high) scored 46
  and would have been called. Now:
  - **one or two** serious problems cost points, as asked ("don't reject for 1 thing");
  - **three at once** reject (`NORTHSTAR_RUNNER_REJECT_PROBLEM_COUNT`);
  - a coin already **crashed to 20% or less of its high** rejects (`NORTHSTAR_RUNNER_REJECT_CRASHED_X`).
  - Results on this morning's coins: $CITY and $LARPOWEEN (29% bundled, 40% sniped, launch jumped)
    are rejected; $IMP (sniped, pulled back to 32%) can still be called.
- **Dashboard:**
  - **Pulse tab: 🐦 Twitter attention:** contracts, tickers and words from the feed in the last hour.
  - **Learning tab: 🧭 Narratives:** each theme's week vs all coins.
- Tests: `tests/test_twitter_narrative.py`, pacing in `tests/test_runner_calls.py`.

## 4.1.0 — 2026-10-23 — change every setting on the dashboard; teach the AI with real coins

Operator: "I must be able to change every setting inside the dashboard", "I want to be manually
able to teach the AI with real coins".

- **Settings tab: every setting is editable** (`operator_settings.py`).
  - **Validation:** each value is checked exactly like a .env value; a bad value is refused with
    the reason.
  - **Live and kept:** a change applies to the running bot at once and is stored, so it survives
    restarts.
  - **Priority:** a dashboard value beats .env, the default, and anything the learners tuned.
    The learners (settings tuner, curve learner) leave a setting you set alone.
  - **Restart-only settings:** loop timings, feature on/off switches, rate limits and channel ids
    are read at start; the panel marks them "after restart".
  - **Reset:** puts a setting back to .env or the default.
  - **Never editable:** keys, tokens, passwords, and where the bot keeps its data or listens.
  - **New groups:** "Calls on young coins (Pulse)", "Tracked wallets & attention", "Paper trading".
- **Teach the AI** (`teach.py`). Every coin's research drawer has 👍 runner / 👎 rug /
  😐 went nowhere / clear.
  - A lesson turns the coin's own research numbers into a training example.
  - It overrides whatever the bot measured for that coin in every training example. A coin you
    know rugged is never learned as a winner.
  - Both brains learn it at their next hourly training.
  - The daily cleanup never removes a lesson.
- Tests: `tests/test_operator_settings_teach.py`.

## 4.0.1 — 2026-10-23 — extreme cases still reject outright

Operator answer: hard rejects for "traps + extreme only" (4.0.0 had made only real traps reject).

- A runner call is also rejected outright when:
  - other wallets bought **40%+** of supply in the creation block (`NORTHSTAR_RUNNER_REJECT_BUNDLE_PCT`);
  - the dev holds or bought **20%+** of supply (`NORTHSTAR_RUNNER_REJECT_DEV_PCT`).
- These coins are dropped from tracking at once, since they can never clear.
- Anything below these limits stays a scaled score penalty, as in 4.0.0.
- Test: `tests/test_runner_calls.py`.

## 4.0.0 — 2026-10-23 — score, don't veto; Pulse rescans; posts in the operator's layout

From the operator's handwritten brief ("Northstar Bot", pages 1-2). Includes 3.5.0 (wallet watch).

**Calls**
- **Only real traps block a call.**
  - The traps: mint authority, freeze authority, a restricting token extension (each must RUN and
    PASS), a RUG LIKELY verdict, a completed curve.
  - Everything else is a scaled penalty on the 35 safety points: bundled launch (10% up to 50%+),
    sniped launch, dev buy at launch, snipers still holding, top-10 and largest holder, dev bag,
    insider tiny trades, fresh or linked wallets, the launch chart, rug-check HIGH RISK / CAUTION
    and serious red flags, recycled socials, checks that could not run.
  - One weak signal no longer kills a strong coin; several problems still drop the score under
    the floor. A 74%-bundled coin with 46% in the top 10 scores 0 safety points.
- **Score:** momentum 40 + pulse up to +10 + safety 35 + attention 25 (cap 100). The floor stays 45
  and is learned by the curve learner. The parts and every penalty are stored with the research.
- **Pulse rescans.**
  - The curve radar samples every coin's market cap each scan (every 30 s) and reads its
    15-minute pattern.
  - A first pullback that bounces (+10), a first pump holding near its high (+6), dumping (-8).
  - A near miss (within 15 points of the floor, no trap) is researched again after 1, 2, 4, 8 min,
    at most 10 min apart, up to 5 times, each time with fresh numbers. Then it is dropped.

**Posts**
- **One layout for every post** (calls, early watches, milestones):
  - "Northstar Callouts · time", coin image, $TICKER, Callout price, Current price, Multiplier,
    Peak since callout, Callout type, Links, contract address;
  - buttons: More info, Axiom, DexScreener;
  - the title starts with the call type's emoji (☠️ for a rug scalp).
  - Price is market cap / supply: 1B for pump.fun coins, the coin's own price and market cap for
    others.
- **VIP:** calls, early watches (on by default again, in the compact layout), take-profits and
  milestones (2x, 3x, 5x, 10x) for every call, including runner calls.
- **Only good news is posted** (`NORTHSTAR_POST_DRAWDOWNS=false`). Stop-loss, time-stop,
  trailing-stop, exit-now, warning and rugged messages are recorded but never posted to VIP or
  free. The paper accounts and the learners still use them. They go only to a separate warning
  channel, if one is set.
- **Free channel:** every VIP call 20 minutes later (`NORTHSTAR_FREE_CALLOUT_DELAY_MINUTES`), with
  every milestone reached so far posted at once, then each new milestone as it happens.
  - Not rug scalps: a minutes-long trade on a rug is useless and dangerous 20 minutes late.
  - Not a call whose plan already exited.

**Settings:** `NORTHSTAR_POST_DRAWDOWNS`, `NORTHSTAR_FREE_CALLOUT_DELAY_MINUTES=20`,
`NORTHSTAR_EARLY_WATCH_CHANNEL=vip`, `NORTHSTAR_RUNNER_MAX_RECHECKS=5`,
`NORTHSTAR_RUNNER_RECHECK_SECONDS=60`, `NORTHSTAR_RUNNER_RECHECK_MAX_SECONDS=600`,
`NORTHSTAR_RUNNER_RECHECK_MARGIN=15`.

## 3.5.0 — 2026-10-23 — follow the money: what the tracked wallets are buying, live

Operator request: "track what tracked wallets are buying, find where all the attention and money is
flowing". Before, the 1,436 tracked wallets were only checked while a coin was being researched
("do any of them hold it?"), so they could never find a coin.

- **Wallet watch (`wallet_flow.py`).**
  - Every pump.fun and PumpSwap trade the tracked wallets make is streamed from PumpPortal
    (`subscribeAccountTrade`, on the connection the bot already holds) and stored for 3 days.
  - Per coin, over the last 30 minutes: how many different tracked wallets bought, SOL in, SOL out.
- **Research now** when 2+ different tracked wallets buy the same coin within 30 minutes, or one
  of them buys 5+ SOL in a single trade.
  - The coin skips the queue like a radar surge, with fresh DexScreener numbers, so the runner
    check can judge it.
  - Every safety check still applies.
  - Dropped, untracked and already-called coins are not brought back.
- **Attention:** 1 / 2 / 3+ tracked wallets buying right now adds 15 / 30 / 45 attention points,
  with the reason shown on the call ("3 tracked wallets bought in the last 30 min (4.2 SOL in, 0.5 out)").
- **Dashboard, Pulse tab: 💸 Money flow card.** The coins your wallets are buying in the last hour,
  how many wallets, SOL in and out, net, last trade, and the watch's state. Every 30 minutes the
  log shows the top flow.
- **Cost control.** PumpPortal meters this stream at 0.01 SOL per 10,000 trades, and it needs an
  API key whose linked wallet holds at least 0.02 SOL.
  - Without `NORTHSTAR_PUMPPORTAL_API_KEY` the watch stays off and says so in the log.
  - A wallet trading more than 60 times an hour trades like a bot: it is muted (unsubscribed)
    for the day.
  - After 30,000 messages in a UTC day (about 0.03 SOL), the stream pauses until midnight UTC.
- Settings: `NORTHSTAR_WALLET_WATCH_ENABLED`, `..._FLOW_WINDOW_MIN`, `..._FLOW_MIN_BUYERS`,
  `..._FLOW_BIG_BUY_SOL`, `..._WATCH_MAX_EVENTS_DAY`, `..._BOT_TRADES_PER_HOUR`.
- Tests: `tests/test_wallet_flow.py`.

## 3.4.2 — 2026-10-23 — scan for callable coins 4x as often

Operator: "research queue should move a lot faster so we can make more calls". The queue was not
the bottleneck: it sat at 0, and coins entering the band already skip it. Two things limited calls.
First, how many coins the curve radar caught in the band. Second, almost all of those failing a
safety check (mostly bundled launches). This release addresses the first; no safety rule changes.

- The curve radar checks every **30 seconds** (was every 2 minutes). Young pump.fun coins often
  cross the $15K-$90K band in under 2 minutes, so a 2-minute scan missed some entirely.
- It looks only at launches from the **last hour** (was 3 hours). A coin over 45 minutes old can
  never be runner-called, so the extra two hours were wasted lookups. Each pass now needs about a
  third of the DexScreener lookups: roughly 40 a minute in total, within the 200 a minute budget.
- The curve watch still follows every coin that entered the band for its full 4 hours.

## 3.4.1 — 2026-10-23 — the first runner call was never priced

From the overnight log: the bot made its first runner call, but the call tracker reported
`'open': 1, 'read': 0` every 30 seconds for hours. A curve coin whose bonding-curve account could
not be read or priced from the chain was skipped silently, with no second source. So the call got
no live market cap, no TP or stop pings, no 2x proof to the free channel, and no paper result.

- A curve call the chain read cannot price is now read from DexScreener, which lists the bonding
  curve as a "pumpfun" pair with a market cap. If it has migrated, the deepest pool is used.
- The reason a read failed is logged once per coin ("curve account did not parse", "no SOL/USD
  price", ...), so a miss can never be silent again.
- Test: `tests/test_early_calls.py` (fails on the old tracker).

## 3.4.0 — 2026-10-23 — blocked coins: a few rechecks for high scorers, then dropped

Operator request: "High scoring coins should be tracked a few times again and if they don't pass
delete from tracking."

After a runner check that does not call, every coin now gets one of two outcomes:

- **Recheck:** the runner score is at least the floor (45) and every blocker can still change
  (holder concentration, dev holding, snipers still holding, insider tiny trades, volume, buys vs
  sells, a check that could not run). It is researched again in 3 minutes, up to 3 times
  (`NORTHSTAR_RUNNER_MAX_RECHECKS`, `NORTHSTAR_RUNNER_RECHECK_SECONDS`). If it still does not
  pass, it is dropped.
- **Drop now:** the score is under the floor, or a blocker can never clear. That covers a bundled
  or sniped launch, a 6x+ first-minute jump, a staircase or already-dumped chart, mint or freeze
  authority, a restricting token extension, recycled socials, past the 45-minute age limit, or a
  completed curve.

Dropped coins:
- get state DROPPED and leave the research queue. Neither the radar nor any feed brings them
  back. "Retrack" in the panel undoes it;
- are still measured by the curve watch for 4 hours, so the learners see what they did.

Also fixed: the curve radar could queue coins it was only measuring (the curve watch's open
coins), including untracked ones. It now only queues coins it may research.

The 5-minute runner summary now reads "N in the band, M called, R to recheck, D dropped".

## 3.3.2 — 2026-10-23 — paper sanity rails, and what each safety rule blocks

From the live log:

- **The brain's paper account "bought" a coin for $1.35 billion.** One bad price quote had blown up
  its balance. Fixes:
  - **Rails:** the brain never buys a coin that failed the prefilter or a safety check, was rated
    RUG LIKELY, has under $10K liquidity, or shows an unbelievable entry (market cap under $5K).
  - **Same pool:** a position is priced from the pool it was bought in, never from another pool
    with a broken price.
  - **Bad quotes:** a price that moves more than 20x (up or down) between two checks 15 seconds
    apart is ignored as a bad quote. This applies to both accounts.
  - **Repair:** a run with impossible numbers (a trade or balance over 1,000x its start) is closed
    once at start and a fresh run begins with the same settings. The broken run stays in history.
- **What each safety rule really blocks.** In the log, all 4 runner candidates were blocked by
  bundled launches (15-29% bought in the creation block), plus a 7.2x first-minute jump or an
  already-dumped chart. The runner check now stores each coin's first blocker and its bundle share.
  `python main.py curve` and the Learning tab then show, per blocker, how often the blocked coins
  hit 2x, hit a clean 2x, or fell to 0.3x. Any loosening is then based on whether a rule blocks
  rugs or blocks runners. Nothing is loosened by this change.
- Tests in `tests/test_paper.py` and `tests/test_learning_v330.py`.

## 3.3.1 — 2026-10-23 — stop re-checking dead coins every hour

From the live log: 157 "Curve trade sample" lookups in 2 minutes, nearly all finding no trades at
all.

- **Coins the curve radar found are no longer re-checked hourly forever.** A coin with no pool and
  no trades has nothing to judge. Before, a launch-only coin waited for the radar, but one the radar
  had ever found was put back on a 1-hour timer. That covers thousands of coins, nearly all dead,
  at about one RPC call a second. Now it waits for the radar, which re-queues it the moment it
  trades in the band again. Coins a DexScreener feed or a migration found keep their timer. The
  backlog clears itself within an hour of the update.
- **A sample with nothing in its window is logged at DEBUG.** It filled the log. Samples that found
  trades or hit an error are still logged.
- Test: `tests/test_engine.py`.

## 3.3.0 — 2026-10-22 — always-on learning, clean training data, self-repair

Operator request: clean the database so the AI trains on real data, learn 24/7, change in real
time, and fix itself.

- **Training cleanup (`cleanup.py`).** Runs a minute after start, then daily. `python main.py
  cleanup [--dry-run]` runs it by hand.
  - Training examples:
    - removes examples with no label after 2 days;
    - removes examples where 60%+ of the numbers were never measured;
    - removes repeats of the same coin within 6 hours (one coin researched 40 times an hour
      counted as 40 coins).
    - Removed keys are remembered (`training_removed`), so harvest never copies them back.
  - Curve watch: sets aside coins the radar stopped seeing within 10 minutes of entering the band
    (their "outcome" was only their entry price), and rows with impossible numbers.
  - Research history, calls, paper trades and settings are never touched.
- **Curve learner (`curve_learn.py`).** The runner calls now learn from the coins they are
  actually made on.
  - The curve watch stores the runner check's verdict per coin: its score, and whether every
    safety check passed.
  - Every 30 minutes the early trade plan is replayed on real 4-hour outcomes for each score
    floor. `runner_min_score` moves one step (5 points) toward the floor that made the most per
    trade.
  - Only coins that passed every safety check count. A new floor needs 25+ coins and must beat
    the current floor on older AND recent coins. The floor stays within 35-70.
  - The learned floor is kept across restarts. It is shown on the Learning tab and in
    `python main.py curve`.
- **Learning every hour** (`NORTHSTAR_LEARNING_INTERVAL_HOURS`, was 6). Both brains retrain and
  the settings tuner runs hourly; changes apply live. Measured: about 80 s for the runner brain
  and 7 s for the rug brain on 6,000 examples, in the background.
- **Self-repair watchdog.** Every minute it checks the background loops. A loop whose iteration
  has hung for 15+ minutes (and 3x its own interval) is cancelled and restarted, with an ERROR
  line in the log.
- Unchanged: the safety checks themselves. No learner can switch off or loosen a rug check.
- Tests: `tests/test_learning_v330.py`.

## 3.2.2 — 2026-10-22 — let safe runners through, show runner calls

Operator: "nothing called out for 5 hours".

- **The v3.2.1 curve-routing fix is included.** It was pushed after PR #12 had been merged, so it
  never went live.
- **Runner score floor 60 → 45.** The score is momentum 45 + safety 30 + attention 25. With no
  KOL channels set, attention is usually 0. A safe coin with ordinary momentum (a quarter of its
  market cap traded in 5 minutes, 4 buys for every 3 sells) scores about 50-58, so 60 blocked
  coins that had passed every safety check. The live coin `8HUGj6…` scored exactly 60.
  - Every safety rule still applies: each required check must PASS, and any FAIL blocks.
  - 45 still rejects weak flow, for example flat buys at $85K with falling price.
  - It is a setting (`NORTHSTAR_RUNNER_MIN_SCORE`). The curve watch shows how coins at each
    level did.
- **Runner summary in the log every 5 minutes:** how many coins reached the runner check, how many
  were called, the best score, and the most common blockers. The next "why nothing?" can be
  answered from one line.
- **The dashboard counts runner calls.** "Calls (24h)", "open" and the Calls tab only read the old
  calls table, so runner calls would have shown 0. They are now included, marked 🎯 RUNNER.
- Unchanged on purpose: the "price sources disagree" check that rejected $KIMCHI. On your history,
  coins rejected for it rugged 64% of the time (23 of 36).

## 3.2.1 — 2026-10-22 — curve coins reach the runner check

From the first 10 minutes of v3.0.0 live: the curve radar found coins, but most never reached the
runner-call check.

- **Curve coins were researched as if they had migrated.** DexScreener lists a pump.fun coin's own
  bonding curve as a "pumpfun" pair. When it gave a liquidity figure for it, research treated the
  coin as a DEX-pool coin. It then failed the $7,500 pool liquidity gate (a curve's "liquidity" is
  a few thousand dollars) and never got the runner check: `2bkP52…` and `5wgCSX…` in the log.
  Now a coin whose best pair is its own unfinished curve always gets the curve research. That
  research checks the curve on-chain, so a completed curve still goes the pool way.
- **Busy coins could never pass the launch-buys check.** It reads the curve's transactions back to
  the launch, and stopped after 5,000. A coin with real volume at $40K often has more, so the
  check stayed UNKNOWN and blocked the call: `8HUGj6…` in the log (which was also correctly
  blocked for having dumped to 16% of its high). Runner candidates now read up to 20,000 back
  (`NORTHSTAR_RUNNER_LAUNCH_MAX_PAGES`, 1,000 per page). Unreachable still means UNKNOWN, never a pass.
- Tests in `tests/test_pipeline_scenarios.py`.

## 3.2.0 — 2026-10-22 — the learning brain paper trades its own picks

Operator request: let the AI learner paper trade.

- **Brain paper account (`paper.py` BrainTrader).** This is a second account, separate from the
  calls account, with its own starting amount. Simulated only.
  - Every coin researched with a DEX pool gets the runner brain's odds and the rug brain's odds.
  - The brain buys when the runner odds are 15%+ (`NORTHSTAR_PAPER_BRAIN_MIN_P`; the base rate is
    about 5%) and the rug odds are under 30% (`NORTHSTAR_PAPER_BRAIN_MAX_RUG`). It buys at the
    pool's market cap at that moment.
  - One position per coin per run, at most 10 open (`NORTHSTAR_PAPER_BRAIN_MAX_OPEN`).
  - Positions follow the standard trade plan (TP1, TP2, stop, time stop, trailing stop) on
    DexScreener prices checked every 15 seconds. Anything still open after 24 h
    (`NORTHSTAR_PAPER_BRAIN_HOLD_HOURS`) is sold at the last price seen.
  - It never posts anything and has no effect on real calls. It shows whether the brain's picks
    make money before the brain gets a say.
- **Paper tab:** a switch between "Bot's calls" and "Brain's picks". Each account has its own
  starting amount and "Start new run". Brain trades show the odds they were bought at.
- **`/paper`** shows both accounts.
- Tests: brain account in `tests/test_paper.py`.

## 3.1.0 — 2026-10-22 — paper trading on every call

Operator request: paper trade every callout, with an editable starting amount.

- **Paper trader (`paper.py`, tables `paper_runs` and `paper_trades`).** Simulated only; nothing signs
  or sends a transaction. It follows every call posted after the run started: runner calls, VIP
  calls, rug scalps and manual calls.
  - Each call buys with a share of the cash left (default 10%), at the call's entry market cap.
  - It sells exactly when the bot posts a plan event:
    - TP1 and TP2 sell the plan's share;
    - trailing stop, stop loss, time stop and exit-now sell the rest.

    Each sell uses the market cap the bot tracked at that moment.
  - Whatever is left when tracking ends (window over, rugged, message deleted, untracked) is sold
    at the last market cap seen.
  - A fee and slippage charge (default 1%) is applied on every buy and every sell. It is an
    assumption, not a measured value.
- **Dashboard Paper tab:**
  - balance, profit/loss, cash, open positions, closed trades;
  - every trade with its sells and P&L;
  - a form to set the **starting amount**, size per call and fee. "Start new run" resets the balance
    to the new amount and keeps earlier runs in history.
- **`/paper` slash command** in Discord: the same account summary.
- Settings: `NORTHSTAR_PAPER_ENABLED`, `NORTHSTAR_PAPER_START_USD`, `NORTHSTAR_PAPER_POSITION_PCT`,
  `NORTHSTAR_PAPER_FEE_PCT` (the first run's values; the dashboard changes them after that).
- The startup warning about the missing AI key now only appears when runner calls are off.
- Tests: `tests/test_paper.py`.

## 3.0.0 — 2026-10-22 — runner calls, attention score, compact calls, free = proven wins

Operator report: "Nothing is even being called out." Two causes, both in the code:

- the early-call path needed the AI judge, and no AI key is set, so it could never call;
- the "already +400% since launch" rule rejected nearly every curve coin above about $25K, while
  the curve radar only looks at $24K-$50K. The two rules cancelled each other out.

What changed:

- **Runner calls (`runner.py`), no AI key needed.** A pump.fun coin is called when all of these hold:
  - momentum: $15K-$90K market cap, 45 min old or less, $7.5K+ 5-minute volume, buys at least
    matching sells (the curve radar's DexScreener numbers, seconds old);
  - safety: mint and freeze authority revoked, no restricting extension, holder concentration,
    dev holding and launch buys all RAN and PASSED. Any FAIL gate (snipers, insider tiny
    trades, wallet network, ...) blocks. So do a bad launch chart, RUG LIKELY / HIGH RISK,
    recycled socials, and serious rug-check red flags. UNKNOWN is not a pass;
  - runner score 60+ out of 100: momentum 45, safety 30, attention 25.

  At most 6 calls an hour. Every candidate's score and blockers are stored with its research.
  The weights are a first setting: the curve watch records what each coin did, so
  `python main.py curve` checks them against real outcomes.
- **Attention (`attention.py`).** Every coin gets a 0-100 attention score with plain reasons.
  Sources:
  - public KOL Telegram channels (`NORTHSTAR_KOL_TELEGRAM_CHANNELS`, read from their public
    t.me/s/ page): which mints and $TICKERs they post;
  - crypto news RSS (CoinDesk, Cointelegraph, Decrypt);
  - pump.fun launch waves;
  - CoinGecko / GeckoTerminal trending;
  - Google News, Google Trends, Wikipedia;
  - your tracked wallets holding it;
  - DexScreener promotion.

  It counts in the call score, and the curve watch stores it per coin, with a new rule
  "nexas + attention".
- **Compact Discord calls.** Only the key numbers: market cap, liquidity, 5m volume, age, holders,
  launch, rug odds, attention, tracked wallets, score, plan, peak, address. Plus the coin image and
  buttons **More info** / Axiom / DexScreener. More info shows the full research, only to the
  person who clicked (handled by the slash-command bot, so it keeps working after a restart).
- **The free channel gets proven wins only.** A call is posted there once it reaches 2x
  (`NORTHSTAR_FREE_PROOF_MULTIPLE`, `NORTHSTAR_EARLY_CALL_FREE_AT`). Delayed free copies of
  fresh calls are gone. A manual call goes to free only when "also post to free" is ticked.
- **Early watches never post to free.** `NORTHSTAR_EARLY_WATCH_CHANNEL` defaults to `off`
  (`free` now means off; `vip` still posts there). Coins that are not posted are UNTRACKED,
  not followed for hours.
- The old AI early-call path is still there with `NORTHSTAR_RUNNER_CALLS_ENABLED=false`.
- Tests: `tests/test_runner_calls.py`, the More info button in `tests/test_commands.py`, and
  updated routing, free-copy and embed tests.

## 2.6.2 — 2026-10-22 — measure the early band (where call bots post)

Operator request: reverse-engineer the "Nexas Alerts" calls and make coin finding better.

- **Nexas, from its posted calls** (20 calls, 16-22 Oct):
  - every coin is a pump.fun coin 1-27 minutes old, at $14K-$58K market cap (on or just off
    its curve), with $7.7K-$74K of 5-minute volume;
  - the 5-minute price change ranges from -85% to +130%, so price direction is not a filter;
  - "Dev Holding 1.5%" and "Fees 15%" are the same on every call (placeholders, not data);
  - its "Quality Score" did not line up with results: the 3x calls scored 49-82, and so did
    the misses;
  - it posts milestones but never losses.

  In the excerpt, 4 of 17 calls hit 2x or more and 2 more hit 1.5x; the rest posted nothing.
  Peaks, not exits.
- **Northstar could not compete there, and could not measure it.**
  - The curve radar looks at $24K-$50K, but the "already +400% since launch" rule rejects nearly
    every coin above about $25K. That is the whole Nexas zone.
  - Curve-stage rejections were never scored, because decision tracking needs a DEX pool.
- **Curve watch (`curve_watch.py`, table `curve_watch`).**
  - Every pump.fun coin that enters the $10K-$80K band on its curve is recorded the moment it
    enters: age, market cap, liquidity, 5-minute volume, buys, sells, price change.
  - It is then followed for 4 hours, on the curve and after migration: peak, low, the worst
    drop before the peak, and whether it migrated.
  - It reuses the curve radar's DexScreener batch, so there are no extra API calls. One small
    row per coin, kept 21 days.
- **Rules scored on real outcomes** (`python main.py curve`, and the Pulse tab's "Early band"
  card): every coin in the band, a Nexas clone, Nexas plus more buys than sells, final stretch
  $40K+, today's curve radar, and heavy volume. Each shows the share that reached 1.5x, 2x, 3x,
  a clean 2x (no 0.6x first), the share that fell to 0.3x, and the share that migrated. The
  early-band rules (radar band, the +400% rule) change only once these numbers exist.
- Tests: `tests/test_curve_watch.py`.

## 2.6.1 — 2026-10-22 — two brains with a compact memory, manual untrack, holder-network gate

Operator request: one brain that learns rugs and bad coins, one that learns coins that ran; a
compact database that still remembers the training data; a way to untrack a coin by hand (the
Trump $1 coin was still tracked); and read the holders, their wallets and shared funding when
judging risk.

- **Two brains.**
  - **Rug brain** (`rugmodel.py`, from v2.6.0): odds of falling to 0.3x. It sets the rug odds in
    the score.
  - **Runner brain** (`brain.py`): odds of a clean runner (2x before 0.6x, no rug after).
  - The dashboard's Learning tab shows both, each with its own accuracy and signals.
  - **The runner brain must now also beat the trust bar on near-call coins** (coins with a full
    minute price path). On the 22 Oct data its 0.71 overall came from easy rejects; on
    near-call coins it scored 0.55. It is no longer trusted and has no say in calls until it
    really ranks those coins.
- **Compact training memory (`training_store.py`, table `training_examples`).**
  - Every finished example (minute path or 24 h tracking) is stored once with its labels: rug,
    runner, peak and low. The brain's numbers are packed as float32 in a versioned feature
    order, about 280 bytes a coin instead of about 1.5 KB of JSON.
  - Every rug and every runner is kept up to `NORTHSTAR_TRAINING_MAX_EXAMPLES` (30,000). Only
    plain coins are thinned when it is full.
  - Both brains train from it, so they keep learning from coins long gone from the raw tables.
  - On the 22 Oct copy: 5,017 examples (1,118 rugs, 222 runners) in 1.4 MB. After a month of
    raw pruning the rug brain still trained TRUSTED at AUC 0.92 (recent 0.90).
- **decision_tracking no longer grows forever** (about 3 MB a day before):
  - harvested numbers on finished rows are cleared after 2 days;
  - finished rows older than `NORTHSTAR_TRACKING_RETENTION_DAYS` (21) are deleted;
  - called coins keep their tracking.

  On the 22 Oct copy, one pass took it from 9,902 rows / 15.1 MB of numbers to 2,860 rows /
  4.4 MB.
- **Manual untrack.**
  - Use `/untrack <mint> [reason]` or `/retrack <mint>` in Discord (admin), or the
    **Untrack / Track again** button on a coin in the dashboard.
  - An untracked coin (state IGNORED) leaves the watch list and the research queue. Its open
    call and early-watch posts stop updating, and no feed, radar, surge or migration brings it
    back.
  - Its price is still recorded so the brains learn how it ended.
- **Holder-network gate `wallet_network`** (risk threshold; the rug brain cannot overrule it).
  - Fails when any of these is true:
    - brand-new wallets hold 15%+ of supply;
    - 80%+ of 5 or more traced holder/buyer wallets are brand new;
    - one group of wallets linked by shared funding holds 8%+.
  - From the bot's own 24 h tracking, those coins crashed 90%, 93% and 71% of the time.
  - Wallet tracing now covers the top 15 holders first, then launch and current buyers: 22
    wallets instead of 12, with second-hop funders as before.
- Tests: `tests/test_v261_memory_untrack_wallets.py` (16).

## 2.6.0 — 2026-10-22 — a learned rug model drives the score; Pulse view

Operator request: rework the 0-100 score so it rates coins correctly, let research really spot a
rug, stop high top-10 alone from killing a coin like Blacktober, and make discovery work like
Axiom Pulse.

**What the 22 Oct database showed** (478 coins with at least 2 h of minute price paths):
- The old score did not separate outcomes. Coins scoring 65+ were bad (rug or pump-then-rug) 43%
  of the time. Called coins were bad 58% of the time; rejected coins 39%.
- 136 coins were rejected only for top-10 concentration. They were bad 41% of the time, the same
  as coins that passed every check.
- Whether a coin RUGS is predictable. A logistic model on the research numbers, validated
  walk-forward, scored AUC 0.92 overall (0.89 on the most recent coins, 0.83 on fully tracked
  coins). Whether a coin RUNS was not predictable (AUC 0.49).

**Changes:**
- **Rug model (`rugmodel.py`, table `rug_models`).**
  - Learns the odds that a coin falls to 0.3x of its research price. It trains from the same
    tracked examples as the brain, every learning pass, in about 8 s.
  - Used only when TRUSTED: 300+ examples, 30+ rugs, and walk-forward AUC of at least 0.75 both
    overall and on recent coins. On the 22 Oct copy it trains TRUSTED: 5,000 coins, AUC 0.92,
    recent 0.89.
- **Reworked score.**
  - With a trusted model, 60% of the 0-100 score is learned safety, 100 × (1 − rug odds); 40% is
    the old opportunity score (`NORTHSTAR_U_RUG_WEIGHT`).
  - Posts and the dashboard show "learned rug odds" and the opportunity score separately.
  - The risk class follows the measured odds instead of the rule-of-thumb verdict.
- **Decisions.**
  - Rug odds of 60%+ reject the coin (`NORTHSTAR_RUG_REJECT_MIN`).
  - A call needs rug odds of 50% or less (`NORTHSTAR_RUG_CALL_MAX`).
  - At 30% or less (`NORTHSTAR_RUG_OVERRIDE_MAX`), the top-10, creator-holdings and
    linked-cluster thresholds stop blocking. They are still shown as concerns.
  - Proven traps, liquidity, and the launch / sniper / tiny-insider-trade filters are never
    overruled.
- **Out-of-fold results by rug odds** on 367 fully tracked coins:

  | Rug odds | Bad | Clean 2x | Any 2x |
  |---|---|---|---|
  | under 0.2 | 9% | 11% | 13% |
  | 0.2 - 0.4 | 26% | 10% | 17% |
  | 0.4 - 0.6 | 33% | 15% | 26% |
  | 0.6 and over | 54% | 8% | 30% |

  Low odds cut rugs; they do not find runners.
- **Pulse tab (dashboard).**
  - Every coin researched in the last 3 hours, in Axiom-style columns: New pairs (on the curve),
    Final stretch (close to migrating), Migrated, and **Passed all filters**.
  - Each card shows market cap, liquidity, age, score, rug odds, the filters it failed (red) or
    only warned on (amber), "N/M filters passed", and tracked wallets holding.
  - Dust (no liquidity / no trades) is counted, not shown.
  - A funnel line shows where coins fail most.
- Tests: `tests/test_rug_model_v260.py` (13).

## 2.5.10 — 2026-10-22 — rugged calls are closed

- **Rug close for calls (`monitor.py`).** Calls were tracked for 72 hours whatever happened. Now,
  once a verified price read puts a call at or under `NORTHSTAR_CALLOUT_RUG_CLOSE_X` (0.3x) of its
  call market cap:
  - one "RUGGED" warning is posted;
  - the call is CLOSED and no longer tracked.

  This matches the v2.5.8 rule for early watches. 0 turns it off.
- Tests: `test_alerts_monitoring.py` (rugged call closed once; a 0.4x dip stays open).

## 2.5.9 — 2026-10-22 — rug filters, tracked wallets, honest "watched" count

Operator request: track my wallets and see if they are in a coin; Axiom-style filtering; reject
coins with a giant jump in buys at the very start; look at how many coins the creator launched and
how many migrated; look for tiny buys and sells by bundlers or top-10 holders in the live trades.

- **Tracked wallets (`analysis/holdings.py`).** The bot reads a wallet list from
  `NORTHSTAR_TRACKED_WALLETS_FILE` (default `data/tracked_wallets.json`): an Axiom wallet-tracker
  export, or one address per line.
  - On every full analysis, and on every early-watch candidate, it reads each wallet's token
    account for the coin on-chain: 100 per call, 15 calls for 1,436 wallets.
  - The report and the Discord post show who holds the coin and what share of supply, plus who
    held it and has sold out.
  - The derived account addresses matched 1,191 of 1,199 real holder accounts in the 22 Oct
    database. The other 8 are pool vaults.
  - Recorded and shown only; it does not change the score. There is no outcome data on these
    wallets yet.
- **Launch buys (`analysis/launch_trades.py`, gate `launch_buys`).** Reads the coin's first 40
  trades on its pump.fun bonding curve, back to the create transaction, with the verified
  TradeEvent decoder. This works for migrated coins too: v2.5.7's chart check could not see a
  migrated coin's launch.
  - **FAILS on any of:**
    - other wallets bought 10%+ of supply in the creation block (bundled);
    - other wallets bought 25%+ in the first 10 seconds (sniped);
    - dev plus early buyers bought 35%+.
  - **UNKNOWN** when the launch is more than 5,000 transactions back or unreadable.
  - Read once per coin.
- **Snipers still holding (gate `sniper_holdings`).** The first-seconds buyers' current balances,
  read on-chain. FAIL at 15%+ of supply. This is Axiom's "snipers H".
- **Tiny insider trades (`analysis/micro_trades.py`, gate `insider_micro_trades`).**
  - In the verified live-trade sample, trades under 0.01 SOL count as tiny.
  - "Insiders" are top-10 holders, launch snipers/bundlers and linked-wallet clusters.
  - 3 or more tiny trades by insiders FAIL. The overall share of tiny trades is recorded.
- **Creator history (`creator_stats` table, `analysis/creator.py`).** Every launch and graduation on
  the PumpPortal feed is counted per creator wallet; at startup it is filled from the stored feed
  (about 65 hours on the 22 Oct database).
  - Before this, creator history was UNKNOWN on every coin: RugCheck sent no launch list.
  - **Not a reject by default.** On the 22 Oct database (183 coins with complete price paths and a
    known creator, counted point-in-time), coins whose creator had 5+ earlier launches or 2+
    migrations were bad (rug or pump-then-rug) 41% of the time. All other coins were bad 58% of
    the time.
  - Turn it on with `NORTHSTAR_CREATOR_HISTORY_BLOCKS=true`.
- The new gates are risk thresholds: a FAIL rejects the coin, which is then not watched. Missing
  data (UNKNOWN) never blocks on its own. Early-watch candidates on the curve go through the same
  filters before they are posted.
- **"Coins being watched" is honest now.**
  - Launches that only the pump.fun feed reported, with no research scheduled, leave the list
    after 30 minutes. The curve radar still brings them back on real buying.
  - The dashboard shows three separate numbers: on the watch list, queued for research, and new
    launches waiting (not watched).
- The launch-buy, sniper and tiny-trade thresholds are first settings, not measured. The bot had
  no stored launch trades to test them on. Every report stores the metrics so they can be checked
  against outcomes.
- Tests: `tests/test_rug_filters_v259.py` (29).

## 2.5.8 — 2026-10-22 — rugged early watches are closed, double-warning coins rejected

The first v2.5.7 log showed early-watch posts at 0.11x to 0.15x of their call price still being
tracked and edited every cycle. They would have stayed open for up to 72 hours, and the edits
caused Discord rate-limit (429) warnings.

- **Rug close (`early_tracker.py`).** Once a verified on-chain reading puts an early watch at or
  under `NORTHSTAR_EARLY_WATCH_RUG_CLOSE_X` (0.3x, the same line the learning labels use for a
  rug) of its call price, the post is edited once to "🪦 Rugged … removed from the watch,
  tracking ended" and the watch is CLOSED. The stop-loss message still posts first. A failed or
  missing read never closes a watch; 0 turns the rule off. The value is clamped to 0–0.9.
- **Two or more serious warnings → REJECT, not watch (`analysis/unified.py`).** The same log
  had coins on the watch list as CAUTION with two serious warnings, some with a score under the
  48 watch line. Examples: "one wallet holds 13.7% (crashed 92% of 157 times)", brand-new wallets
  holding 16.5%, a copycat ticker. The engine's own measurement says two serious warnings behave
  like HIGH RISK (95% crashed). Such a coin is now rejected (reason class SERIOUS_WARNINGS) and is
  not watched. A coin with one serious warning can still be watched, never called.
  `NORTHSTAR_U_REJECT_DOUBLE_WARNING=false` restores the old behaviour.
- Tests: `tests/test_early_tracker.py::RugClose`; `tests/test_unified_engine.py` updated for the
  double-warning rule.

## 2.5.7 — 2026-10-22 — the start of a coin must be organic

Operator request: no obvious rugs on the watch list. The examples were coins whose chart started
with a giant jump or a few huge steps, and coins that had already rugged but stayed watched.

- **Launch-shape check (`analysis/launch.py`, gate `launch_shape`).** It reads 1-minute candles
  from the coin's earliest pool, the pump.fun curve when listed, from creation onwards, in one
  GeckoTerminal call. The gate FAILS on:
  - an **instant jump**: 6x or more in the first 2 minutes of trading (`NORTHSTAR_LAUNCH_MAX_FIRST_JUMP_X`);
  - a **staircase**: 85% or more of a 3x+ rise made in 3 one-minute candles, with trades in under
    40% of the first 30 minutes;
  - an **already dumped** coin: price at or under 35% of its high since launch
    (`NORTHSTAR_LAUNCH_MAX_DRAWDOWN_X`).

  A FAIL rejects the coin as a risk threshold, so it is not watched and gets no second look. No
  candles means UNKNOWN: recorded, never taken as organic, and not blocking on its own. Every
  report stores the launch metrics (`report.launch`), so the thresholds can be measured against
  outcomes and tightened.
- **Launch rules only on verified launch history.**
  - The instant-jump and staircase rules apply only when the candles come from the pool the coin
    started on (its pump.fun curve, or an AMM pool it launched on directly) and the first candle
    is within 3 minutes of that pool's creation.
  - Anything else is INCOMPLETE: a chart source that indexed the pool late, or a pump.fun coin
    seen only on its post-migration pool. For those, only the drawdown from the highest price
    seen can fail the coin, and a clean-looking partial chart is UNKNOWN, not organic.
  - Early-watch coins on the bonding curve get the same check, on the curve's own candles.
- **The watch list is bounded.** A watched coin is re-checked at most
  `NORTHSTAR_WATCHLIST_MAX_RECHECKS` (12) times, about 2 hours, then leaves the list (STALE). A
  surge or a new listing brings it back. A watched coin that dumps meanwhile is rejected at its
  next look by the launch-shape check.
- **Not verified against history.** The stored database has no launch candles, so these
  thresholds come from the chart shapes, not from measured outcomes. They are set wide to catch
  only the obvious cases.

## 2.5.6 — 2026-10-22 — the brain learns clean runners; no learning posts

Operator request: better learning, and no "learned something" posts in Discord.

- **A pump-then-rug is a loss for the brain.** A coin that hit 2x before 0.6x but then fell to
  0.3x of the entry price within its 4-hour price path used to be trained as a winner. On the
  22 Oct database that was 128 of the 245 path "winners". The brain now learns only the runs
  that held up (`brain.clean_outcome`). The shared label, the audit and the shadow report are
  unchanged; they show pump-then-rugs separately.
- **The brain must prove itself on the newest coins too.** It is trusted only when walk-forward
  AUC is at least `NORTHSTAR_BRAIN_MIN_AUC` (0.58) overall *and* on the most recent coins. The
  recent bar used to be 0.03 lower.
- **Result on the 22 Oct database.** Retrained on the same coins, the old labels scored 0.50
  (coin flip) and the new ones 0.69 overall, 0.66 recent. So the brain would be trusted again
  and get its bounded say in calls: at most ±12 score points. It can never clear a safety
  check or create a call on its own.
- **No learning messages in Discord by default** (`NORTHSTAR_LEARNING_ANNOUNCE=false`). Changes
  and trust changes go to the log (`learning: ...`); set it to `true` to post them again.

## 2.5.5 — 2026-10-22 — follow fewer coins; test safer call profiles

Operator decisions (22 Oct): stop following coins that fail, unless they have good numbers, a good
narrative and no high holder concentration; keep a 10% learning sample of rejections; test
safer call profiles before changing live calls. **Scoring, thresholds and safety gates are not
changed. Live callouts are not changed.**

- **A rejected coin is not re-researched any more** unless all three hold (`tracking_policy.py`):
  - good numbers: score at or above the watch line, liquidity at least the minimum, not collapsing;
  - a good narrative: enough evidence items, and the narrative category scored 0.6 or better;
  - spread holders: the holder-concentration check ran and passed, and there is no whale, cluster
    or developer-holdings hazard.

  Even then, a coin gets at most `NORTHSTAR_REJECTED_MAX_RECHECKS` (6) looks. Proven traps,
  prefilter rejections and rejections without a full analysis are never re-checked. A rejected
  coin whose data has not changed is not re-queued. A radar surge or a new feed event still
  brings any coin back for one look.
- **Missing-data coins are retried at most `NORTHSTAR_INSUFFICIENT_MAX_RECHECKS` (8) times**,
  instead of forever.
- **Never-researched coins leave the watch list after `NORTHSTAR_DISCOVERED_MAX_AGE_HOURS` (6).**
  On the 21 Oct backup that is 44,340 of the 50,846 coins the Watching tab counted. New
  information (a surge, a migration, a new profile or boost) brings a coin back; a repeat of an
  old event does not.
- **Price tracking:** every call, watch and insufficient-data decision is tracked, as is every
  second-chance rejection, plus a fixed 10% sample of the other rejections
  (`NORTHSTAR_REJECT_TRACKING_SAMPLE_PCT`). On the backup's last 24 h that is 723 of 7,766
  rejections instead of all of them. The sample is fixed by mint (a coin is always in or always
  out), so the audit and the learning brain still measure false rejections, on less data.
- **Size bounds.**
  - Finished coins nobody follows are forgotten after `NORTHSTAR_CANDIDATE_RETENTION_DAYS` (7).
    Those are stale coins, and rejected or insufficient-data coins with no re-check scheduled.
    Coins with a callout, an early signal or open price tracking are always kept.
  - Learning rows are kept only as long as the learner and the brain read them (21 days instead
    of 45).
  - `NORTHSTAR_DB_VACUUM_ON_START` (off by default) shrinks the file at startup once 20% of it
    is free space. It needs free disk about the size of the remaining data, so on a host with a
    tight disk limit, shrink a downloaded copy instead (docs/DATABASE.md).
- **Automatic size control**, in the hourly retention pass (docs/DATABASE.md):
  - analysed research reports lose bulky detail after 24 h;
  - snapshots that only repeat a coin's previous decision are removed;
  - callout price updates are thinned to the 5-minute high and low after 24 h;
  - market snapshots are kept 48 h;
  - freed space goes back to the disk on its own (incremental auto-vacuum, switched on by one
    full VACUUM or for new databases).

  On the 22 Oct database (514 MB): 236 MB, with callouts, tracking, outcomes, learning shadows,
  brain training rows, `audit` output and every callout's peak, low and event order unchanged.
- **Shadow testing of call profiles chosen from the stored price paths** (no live change):
  - `pool_age_5m`;
  - `liquidity_50k,pool_age_5m`;
  - `deep_liquidity` ($100K, the safe extreme).

  They're recorded next to every live decision with the audit's relaxations. The
  $100K-and-1-hour profile is no longer a default: on 17 coins it had no 2x at all. A profile
  only ever turns a STRONG call into WATCH, but whether a coin fits it is recorded for every
  analysed coin. See docs/DECISION_ENGINE.md, "Upside vs rug".
- **The shadow report separates clean runners from pump-then-rugs** (0.3x after the 2x on the
  ordered path). It shows calls per day and compares the coins each profile admits with the ones
  it excludes.
- **A re-check needs objectively strong numbers.**
  - Liquidity of `NORTHSTAR_SECOND_CHANCE_MIN_LIQUIDITY_USD` ($35K) or more.
  - Buyers ahead of sellers over 1 h, and volume keeping its 6-hour pace (not fading).
  - Score at or above the watch line *without* the learning brain's tilt.
  - Safety established: an unresolved critical check never earns a re-check, alongside the
    narrative and holder conditions above.
- **The learning brain cannot drive calls while unvalidated.** An untrusted brain (walk-forward
  accuracy under `NORTHSTAR_BRAIN_MIN_AUC`, 0.58) adds no tilt, and the brain never clears a veto.
  This was already the behaviour; tests now cover it alongside the re-check rule.

## 2.5.4 — 2026-10-22 — database size and fair outcome tracking

Measured on a copy of the 21 Oct 17:22 backup (394 MB, v2.4.1 era). The production database was
not read or changed. Commands, backup and recovery: `docs/DATABASE.md`.

**Discovery deduplication.** 74% of `discovery_events` rows (194,828 of 262,785) were exact repeats
of the last event for the same coin and feed. A repeat now increments `seen` and updates `last_ts`
instead of adding a row; a changed payload is still a new row. Existing repeats are merged once,
in short batches. The first launch event (creator, curve key) and the latest profile, the two
rows Northstar reads, were checked for every coin on the copy and are unchanged. Retention uses
the time a row was last seen.

**Snapshot compaction.** Prefilter-rejection reports older than `NORTHSTAR_SNAPSHOT_COMPACT_HOURS`
(24) lose the provider metadata around each value. The reasons, the decision and every market
number stay, so the brain's feature vectors are unchanged. A coin's latest snapshot, snapshots an
alert points to and anything that reached a security check are never compacted.

**Retention.** Bonding-curve events are kept `NORTHSTAR_CURVE_RETENTION_HOURS` (6), down from a
fixed 7 days; research reads only the last 5 minutes. The discovery and snapshot retention days
are unchanged (4 and 7).

**Tracking fairness.** The tracker used to price-check the 600 OLDEST open decisions every pass.
With about 2,600 open, decisions from the latest ~12 hours got no checks at all, and at least 10%
of finished rows had 0 checks. Each pass now checks called-out coins first, then decisions never
checked, then the least recently checked, up to `NORTHSTAR_TRACKING_POLL_LIMIT` (600; range
1–1200). With the backup's 2,100–3,300 open decisions, each gets 120–180 checks a day (30 needed)
for at most 20 DexScreener requests per 2-minute pass. The log warns hourly when coverage would
drop below 60 checks a day.

**Database safety.**
- **Short transactions.** The one-time merge and the curve cleanup run in batches of about
  2,000 rows, with the WAL truncated between them. One big transaction had held the database
  for 5.3 s and grown the WAL by 146 MB.
- **Interruptions.** Stopping part-way loses nothing and counts nothing twice; the merge is
  marked done only after its last batch.
- **`python main.py db` is a dry run by default.** `--apply` runs the pass. `--apply --vacuum`
  also shrinks the file, and refuses while the bot holds its lock or when free disk is under
  about 1.1 times the database size.
- **Setting bounds.** Negative retention hours mean off. The poll limit is clamped, because
  SQLite reads a negative LIMIT as "every row".

**Result on the copy.** 394 MB → 179 MB after the pass and VACUUM. These were identical before and
after:
- alerts, price history, decision tracking, outcomes, shadows, early signals, snapshot and
  candidate counts;
- every stored reason;
- the brain's inputs and training rows (4,170 rows, 289 runners);
- `python main.py audit` output.

## 2.5.3 — 2026-10-21 — shadow testing

Record-only comparison of the live decisions with a proposed scoring policy. **No live decision,
callout, threshold or stored snapshot changes.** Report: `python main.py shadow [--days 7] [--json]`.

- **`northstar/shadow_test.py`, table `shadow_tests`** (added automatically). For every coin the
  engine decides, one row holds the live decision and the proposed one for the same report, both
  score breakdowns (points per category, suspicion penalty, brain tilt, completeness), the reason
  class and reasons of each, and which proposal rules changed something.
- **Same scoring code.** The proposal re-derives the decision with the unified engine from the live
  result's own evidence, brain tilt and AI read. With no rules it reproduces the live decision
  exactly (tested). Proven traps (authorities, transfer hooks, liquidity removal, no exit) are never
  relaxed.
- **Proposal p1** (from the 21 Oct history audit; `NORTHSTAR_SHADOW_TEST_RULES`):
  `lp_unverified_warn`: on PumpSwap, an unlocked LP reported by RugCheck but not read on-chain
  caps at WATCH instead of a hard veto. `holder_gate_warn`: holder concentration over the bar
  caps at WATCH instead of rejecting. Extra unified-engine settings can be tried with
  `NORTHSTAR_SHADOW_TEST_OVERRIDES` (JSON, `u_*` only); each proposal is stored under its own name.
- **Outcomes without future data**, from data Northstar already records (no extra API calls): the
  learning shadow's minute path recorded in the same pass, else the 24h tracking of the same
  snapshot. Rows are labelled only after the whole window; INCOMPLETE stays unlabelled.
- **No duplicates**: one row per snapshot and policy; a coin re-researched within 30 minutes
  with the same live and proposed decisions is not recorded again. The report counts each
  coin once per decision pair.
- **Coins rejected ONLY for the unverified PumpSwap LP are now analysed to the end**
  (`NORTHSTAR_SHADOW_TEST_DEEP_RESEARCH`, default on), so the LP proposal can be compared.
  Their live decision is still REJECT: the unified engine's hard veto, class SAFETY_FAIL. Their
  reasons read "hard veto: rugcheck danger: ..." instead of "gate failed: rugcheck_danger ...".
  Each of these coins now costs the normal holder and trade lookups.

## 2.5.2 — 2026-10-21 — decisions from the live history

Measured on the live database (17–21 Oct, v2.4.1 running): see the project doc
"northstar-history-audit-2026-10-21". Operator decisions taken on those numbers:

- **Rug scalps are off by default** (`NORTHSTAR_SCALP_ENABLED=false`). They were 37 of 41 calls and,
  replayed on their real price paths with the learned scalp plan, lost about 7% per call (median
  −12%, 10 wins of 28, worst −97%). The code stays; `true` turns them back on.
- **A DexScreener/RugCheck price disagreement blocks calls again** (it was a confidence note in
  2.5.0). Coins rejected for it rugged 64% of the time (23 of 36). Graded as a risk threshold.
- **Unlocked LP keeps rejecting.** RugCheck's "Large Amount of LP Unlocked" rejected 131 coins (all
  hand-made PumpSwap pools, 0% locked) that did not rug more than average, but the LP really can be
  pulled, so it stays a hard stop. Northstar's own on-chain LP read confirmed none of them; it now
  accepts the token on either side of a PumpSwap pool and records why a read failed
  (`report.lp_onchain_check`, shown by `python main.py audit`).
- **Holder concentration keeps rejecting** (unchanged; it did not separate rugs from runners on
  this history, to be re-measured).
- **The audit compares like for like**: a failed gate against coins that reached the same checks
  and passed, a red flag against fully analysed coins without it. Before, everything was compared
  with all decisions, mostly dead coins, which made every signal look "weak". Stored gates are
  now replayed exactly as recorded.

## 2.5.1 — 2026-10-21 — false rejections vs rugs called

Targeted fixes to the call paths that skip the unified engine, plus a way to measure both
error types on the bot's own history. Everything in 2.5.0 is included.

- **`python main.py audit`** (`northstar/outcome_audit.py`): counts rugs called per call type
  (standard, rug scalp, early narrative, manual) and runners rejected per rejection reason; shows
  for every failed gate and rug-check red flag whether it catches rugs or only blocks runners;
  replays the stored reports through the current engine (before vs now) and replays the new
  early-call gate on past early calls. Ordered price paths are used where they exist; when only
  the high and low are stored, coins that did both are reported as MIXED, never guessed.
- **Early narrative calls no longer treat unchecked safety as clean.** Mint/freeze authority,
  token extensions, holder concentration and creator holding must have RUN and PASSED; a coin
  whose holder check was skipped or failed to load is held back.
- **Early calls are held on any serious (high/critical) rug-check red flag.** They bypass the
  unified engine, so a single serious flag (verdict CAUTION) used to pass — e.g. one wallet
  holding 14% under a loosened 15% holder threshold.
- **Rug scalps are never posted when the exit is blocked**: no Jupiter sell route, ruinous exit
  cost, a collapsing price, or a rug-check trap flag. The scalp check used to read gates only.
- **Curve stage: unmeasured is not the same as missed.** Buys that carry no SOL amount make the
  buy flow UNMEASURED (it was counted as 0.00 SOL — live log: 79 verified trades rejected for
  "buy flow 0.00 SOL"), and a missing market-cap history makes the trend unmeasured. A coin whose
  only problems are unmeasured inputs is INSUFFICIENT DATA and re-checked, not REJECTED.

- **One outcome label for every learning path** (`brain.label_path` / `brain.label_extremes`):
  runner = 2x before 0.6x, rug = 0.3x before 1.3x. Events whose order is unknown (both inside one
  candle, or a 24h high and low that conflict) are AMBIGUOUS, and histories that stop early are
  INCOMPLETE; neither is trained on. The brain's 24h-tracking rows no longer count any coin whose
  high touched 2x as a winner, shadow evaluation stores the label, the red-flag statistics count
  runs and rugs by it (the `crash` figure the rug check reads is unchanged), and the outcome audit
  uses the same functions. Old databases gain `shadow_calls.label` and `flag_outcomes.rug/labelled`
  in place; existing rows are kept.

## 2.5.0 — 2026-10-19 — scoring and decision audit

Why almost everything was rejected or INSUFFICIENT DATA, and why one report could show two
scores. Full write-up: `docs/DECISION_ENGINE.md`, section "v2.5". Regressions:
`tests/test_decision_v25.py`.

- **One score, one decision, everywhere** (`northstar/final.py`). `/research`, the CLI, the
  dashboard, the stored snapshot, the candidate row and the alert now all carry the unified
  score, risk and decision. The old v2.3 score (the "41.2" next to "58/100") is kept only as
  `legacy_score_v23`. The rug check shows "rug-risk points", not a second "/100".
- **The engine's live passes no longer desynchronise the decision**: after live context, the
  learning brain and the AI read, the action is re-derived from the final unified result and
  the snapshot row is rewritten.
- **Publication policy is computed on its own**, so cooldown, daily cap and the callout
  market-cap minimum apply even when a gate failed or was unknown.
- **Missing optional data no longer rejects.** Narrative shortfall, an unverified trade sample
  and unknown non-trap checks are EVIDENCE_GAPs; extended/fading/weak-buying are TIMING. Both cap
  at WATCH and lower confidence; neither raises risk. (`NORTHSTAR_U_NARRATIVE_BLOCKS=true`
  restores the old hard rule.)
- **Concentrated liquidity is judged, not left unknown.** LP safety is weighed across every pool;
  a verified burned PumpSwap floor that clears the liquidity minimum passes, DLMM-only liquidity
  fails, unread pools stay UNKNOWN. A measured partial LP burn is a FAIL, not an UNKNOWN.
- **Provider price disagreement is a contradiction, not a rejection.**
- **No fact is counted twice** (gate + verdict flag + scoring check linked; verdict level
  deduplicated).
- **Safety is the veto layer only**; clean safety checks no longer add score points, so a
  RugCheck outage no longer lowers the opportunity score. The narrative cap now actually binds
  (default 10).
- **New checks**: price collapse (dump in progress) and exit cost (5% warn / 15% block).
- **Trades are sampled from the decodable PumpSwap pool** when the deepest pool is DLMM/CLMM;
  every pool of the token is excluded from holder maths.
- **Decision funnel telemetry**: `funnel_events` table, `python main.py funnel`, dashboard card.
- **RugCheck "Large Amount of LP Unlocked" vs an on-chain burn read.** In the live log this
  danger alone hard-rejected coins before analysis. When Northstar has read the LP burn on-chain,
  a contradicting RugCheck LP danger becomes a provider conflict (lower confidence); every other
  RugCheck danger, and the LP danger without an on-chain read, still fails the gate.
- Score scale changed: re-run `scripts/validate_decision_engine.py` on the live database before
  relying on the 68/48 thresholds. Includes everything in 2.4.1 and earlier.

## 2.4.1 — 2026-10-19 — what the first live day found

Everything here came from watching v2.4.0 run on Wispbyte, not from a test.

- **Every actively traded pump.fun curve was being thrown away.** The curve decoder worked out
  whether a curve was SOL-quoted by checking that the account's own lamports covered its
  `real_sol_reserves`. Pump.fun keeps the reserves elsewhere: read from mainnet, curve
  `CgfbhqzqSHoygUwdywzRo8o9wZEbLtRAVYw8fXkPjiaF` held 1,493,520 lamports of rent against
  870,873,131 lamports of reserves. Since `real_sol` is zero until the first buy, dead coins
  passed and coins with real buying were dropped as "not SOL-backed" — precisely the coins the
  early-call path exists to find. `tests/test_curve_quote.py` now pins the decode against that
  real 166-byte account.
- **New launches are no longer researched on a timer.** The bot was researching 1,740 coins an
  hour, 44% of them returning INSUFFICIENT DATA because they had neither a pool nor a trade yet.
  A launch is now recorded and left alone until the curve radar sees it in the early band with
  real buy flow — one DexScreener batch call covers every recent launch — and only then is it
  researched. `NORTHSTAR_RESEARCH_NEW_LAUNCHES_ON_TIMER=true` restores the old behaviour.
- **Top holders and a thesis block on every callout.** The top wallets by share of supply, each
  with what the bubblemap actually traced — age, fresh, launch buyer, connected group, insider —
  then the narrative, what attention is behind it, what argues against it, and the AI's verdict
  with its reasoning.
- **One risk verdict per message.** A callout could show the unified engine's risk class on the
  decision panel and the older RiskLevel below it, so the same message said LOW and INCOMPLETE
  at once. The unified score is also capped at 99: a round 100 reads as certainty.
- **Secrets no longer reach the screen through error messages.** A Discord token pasted with a
  trailing space produced an "Illegal header value" error that quoted the whole Authorization
  header onto the dashboard. Errors are redacted, and the self-test now detects the whitespace.
- **Wikipedia's most-read feed was dead** since it shipped: Wikimedia rejects a User-Agent with
  no contact URL, so it returned 403 every time. Fixed.
- **Jupiter's default rate drops to 20/min**; at 50 the keyless API throttled within seconds of
  starting, and a throttled exit check costs the sellability signal.
- Includes everything in 2.4.0 and earlier. 408 tests pass, 4 skipped.

## 2.4.0 — 2026-10-19 — one decision engine

Northstar answered the same question four times: an opportunity score, a risk level, a rug
verdict and a learning brain, with the AI judge on the early-call path only. They penalised the
same things more than once, the one component measured against real outcomes (the rug verdict)
ran AFTER the decision and only fed the embed, and the brain was bolted on in the engine where it
matched blocker strings. v2.4.0 replaces that with a single engine
(`northstar/analysis/unified.py`, full write-up in `docs/DECISION_ENGINE.md`).

- **One result per coin**: score 0–100, risk LOW/MODERATE/HIGH/EXTREME, decision STRONG
  OPPORTUNITY / WATCH / CAUTION / REJECT / INSUFFICIENT DATA, plus confidence, data completeness,
  positives, negatives, entry assessment, what would invalidate the thesis and what should force a
  re-check. Score, risk, confidence and completeness stay separate numbers; the score is not a
  probability of reaching 2x.
- **Nothing is counted twice.** Every observation is one entry in an evidence ledger with one
  owning category and a provenance grade (FACT / INFERENCE / UNKNOWN / UNAVAILABLE). The gate and
  the flag that describe the same hazard now share one canonical key, so holder concentration is
  one warning instead of three.
- **Safety is not compensatory.** Hazards never enter the score: confirmed traps reject, serious
  warnings cap the decision, suspicions cost a listed penalty, and an unresolved TRAP check
  returns INSUFFICIENT DATA. A provider timeout is never treated as evidence of safety. Where the
  hand-written severity list and the learner disagree, the measured crash rate wins.
- **A call has to be callable**: essential categories (safety, liquidity, holders, structure) each
  need real evidence, entry conditions have to be right now, and a STRONG call must still clear
  the bar with its narrative points removed — a good story on a bad chart cannot be called.
- **The brain is a bounded tilt** of at most 12 points, only when it is trusted; it can lower or
  lift a score but never clear a veto. The old string-matching promote/veto path is kept for
  `NORTHSTAR_UNIFIED_ENABLED=false`.
- **The AI judge now reaches the main callout path**, asked only about coins already heading for a
  call or a watch, contributing 6 of the 16 narrative points. Follower growth, tweet velocity and
  sentiment remain explicitly unavailable — there is no paid X access and they are not invented.
- **Contradictions are shown, not averaged**: deep liquidity with no sell route, a locked-LP claim
  the pool disagrees with, liquidity above market cap. Each costs confidence and is printed.
- **One decision panel** on the callout and in the dashboard coin drawer, the latter with a
  per-category table of points, weight, coverage and the evidence behind each.
- **Every v2.3.0 threshold is preserved** (extended price, narrative evidence, volume
  acceleration, buy/sell ratio, liquidity floor, cooldowns, caps) because several were tuned by
  the learning loop. Publication policy can only hold a call back, never create one.
- **Validation** (`scripts/validate_decision_engine.py`, replayed on 509 unique tracked coins):
  ranking AUC 0.638 for the unified score against 0.498 for the old opportunity score, but only
  63 coins have a complete report, and a liquidity-only baseline scores 0.596 — so the ordering
  edge is real and modest. Top 50 by score reached 2x 28.0% of the time against a 16.3% base
  rate. No weights are fitted, no win rate is claimed, and the database cannot establish whether
  2x came before or after a drawdown. Limitations are listed in the doc.
- Tests: 396 pass, 4 skipped. 26 new cases cover a clean coin, a critical vulnerability beating a
  high score, a failed sell quote, thin liquidity, missing holder data, conflicting providers,
  narrative versus structure, volume versus concentration, an unavailable AI provider, RugCheck
  down with and without an on-chain LP read, a stale score, a deteriorating called coin,
  unresolved outcomes, and a walk-forward leakage guard.
- No schema change. Rollback: `NORTHSTAR_UNIFIED_ENABLED=false`, or run the previous folder
  against the same `data/`.
- Includes everything in 2.3.0 and earlier.

## 2.3.0 — 2026-10-19 — free AI providers, CoinGecko narratives, real exit checks

- **The AI judge now works without paying for Claude credits** (`ai_judge.py`). Set
  `NORTHSTAR_AI_PROVIDER` to one of `groq`, `gemini`, `openrouter`, `cerebras`, `mistral`,
  `github`, `local` or `anthropic` and paste a key; each preset carries its own endpoint and
  model, and three API shapes are handled (Anthropic Messages, OpenAI-compatible, Gemini
  generateContent). The hourly cap, daily budget, strict answer parsing and caching are
  unchanged — only where the answer comes from. Free tiers cost nothing, so the price settings
  default to 0. These endpoints are blocked from the build container, so the request shapes are
  covered by tests against fakes; the live call has to be confirmed on the host that runs the bot
  (the self-test NARRATIVE section does that in one click).
- **CoinGecko narratives** (`providers/coingecko.py`, `events.py`): the attention feed now also
  carries CoinGecko trending coins and the sectors with the strongest 24 h market-cap change, so
  the judge sees which *crypto* narratives are hot, not only world news. Uses the CoinGecko key
  the bot already has.
- **Exit check / sellability** (`providers/jupiter.py`, free, no key): before a coin can be called,
  the bot asks Jupiter what a $100 / $500 / $1,000 sell back to SOL would actually return, and
  records the price impact, the biggest size that still routes, and the first size that cannot be
  filled. New red flags: cannot sell even the smallest size (critical), a normal size cannot be
  filled (high risk on its own), 15%+ impact (high), 5%+ impact (medium). The exit cost is shown
  on the call, and `sell_impact` / `sell_blocked` are now learning signals.
  This closes the biggest gap in the audit: a 3x on $5k of liquidity you cannot exit is not a win.
- Includes everything in 2.2.0 and earlier.

## 2.2.0 — 2026-10-19 — early narrative calls (30–50k)

- **Live attention feed** (`events.py`, free, no keys): pump.fun launch waves (a word appearing in
  many launches by many DIFFERENT creators in 30 min vs the 6 h before), Google News, Google Trends
  "trending now", most-read Wikipedia. Verified on real launch history: it surfaces the live waves
  (e.g. 91 launches by 33 creators around one word) and ignores single-bot ticker spam.
- **AI narrative judge** (`ai_judge.py`, Claude): judges a coin's narrative against that live feed —
  fit, originality, attention, timing (early/peak/late/dead), red flags, 0–100 score, CALL/WATCH/PASS.
  Deployer-written text is passed as quoted data and the answer is parsed strictly (bad JSON, unknown
  verdict or missing score = no call). Hard hourly and daily-dollar caps with per-call spend recorded;
  results cached 30 min or until the coin moves 1.5x. Roughly $0.0002 per judged coin.
- **Early narrative calls** (`early_calls.py`): bonding-curve coins at 30–50k that pass flow, safety
  and rug-check gates AND get a CALL from the judge are posted to **VIP** as "⚡ EARLY NARRATIVE CALL"
  with the AI reasoning, live attention lines, flow, an early plan and the rug check.
- **Curve radar**: scans recent launches every 2 min and researches coins entering the band immediately.
- **Tracking through migration**: market cap from the bonding curve on-chain, then from the PumpSwap
  pool; records the peak, posts plan pings, and posts a proof copy to the **free channel** at 1.5x.
- Self-test gained a NARRATIVE section (API key, feeds, spend today, early calls in 24 h).
- Fixes found by the new tests: a stored unparseable AI answer could be served as a cached verdict;
  the rug-check text crashed on a partial verdict; the curve radar could run before its table existed.
- Includes everything in 2.1.0 and earlier.

## 2.1.0 — 2026-10-19 — smarter coin finding

Tested on 731 real tracked coins before shipping (accuracy = walk-forward AUC on coins the model had
not seen; 0.50 = coin flip):
- New signals for the learning brain, each kept only because it improved accuracy on all three
  targets (1.5x, 2x, 3x):
  - **Hot ticker**: other launches with the same ticker in the last 6 h. Copied tickers ran 17% of
    the time, unique tickers 1%. Shown on calls as "🔥 Hot ticker: N other $X launches in 6 h".
  - **DexScreener community takeover** (CTO).
  - **Trades so far** (24 h and 1 h; fewer = earlier) and **1-hour volume vs market cap** (churn).
  - Runner accuracy 0.664 -> 0.702; with the brain's own first signal search 0.72. Top-10% picks
    reached 2x 33% of the time (average coin 13%).
- **The brain tests signals by itself**: each training it tries adding one experimental signal
  (volume vs 6 h pace, 24 h change, trade sizes, 6 h buy share, market heat, 6 h trades) or dropping
  a weak one, and keeps the change only if accuracy on unseen coins rises by 0.01+ without the most
  recent coins getting worse. Changes are logged and announced.
- **Market heat**: median 1-hour move of the coins researched in the last 30 min (tested live).
- History is rebuilt with these signals at startup, so the brain uses them from day one.
- `scripts/feature_lab.py`: rerun the experiment on your own database any time.
- Includes everything in 2.0.1 and earlier.

## 2.0.1 — 2026-10-19

- Trade plan text: "sell 33%, then trail the rest" (a comma was missing).
- Includes everything in 2.0.0 and earlier.

## 2.0.0 — 2026-10-19 — second brain

Rug detection (a pro's checklist, automated)
- **Rug check verdict** on every coin: RUG LIKELY / HIGH RISK / CAUTION / LOOKS CLEAN / NOT ENOUGH
  DATA, with the red flags, the good signs and what could not be checked. Shown on calls,
  `/research` and the dashboard. Never loosens a safety gate.
- **Calibrated on real outcomes**: on 731 coins followed for 24 h, the average coin fell below 0.3x
  41% of the time; coins with one wallet holding 8%+, concentrated buying, wash trading, a copycat
  ticker, mostly brand-new wallets or a dev holding/selling crashed 85-100% of the time (now
  serious flags). "Liquidity dropped from its high" crashed only 27% (no longer a trap). On that
  history HIGH RISK coins crashed 95% of the time, CAUTION 67%.
- **The verdict keeps learning**: crash rate per red flag is measured from the bot's own tracking,
  shown next to each flag, and a medium flag that keeps proving deadly is upgraded automatically.
- **On-chain bubblemap**: funding traced two hops (dev -> middle wallet -> holder), wallets funding
  each other, wallet age for every top holder, supply held by wallets under 24 h old, launch
  buyers. Wallets funded from a busy exchange/router wallet are no longer linked by that alone
  (it caused false "connected wallets").
- Dashboard: interactive bubblemap + holder table (supply, age, funder, group) in every coin view;
  "What predicts a rug" table in the Learning tab.
- The learning brain also learns from the rug-check numbers (risk score, fresh-wallet supply,
  linked groups, 2-hop links, exchange-funded holders).
- Call embeds are trimmed to fit Discord's 6000-character limit (bigger embeds could fail to post).

Control panel
- Password-protected web dashboard (Overview, Watching, Calls, Learning, Settings, Self-test).
- Self-test button / `python main.py selftest --code`: settings, Discord login and channels,
  every data source, database, live loops, learning and the full test suite. Never posts.
- Dashboard settings now actually read from `.env`; Wispbyte `SERVER_PORT` used automatically.

Learning brain
- Learns what makes a RUNNER (2x before -40%) from the bot's own history (shadow price paths +
  24 h decision tracking). Walk-forward validated; on real history AUC 0.66, top-10% picks ran 2x
  twice as often as the average coin. Trusted brain can veto weak calls or promote coins held back
  only by score bars; never a safety check.
- Tuner retunes selection, the brain's odds bars, the runner plan and radar thresholds (from surges
  and near-misses), one step at a time, only when it holds up on recent unseen coins.
- Runner plan defaults: 33% at 2x, 33% at 4x, 40% trailing stop, -35% stop, 45-min time stop.

## 1.4.0 — 2026-10-19 (early)

- LP burn verified on-chain for PumpSwap pools when RugCheck is down (it blocked every migrated
  coin, including a live radar surge).
- Reddit removed entirely; social attention = the free checks.
- Deleted Discord callouts are no longer re-edited every cycle.

## 1.3.0 — 2026-10-18 (evening)

- Surge radar (30 s DexScreener scan, immediate research), momentum gate, trade plan with entry,
  TP1/TP2, stop, trailing and time stop, rug-scalp calls with a RUG banner, CoinGecko candles for
  peaks, "Peak since callout", live embed edits, self-learning v1, 12 review fixes.

## 2026-10-18 — full audit

Verification
- Pump.fun bonding-curve trades are decoded from the program's TradeEvent and checked against the trader's token balance (`analysis/pump_trades.py`). Live: ~90% of in-window transactions verified, 0 decode errors.
- NEW: PumpSwap swaps (≈94% of post-migration pools) are decoded from the program's BuyEvent/SellEvent, bound to the selected pool and checked against the trader's token balance (`analysis/pumpswap_trades.py`). Before this, no post-migration swap was ever verified, so demand stayed INFERRED and capped at ~2/20 points. Trade stats are VERIFIED only when at least `MIN_VERIFIED_SWAPS` swaps verify; other DEXes stay INFERRED.
- Creator-dump findings from verified swaps are labelled CONFIRMED/VERIFIED; the post-callout creator-sell warning no longer says "VERIFIED on-chain" for undecoded candidates.
- Pump.fun curves quoted in a token other than SOL (newer curve accounts record a quote mint) are reported as INSUFFICIENT_DATA instead of having their reserves read as SOL.
- Event layouts checked against the official IDLs (pump-fun/pump-public-docs: pump.json, pump_amm.json).

Fixes
- Early-watch posts are tracked and edited every 2 minutes from the on-chain curve (`early_tracker.py`); deleted posts stop being tracked.
- Discord: the publisher retries transient connection failures (e.g. DNS) instead of crashing the process; a rejected token still fails immediately.
- Slash commands: `/check` always failed (defer then send_message), `/why` always failed (selected a non-existent column), `/research` always showed price/liquidity UNKNOWN and never used the AUTO_POST colour. The command client can no longer take the engine down.
- `AlertService.publish` returned an id even when delivery failed; it now returns None until delivered.
- Callout footer again carries "Research signal, not a trade recommendation. Not rug-proof."
- Market structure: "INSUFFICIENT_HISTORY" is reported when the selected pool has fewer than two stored snapshots (was "POTENTIALLY_CONSTRUCTIVE" from provider windows alone).
- Dashboard: token panel showed "$—" for price/liquidity/market cap; small prices rounded to "$0"; DB handle leaked on errors.
- Database retention: discovery events and old research snapshots are pruned hourly (they grew ~150 MB/day with no cleanup).
- Startup/health report `CONFIGURATION PROBLEM` for settings that make posting impossible.

Tests
- Stale fixtures updated to model PumpSwap swaps; one wrong expectation corrected (drawdown 4.85%, not ≥10%).
- New: `test_pumpswap_trades.py`, `test_publisher_start.py`, `test_retention.py`, config-problem tests. 155 tests pass (4 live tests are opt-in); mutation checks confirm each verification guard is covered.

## 1.0.0 — 2026-10-15

Initial standalone build. No prior Northstar repository was available.

- Discovery: DexScreener profiles, community takeovers and boosts; PumpPortal launches and migrations; deduplication by mint.
- Security gates: on-chain mint verification, mint/freeze authorities, Token-2022 extensions (fees, hooks, permanent delegate, default-frozen, non-transferable, pausable), liquidity floors and ratios, liquidity-removal detection, LP lock/burn/bonding/CLMM status, RugCheck cross-checks.
- Research: top-20 holders (raw vs adjusted), on-chain swap sampling, launch-bundle detection, wallet age and funder tracing, linked-wallet clusters with evidence strength, creator history, market structure, narrative intelligence.
- Transparent 100-point score with a separate risk level and confidence; strict decision precedence.
- Discord publishing (outbox pattern, pre-flight re-check, restart recovery), embed edits, milestone and warning alerts.
- Outcome tracking for all decisions, plus a survivorship-aware performance report.
- 69 mock tests and 4 opt-in live tests.

Defects found and fixed during build (each reproduced by a test first):
- Creator check returned no UNKNOWN flag when no creator could be identified, so a missing creator looked clean (`test_missing_creator_history`).
- A blank value copied from `.env.example` (for example `NORTHSTAR_DISCORD_CHANNEL_ID=`) crashed settings loading (`test_env_example_loads_with_blank_values`).

## Unreleased — tracker-style Discord callouts
- Reworked callout embeds to lead with a compact four-cell summary: call market cap, current market cap, performance, and multiplier.
- Added clear Northstar Auto-Callout / Solana attribution and a LIVE label.
- Updated the summary metrics whenever monitoring refreshes the original embed, while retaining detailed market, risk, and data-quality sections below.
- Initial callouts intentionally show +0.0% and 1.0x as the baseline; no gains are fabricated.


## Strict-quality profile update
- Tightened default publication policy to score >= 92, HIGH confidence, LOW_DETECTED risk only, three independent narrative evidence items, and max two alerts/day.
- Raised liquidity/holder/creator/cluster thresholds and require verified LP lock/burn status.
- This is a conservative filter profile, not a claim of 70–80% validated win rate. Validate with recorded outcomes before making performance claims.


## Unreleased — pre-migration incomplete-data path
- Added persisted PumpPortal bonding-curve observations, separate from DEX pool snapshots.
- Routed known PumpPortal launches with no usable DEX pool into a pre-migration research path instead of immediately stopping at `no usable pool`.
- Added a separate early-watch signal based on observed recent trade activity and market-cap-in-SOL trend; it is never represented as a standard low-risk callout.
- Added a validated Pump.fun bonding-curve account decoder and a bounded free-RPC recent transaction sample fallback, so early research does not require metered trade subscriptions when the configured RPC can supply the data.
- Added explicit, disabled-by-default opt-in for metered PumpPortal token-trade subscriptions, with a hard subscription cap.
- Added persistent early-signal entry/current market cap (SOL), peak multiple and minimum multiple tracking.
- Reduced the initial research delay from 10 minutes to 20 seconds; candidates with insufficient evidence remain blocked and are rechecked.

## Discovery reliability hotfix — 2026-10-15

- Added a PumpPortal quiet-stream watchdog: if a connected socket delivers no valid mint events for 180 seconds, the client reconnects and resubscribes instead of staying silently connected forever.
- Added periodic quiet-stream diagnostics, reconnect counts, event counters, last-event age, and heartbeat reporting so an idle feed can be distinguished from a stopped discovery loop.
- Discovery now logs every scan, including scans that return zero new candidates. This makes provider/feed stalls visible in Wispbyte logs.
- Reconnect backoff resets after actual valid events rather than merely after a socket connection, avoiding rapid reconnect loops when the upstream is stale.

## v1.3.0 — Discord slash commands
- Added initial Discord slash-command suite for research, quick check, scan queue, calls, performance, decision explanations, personal watchlist, comparison, deep research, and admin status/diagnostics/settings.
- Commands use existing SQLite research/callout history and configured research providers.
- Admin commands enforce Discord Administrator permissions. Settings are view-only; no runtime threshold changes are applied.
- Added Discord setup documentation and Wispbyte enablement steps.
