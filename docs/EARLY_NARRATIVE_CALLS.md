# Early narrative calls (30–50k)

Northstar calls bonding-curve coins **before** they migrate when they ride a narrative that is
live *right now*, judged by Claude against four free attention feeds.

## The feed (`northstar/events.py`)

| Source | What it answers | Refresh |
|---|---|---|
| Pump.fun launch waves | What are traders launching right now? A word appearing in many launches by **many different creators** in 30 min vs the 6 h before | 60 s |
| Google News RSS (top, world, tech, entertainment) | Which real event caused it | 10 min |
| Google Trends "trending now" RSS | What people are searching | 10 min |
| Wikipedia most-read | Viral people, events and memes | 1 h |

A wave needs several different creators: one bot spamming 30 copies of its own ticker is not a
narrative. All four sources are free and need no keys; each one is optional and a failure is logged,
never fatal.

## The judge (`northstar/ai_judge.py`)

Claude gets the coin (name, ticker, description, socials, 5-minute flow, ticker crowding) plus the
live feed, and answers in strict JSON: narrative, which live event it rides, fit / originality /
attention, timing (early·peak·late·dead), red flags, a 0–100 score and CALL / WATCH / PASS.

- Coin text is written by anonymous deployers, so it is passed as **quoted JSON data** with control
  characters stripped, and the system prompt says to treat it as data, not instructions.
- The answer is parsed strictly: an unknown verdict, a missing score or unparseable JSON is
  discarded (no call).
- Hard caps: `NORTHSTAR_AI_MAX_CALLS_PER_HOUR` and `NORTHSTAR_AI_DAILY_BUDGET_USD`. Spend is
  recorded per call in `ai_judgements`. Results are cached per coin for 30 minutes, or until the
  market cap moves 1.5x.
- No API key → no early calls at all (everything else keeps working).
- **The judge does not have to be Claude.** Set `NORTHSTAR_AI_PROVIDER` and paste that provider's
  key in `northstar.env`: `groq` (default, free tier, fast), `gemini`, `openrouter` (free `:free`
  models), `cerebras`, `mistral`, `github` (free with a GitHub account), `local` (a model running on
  your own machine, no key) or `anthropic` (paid). Model and endpoint come from the preset unless you
  set `NORTHSTAR_AI_MODEL` / `NORTHSTAR_AI_BASE_URL`. Everything downstream — the prompt, the strict
  parsing, the caps, the caching — is identical, so you can switch providers whenever one runs out.
  Run the self-test (NARRATIVE section) after switching: that is what proves the key really works.
- The attention feed also includes **CoinGecko trending coins and the hottest sectors by 24 h
  market-cap change**, so the judge can tell a world-news narrative from a crypto-native one.

## The call

A coin is called only when **all** of these hold: still on the curve, market cap inside the band,
≥10 verified trades and ≥8 unique buyers in 5 min with buys ahead of sells, ≥1 SOL bought, no failed
security check, rug check not RUG LIKELY/HIGH RISK, no recycled socials — and Claude returns CALL
with a score ≥ `NORTHSTAR_EARLY_CALL_MIN_AI_SCORE`.

Posted to **VIP** as "⚡ EARLY NARRATIVE CALL" with the AI's reasoning, the live attention lines, the
flow, an early plan (2x/4x, −40% stop, 40% trail, 30-min time stop) and the rug check. The tracker
follows the coin on the curve and **through migration into its pool**, records the peak, posts plan
pings, and posts a proof copy to the **free channel** once it passes `EARLY_CALL_FREE_AT` (1.5x).

## Cost

About 1,300 input + 120 output tokens per judged coin. At Haiku pricing (currently $0.10 / $0.50 per
million) that is roughly **$0.0002 per coin**, so a few hundred judgements a day stay well under $1.
The daily budget is a hard stop, not a guideline.
