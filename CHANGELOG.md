# Changelog

Every version contains everything from the versions before it. Each release folder is built from
the complete code (`python scripts/make_release.py`), never from a partial patch.

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
