# Handoff to Claude Code — 28 October 2026

This file hands Northstar over from the claude.ai session that built v2.x–v5.1.0 to Claude Code. Read
`CLAUDE.md` first (how to run, deploy and deliver; hard rules). This file covers **what state things are in,
what is broken, and what the operator wants next**, with leads already found.

---

## 1. Where things stand

- `main` is at **v5.1.0** (`VERSION`), plus operator-made changes (PRs #30–#37, made with ChatGPT): a temporary
  **VIP role system** (`/viprole`, `/viptime`, expiry worker) in `northstar/discord_bot/commands.py`, and a stray
  `commands.py` at the repo root (not imported; ask the operator before deleting it).
- The bot runs 24/7 on Wispbyte. Self-test and logs show all providers OK when the Helius RPC has credits.
- **Test suite:** after the ChatGPT edits, `tests/test_commands.py` had 15 errors (a removed `hasattr(bot, "event")`
  guard). This handoff PR restores the guard: **860 passed, 4 skipped** with pytest's logging plugin on.
- Paper trading uses the **exit engine** (`exits.py`) with **conviction grades** (`conviction.py`, v5.1.0).
- Real-money trading is wired (`live.py`) and **ON in test mode**, but every simulated buy fails (see bug 3).

### Versions this period (details in CHANGELOG.md)

| Version | What |
|---|---|
| v4.2.1–v4.3.0 | Learning tab fixes; a coin back at $2–5K is dead and learned as a rug |
| v4.4.0 | Uxento Vision forwarder (Chrome extension in `tools/vision-forwarder`, installed on the operator's desktop) |
| v4.5.0–v4.5.1 | Confirmed rugs blocked for good; Watching shows only live, safe coins; scalps to their own channel; brain buys vetoed by the risk system; watchlist expiry |
| v5.0.0 | Exit engine (stop, trailing stop, sellers took over, fading volume, no progress, time, TP1/TP2); live trading with a bot-only wallet (PumpPortal local tx, signed locally, test mode by default, kill switch, daily loss limit) |
| v5.1.0 | Conviction grade A/B/C at the buy sets each position's stop (15–35%), size and a 90 s dip-hold; live skips grades under 40; Paper tab shows results by grade; log-format guard test |

---

## 2. Open bugs (fix these first)

### Bug 1 — Milestone posts show multiples the coin never reached ("the bot is lying")
- **$Pilly** (runner call at $26.9K): Calls tab showed peak **$1.06M (39.37x)** and "now $101K"; the operator says it only reached ~$40K.
- **$Pumpers** `8H9568bKm2WXAAFUVXJkLt4CRPWY1gXvCPzDTVURWZ2Z`, called 11:05 UTC at $16.6K: VIP post "hit 25x… peak 27.10x ($450.7K)".
  Axiom showed ATH **$20.5K**, bonding curve 68% (still on the curve), 10 minutes old.
- Only seen in the **VIP** channel (milestones post to VIP live; the free channel only gets delayed proven calls).
- Note: the Pumpers mint does **not** end in `pump`.
- **Leads** (`early_calls.py` `EarlyCallTracker._caps`):
  1. Curve phase: market cap = `total_supply * virtual_sol / virtual_tokens / 1e9 * SOL/USD` from the curve account
     (`research.Researcher._parse_pump_curve_account`). Check this against the real curve of these two coins: a
     different curve layout (newer pump.fun create flow, token-2022, non-`pump` vanity mint, or another launchpad
     whose account happens to parse) would give a wrong ratio.
  2. If the chain read fails, the fallback uses DexScreener: the **highest-liquidity non-pumpfun pair** whose base is
     the mint, `market_cap or fdv`, and marks the coin **migrated**. A spoofed/fake pool or a wrong `fdv` would produce
     exactly this. Check `pairs` for these mints and add a sanity rail: a peak must be confirmed by two reads, or by
     the curve and DexScreener agreeing, before a milestone is posted.
  3. `paper.MAX_TICK_JUMP = 20` only guards paper fills, not the tracker's `peak_mcap`. The tracker has no jump guard.
- Fix must cover the posted milestone, the Calls tab peak and the paper/exit engine reads.

### Bug 2 — "Axiom link doesn't take me to the same coin"
- Every Axiom link is `https://axiom.trade/t/{mint}` (`discord_bot/embeds.py`, ~8 places). Check what Axiom's
  `/t/` route resolves to; Axiom's own coin URLs use the **pair/curve address** (`axiom.trade/meme/<address>`).
  Possibly the same root cause as bug 1 if the wrong token/pool is being tracked.

### Bug 3 — Test-mode trading: every buy fails with `simulation failed: AccountNotFound`
- Trading tab: wallet `7QUF…jhyj`, **balance 0.0000 SOL**. `simulateTransaction` with `sigVerify: true` fails with
  `AccountNotFound` when the fee payer has no account on-chain. **Cause: the wallet is unfunded.**
- Options: (a) operator funds it with a small amount (~0.15 SOL) so simulations run; (b) in test mode, when the payer
  account is missing, record a clearly-labelled paper-style test trade instead of FAILED and say "fund the wallet to
  simulate on-chain". Do not drop `sigVerify` silently.
- Also seen: `trade_max_chase_pct` (25%) skipped the day's best calls ($ber "up 26%", $CARPNIK "up 39%",
  $VANESSA "up 45%" by the time the buy ran). The buy loop runs on calls from the last 2 minutes; worth checking the delay.

### Bug 4 — The brain's paper account almost never trades
- Discord `/paper`: brain account 11 closed (7 in profit, +21.5%), 0 open, nothing new.
- **Lead (confirmed in code):** `engine._apply_brain` returns no odds for `PRE_MIGRATION_BONDING_CURVE` coins, and
  `BrainTrader.consider` needs both brain odds and a DEX pair. Most research now is curve coins, and migrated coins are
  mostly stopped by the prefilter first. On top, the v4.5.0 risk veto (ELEVATED/CRITICAL risk, failed launch gates,
  rug flags) removes most of the rest. Count the skip reasons (`consider()` returns them) before changing anything.

### Bug 5 — `/research` (Discord) on a prefilter reject shows "Score: UNAVAILABLE (legacy v2.3 path (no unified result))"
- Also: a mint pasted with a trailing `\` gave "invalid mint address format". Strip stray punctuation from input.

---

## 3. The operator's backlog (his words, grouped)

### A. Exits that read the chart (top priority for him)
> "a trailing stop should be disabled for a retest and second pump, if a coin doesn't reach a higher high 3 times after
> each swing high/low and if big wallets are still inside the coin… and if a narrative is big. The bot sold on BIBI
> coin which is doing higher highs, it was called at 20k now it's at 200k, but the trailing stop activated at a dip, the
> chart should be analyzed and a choice should be made based on callouts, hype, narrative, wallets, everything THEN an
> educated choice should be made."
>
> "every time we are up like 300 usd on the paper trading then we lose our wins" · "we need to make smarter exits"

- $BIBI (`AcsdEAts1wWTW3wXCs96L33PPovrDiymT1hf8sY5pump`): runner call ~$20K; posted plan's trailing stop fired at
  **3.51x from a 6.07x high**; coin later ~$200K. Paper (exit engine) sold TP1 1.9x, then trail at 0.88x.
- Two exit systems exist: the **posted plan** (`trade_plan.py`, early plan trail 40%) and the **exit engine**
  (`exits.py`, paper/live). Both need the same smarter logic.
- Ideas the operator named: swing structure (higher highs/lows, retests), tracked/big wallets still holding
  (`report["tracked_wallets"]`, `wallet_flow.py`), narrative/hype strength, callouts by other channels
  (Uxento Vision feed, `social_feed.py`).
- Measure any change by replaying recorded price paths (`exits.replay`) and the Paper tab "results by grade".

### B. Better entries (the data says this matters most)
- 24 Oct runner calls: of 16, **10 never went above ~1.0x and died** (several within a minute of the call),
  5 reached 2x+ ($ber 7.7x, $CARPNIK 5.9x). Score did not separate them ($HUMANITY 82 died; $CARPNIK 46 ran).
- Stops can't fix this: those coins gap −50% to −80% between checks. Only not calling them helps.
- Suggested: a **call review** on the Learning tab comparing "died without rising" vs "2x+" calls on stored report
  fields (bundle %, snipers, dev, holders, buyer pattern, copycat names). Two different $ZAIO mints were called 2
  minutes apart; both died.

### C. Missed winners — "why did we not call this?"
- `JDAEYyQswqJga9NmhmL7F2Ruf1CFa6iuS9Kvj8neSTNK` ("100% fine and it ran") — not a pump.fun mint.
- $BIBI shows REJECT (score 22: 16.7% bundled launch, 12.3% price impact) on the standard path even though the
  runner path called it.
- The operator shared a competitor's recap ("NEXAS ALPHA", 18–25 Oct: $DacityCoin 80x, $hi 37x, $UHI 29x, …).
- Leads: top reject reasons per day are `prefilter: liquidity < $5,000` (~400), **`rugcheck_danger: Large Amount of LP
  Unlocked` (~200)**, `pool age > 8h` (~110), `liquidity < 12% of market cap` (~55). The LP-unlocked hard veto likely
  rejects legitimate non-pump.fun launches (some pool types never burn LP). Verify on the missed coins before changing.
- Build a "why not called" view: paste a mint, see every path's decision and the exact blocker.

### D. Research volume
> "we need to do more researches, we only have 115 researched in 2 hours"
- 24h funnel: discovered ~20,600 → researched to a decision ~1,400 → not ready ~6,000.
- The limit is the Solana RPC budget. Helius ran out of credits on 24 Oct and took research down; the operator was
  advised `research_concurrency` 4 and `insufficient_data_recheck_seconds` 600. More research needs a bigger RPC
  plan or fewer RPC calls per coin (cache, skip deep checks for coins the prefilter will reject anyway).

### E. Teaching and a self-improving AI
> "SET UP THE BOT FOR MANUAL TEACHING. PROVIDE ME INFO TO LEARN IT"
> "I want to be able to manually feed the AI, and for the AI to go find coins… it just learns 24/7, the learning must be live"
> "there must be a mode where we mass find research for coins to teach the AI then delete the unneeded ones and those already learned from"
> "I want to be able to control the brain completely"

- What exists: dashboard coin page → **Teach the AI** (runner / rug / went nowhere); the lesson overrides the measured
  label and both brains learn it at the next hourly training (`teach.py`). Brains retrain hourly on labelled outcomes.
- Wanted: a teaching guide for the operator, a bulk "research and label" mode, pruning of learned/unneeded rows, and a
  brain control panel (features on/off, thresholds, retrain now, see what it learned).

### F. "Rebuild the AI completely" (operator's direction, 25 Oct)
> "one massive brain and different segments for different tasks like entries, exits, trailing stop, stop loss, all of
> these tasks must be machine learning… a neural brain… another model for collecting information as a separate project"
- Current models: logistic/boosted-style runner brain (walk-forward AUC ~0.65 trusted, 5,000 coins, ~258 runners)
  and a rug model (AUC ~0.93). Honest constraint: a neural network needs far more labelled examples than ~250 runners;
  with this little data it will overfit. Suggested order: (1) the data-collection project first (separate service,
  many more labelled price paths and exits), (2) separate learned models per task (entry, exit, stop) on that data,
  (3) a neural model once data supports it, compared walk-forward against the current models. Agree the plan with him.

### G. Discord and UI
- **VIP recap channel**: a weekly recap like the competitor's ("16 call-outs above 4.5X — highest milestones shown").
  Must use verified multiples only (see bug 1).
- **Dashboard theme**: "purple, mixed with black, it must look premium".

---

## 4. Evidence on hand (from the operator's screenshots, 24–28 Oct)

- Paper "calls" account (exit engine + conviction), 2.6 days: **+7.9%**, 125 closed, 44 in profit; best +$720, worst −$134.
  By grade: **B** 23 closed, 12 in profit, +9.1% of money put in; **C** 36 closed, 13 in profit, −2.6%. Too few to conclude.
- Many losses are hard stops at 0.08x–0.5x: coins that gapped through the stop (dumps/LP pulls).
- Paper "brain" account: 11 closed, 7 in profit, +21.5%.
- Brain: TRUSTED, walk-forward AUC 0.65–0.67 (recent 0.75). Rug model AUC 0.93.
- Performance report: INSUFFICIENT_DATA decisions median −95% at 6h; REJECT median −0.7% at 6h. Score buckets 30–39
  touched 2x most often (18.9% of 74) — the score is not ordering outcomes well.

## 5. Waiting on the operator

- Fund the bot wallet (bug 3) or accept test trades without on-chain simulation.
- Keep Helius within budget: `research_concurrency` 4, `insufficient_data_recheck_seconds` 600, or upgrade the plan;
  optionally `NORTHSTAR_RPC_RPM` to the plan's limit.
- `NORTHSTAR_DISCORD_SCALP_CHANNEL_ID` if rug scalps should post (they are held otherwise).
- A VIP-recap channel ID once that feature exists.

## 6. Branches and paused work

- `paused/v4.4.1-text-settings-fix` — fixes the dashboard Save error "twitter_feed_channel_ids … Input should be a valid
  string". The operator interrupted it twice; never reviewed. Ask before merging.
- `archive/v2.5-unreviewed-edits` — old edits that did not match the operator's choices. Reference only; do not merge.
- Old `vX.Y.Z-*` branches are merged history.

## 7. Outside the repo

- **Server (Wispbyte):** `.env` with all keys, `data/northstar.db`, `data/tracked_wallets.json`. Only the operator can reach it;
  ask him for log excerpts or a DB copy when needed (never ask for keys).
- **Operator's desktop:** the Vision forwarder extension (v1.1.0) in Chrome, reading Uxento Vision and posting to a
  Discord webhook; the bot reads that channel (needs the Message Content intent).
- **claude.ai Project "Northstar Discord Bot":** audit notes from Oct 18–22 (decision engine, history audit, call evidence).
