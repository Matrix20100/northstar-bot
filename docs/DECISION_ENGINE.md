# The decision engine (v2.4.0)

One score, one risk class, one decision, with the evidence behind each. Code:
`northstar/analysis/unified.py`. Replay script: `scripts/validate_decision_engine.py`.

## What it replaced

v2.3.0 answered the same question four times:

| System | Where | Problem |
|---|---|---|
| Opportunity score (100 pts) | `analysis/scoring.py` | hand-set weights, awarded points for data completeness |
| Risk level | `assess_risk()` | third taxonomy of severity, on top of flags and gates |
| Rug verdict | `analysis/verdict.py` | the only component measured against real outcomes — and it ran **after** the decision, for display only |
| Learning brain | `engine.py` | bolted on afterwards, promoted coins by string-matching blocker text, shipped with its veto switched off |
| AI narrative judge | `early_calls.py` | never reached the main callout path at all |

Holder concentration was penalised in the score, again in the risk level and a third time in the
verdict. Dev history and wash trading were counted twice. Nothing is counted twice now.

## How it works

**1. Evidence ledger.** Every observation becomes one `Check` with a canonical key, one owning
category, and a provenance grade:

| Grade | Meaning |
|---|---|
| `FACT` | measured directly — an on-chain read, a decoded account, a real quote |
| `INFERENCE` | derived from several observations, or a provider's own opinion |
| `UNKNOWN` | we looked and could not tell |
| `UNAVAILABLE` | the provider could not be reached, or the check never ran |

A key appears once. If a signal becomes a veto it is removed from scoring, so a hazard is never
both a veto and a points deduction. The gate and the flag that describe the same hazard share one
canonical key (`HAZARD_ALIASES`), so "top 10 hold a lot" + "one wallet holds 9%" +
`holder_concentration` FAIL is **one** warning, not three.

**2. Category scores.** Each category is scored only over checks that actually have a grade:

```
quality  = earned / available_weight          0..1, how good what we know is
coverage = available_weight / total_weight    how much of the category we know
points   = category_weight * quality * (0.5 + 0.5 * coverage)
```

A check that did not run earns nothing, and thin evidence costs score rather than silently
passing. The absence of a detected risk is never worth points on its own.

| Category | Default weight | What it holds |
|---|---|---|
| Safety checks that ran clean | 18 | authorities, extensions, LP burn/lock, liquidity-removal, RugCheck |
| Liquidity and exit | 20 | depth, liquidity/market-cap, **real Jupiter sell quotes**, churn plausibility |
| Holders and wallets | 16 | top-10, largest wallet, creator holding, fresh wallets, connected groups |
| Developer history | 10 | prior rugs, earlier launches, behaviour |
| Demand and price structure | 20 | unique buyers, buyer trend, volume trend, buy/sell pressure, shape, drawdown |
| Narrative and attention | 16 | independent evidence items, trending metas, socials, ticker crowding, AI read |

Weights live in `config.py` (`u_w_*`) and are overridable by environment variable. **They are
starting assumptions, not fitted coefficients** — see Validation below for why nothing here is
fitted.

**3. Vetoes sit outside the score.** Safety is not compensatory. Narrative and volume cannot
mathematically outweigh a confirmed hazard, because hazards never enter the sum at all:

| Severity | Meaning | Effect |
|---|---|---|
| `CONFIRMED_CRITICAL` | a proven trap: mint/freeze authority live, permanent delegate, transfer hook, paused, RugCheck rugged, **cannot sell at any size**, liquidity under half the floor | **REJECT**, risk EXTREME |
| `STRONG_WARNING` | serious and well evidenced: LP not locked, dev selling, prior rugs, wash trading, a normal sell size that will not fill, a flag the learner measured at ≥80% crash rate | two or more cap the decision at CAUTION; one caps it at WATCH; `blocking` ones reject |
| `UNVERIFIED_SUSPICION` | suspected, not proven; also provider disagreements | −3 points each, max −9, listed on the call |
| `UNRESOLVED` | a **trap check** could not be completed (mint, freeze, extensions, LP, liquidity) | **INSUFFICIENT DATA** |

A provider timeout is never evidence of safety. An unresolved trap check returns INSUFFICIENT
DATA; an unresolved non-trap check (the exit quote, creator history, the bundle check) restricts
instead of rejecting, so one missing non-critical field does not throw a coin away.

Where the severity list and the learner disagree, **the learner wins**: a red flag that the
tracking data measured at ≥80% crash over at least 10 coins is treated as a serious warning with
its measured rate shown, whatever the hand-written list says.

**4. Thresholds from v2.3.0 are kept.** `policy_checks()` carries over the same bars
`decision.decide()` applied — extended price, minimum narrative evidence, volume acceleration,
buy/sell ratio, the liquidity floor — because several of them were tuned by the learning loop.
They disqualify a call without pretending to be proven traps. Demand that was never verified
on-chain is also a warning: it is not good enough to call on.

**5. A call has to be callable.** Four extra conditions, each of which produced a real bug when
it was missing:

* **Essential categories** (safety, liquidity, holders, structure) each need ≥40% coverage and
  ≥45% quality. A blind spot cannot be compensated away by the other five categories.
* **Entry conditions** must be right now — extended, fading, vertical-on-thin-liquidity or a sell
  depth under $500 makes it a WATCH, not a call. A call means "worth entering now".
* **The narrative cap**: a STRONG call must still clear `u_strong_min - u_narrative_cap` with the
  narrative points removed. A good story on a bad chart cannot be called.
* **Completeness** must be at least `u_min_completeness` (0.55 by default).

**6. The learning brain is a bounded tilt.** `apply_model()` moves the score by at most
`u_model_tilt` (12) points, scaled by how far the learned odds sit from the base rate, and only
when the model is trusted (walk-forward AUC above `brain_min_auc`). It can lower a score and it
can lift one, but it cannot clear a veto, invent evidence or move the risk class.

**7. The AI is capped evidence.** `apply_ai()` writes one narrative check worth 6 of the 16
narrative points. The judge is only asked about coins already heading for STRONG or WATCH, so the
budget is spent where the answer can change something. It can never clear a veto, and the
narrative cap means it cannot create a call on its own. Follower growth, tweet velocity and
sentiment stay **unavailable** — there is no paid X access, and inventing them would be worse
than missing them.

**8. Contradictions are shown, not averaged.** Deep liquidity with no sell route, a provider
saying the LP is locked while the pool says otherwise, liquidity larger than market cap: each is
recorded, costs confidence, and is printed. Averaging two sources that disagree invents a number
neither of them reported.

## What comes out

```
score               0-100, compensatory, NOT a probability of 2x
score_ex_narrative  the same score with narrative help removed
risk                LOW / MODERATE / HIGH / EXTREME — from hazards, never from the score
decision            STRONG OPPORTUNITY / WATCH / CAUTION / REJECT / INSUFFICIENT DATA
confidence          LOW / MEDIUM / HIGH — evidence grade mix, unresolved checks, contradictions
completeness        share of expected evidence actually obtained
categories          points, weight, quality, coverage, evidence, weak points, missing per category
hard_vetoes / warnings / unresolved / suspicions / conflicts / gaps
positives / negatives   ranked by weight x quality, real signals before routine confirmations
entry               whether conditions justify an entry now, and why not
invalidation        what would kill the thesis (liquidity level, LP, dev wallet, exit cost, price)
exit_triggers       what should force a re-check
model / ai          the brain's odds and tilt; the AI read, if one was used
checks              the full ledger, for the dashboard
```

The score is **not** the probability of a 2x. When the brain has a calibrated probability it is
reported separately as `model.probability`. Score, risk, confidence and completeness stay four
separate numbers on purpose.

Legacy mapping, so the database, publisher and dashboard keep working unchanged:
`STRONG OPPORTUNITY → AUTO_POST`, `WATCH`/`CAUTION → WATCHLIST`, `REJECT → REJECT`,
`INSUFFICIENT DATA → INSUFFICIENT_DATA`. Publication policy (cooldown, daily cap, market-cap
floor) can only hold a call back; it can never create one.

## Where to see it

* **Discord**: a 🎯 Decision field at the top of the callout, then the rug check detail, then
  entry / invalidation / exit. The bubblemap, trade plan, market-cap tracking and peak tracking
  are unchanged.
* **Dashboard**: a Decision card in the coin drawer with a per-category table — points out of
  weight, a bar, how much of the category is known, and the evidence behind it.
* **Database**: under `report.unified` in `research_snapshots`. No schema change.

## Validation (read this before believing any number)

`python scripts/validate_decision_engine.py data/northstar.db` replays the engine over the bot's
own tracked history, one row per mint, report-as-stored. On the 19 Oct 2026 database:

* 509 unique coins followed with a usable price path; **63 of them have a complete research
  report** — the rest were rejected early and never got holder, trade or cluster data.
* 16.3% reached 2x at some point; 55.0% fell to 0.3x at some point.
* Ranking power (AUC, 0.50 = coin flip): unified score **0.638** across all 509; **0.555** on the
  63 complete reports; v2.3.0's opportunity score **0.498** on those same 63.
* Baselines on the same 509 coins: liquidity alone **0.596**, the rug verdict's inverted risk
  **0.547**. So the engine's ordering edge over "just pick the deepest pool" is real but
  **modest**, and it is the honest headline of this exercise.
* Top 50 by score reached 2x 28.0% of the time against a 16.3% base rate (1.72x).
* Later-period check: AUC 0.563 on the first 254 coins, 0.707 on the last 255.
* By risk class: EXTREME 9.5% reached 2x, HIGH 27.0% (but 83.9% also crashed), MODERATE 20.5%
  with 48.7% crashing. High-risk coins both run and die; the moderate bucket is the best
  risk-adjusted one.

**Limitations, stated plainly:**

1. `decision_tracking` stores the highest and lowest price without their order, so "reached 2x
   **before** a major drawdown" cannot be measured from it. Both columns can be true of one coin.
   That label needs the minute paths in `shadow_calls`, which only exist for coins called after
   that feature shipped.
2. 63 complete reports is far too few to fit or calibrate weights on. Nothing here is fitted;
   every weight is a stated assumption, and that is why the engine is explainable instead of
   optimised.
3. A market-cap multiple is not a realised return. Fees, slippage and the size the pool can
   absorb are not in those numbers.
4. These are the coins the bot chose to follow, so the sample is not the market.
5. Two of the six categories barely exist in the historical data — the exit check (Jupiter, new
   in v2.3.0) and verified trade samples — so historical decision labels skew to REJECT and say
   more about missing evidence than about the thresholds.
6. No win rate is claimed, here or anywhere else in Northstar.

## Configuration

| Setting | Default | What it does |
|---|---|---|
| `NORTHSTAR_UNIFIED_ENABLED` | `true` | off = v2.3.0 behaviour, including the old brain veto/promote path |
| `NORTHSTAR_U_W_SAFETY` … `NORTHSTAR_U_W_NARRATIVE` | 18/20/16/10/20/16 | category weights, must sum to 100 |
| `NORTHSTAR_U_STRONG_MIN` | 68 | score needed for STRONG OPPORTUNITY |
| `NORTHSTAR_U_WATCH_MIN` | 48 | below this it is not worth watching |
| `NORTHSTAR_U_REJECT_DOUBLE_WARNING` | true | two or more serious warnings reject the coin instead of watching it as CAUTION (v2.5.8) |
| `NORTHSTAR_U_MIN_COMPLETENESS` | 0.55 | evidence needed before a call is possible |
| `NORTHSTAR_U_MODEL_TILT` | 12 | most the learning brain may move the score |
| `NORTHSTAR_U_NARRATIVE_CAP` | 10 (v2.4: 20) | at most this many of the points a STRONG call needs may come from narrative |
| `NORTHSTAR_U_THIN_CATEGORY_FLOOR` | 0.5 | coverage below this marks a category thin |
| `NORTHSTAR_U_SCORE_INCLUDES_SAFETY` | `false` | v2.5: safety is decided by vetoes only; `true` = v2.4 formula |
| `NORTHSTAR_U_NARRATIVE_BLOCKS` | `false` | `true` = too few narrative items REJECTS (v2.4); `false` = caps at WATCH |
| `NORTHSTAR_U_EXIT_IMPACT_WARN_PCT` | 5 | exit impact at the largest filled test size that is a serious warning |
| `NORTHSTAR_U_EXIT_IMPACT_BLOCK_PCT` | 15 | exit impact that disqualifies a call |
| `NORTHSTAR_COLLAPSE_H1/H6/H24_DROP_PCT` | 50 / 60 / 75 | a drop this large in that window marks a price collapse |

## v2.5 — the scoring and decision audit (2026-10-19)

Live symptom: Northstar found coins, but almost everything ended REJECT or INSUFFICIENT DATA, and
the same report could show two scores (e.g. 41.2 and 58/100). Every finding below was traced in
the code; the regression for each is in `tests/test_decision_v25.py`.

### What was wrong

| # | Finding | Kind |
|---|---|---|
| 1 | `/research`, `research_snapshots.score`, `candidates.last_score`, the dashboard list and `alerts.score` stored the **v2.3 opportunity score** (`report.score.total`) while the decision and its reasons used the **unified score**. Same coin, two numbers. The rug check added a third "risk N/100". | display / source of truth |
| 2 | `ResearchOutcome.risk` was the old `assess_risk()` level (e.g. CRITICAL) while the panel showed the unified class (e.g. HIGH). | display |
| 3 | The engine re-ran the unified engine with live context, the brain and the AI **after** the snapshot was saved; `_refresh_unified` changed the report's decision without changing `out.action`. The bot could act on one decision while the record showed another. | bug |
| 4 | Publication policy (cooldown, daily cap, callout market-cap minimum) was read from the legacy `decide()` blockers, which are never computed when a gate fails or is critical-UNKNOWN — so on those coins a unified STRONG bypassed the policy at research time. | bug |
| 5 | `MIN_NARRATIVE_EVIDENCE_ITEMS=3` was a **blocking** veto. Reaching 3 needs a trending-meta match AND a public Telegram of 500+; most new coins have neither, so they were REJECTED for missing optional data. | missing data treated as danger |
| 6 | "Extended", "volume fading", "weak buying" and "trade sample not verified" were blocking or serious warnings. Two of them together counted as "2+ serious warnings" -> CAUTION and **HIGH risk**: missing data and timing raised the risk class. | missing data / timing treated as risk |
| 7 | A concentrated-liquidity pool (Meteora DLMM, CLMM, Whirlpool) made the LP status "unknown" -> `lp_lock` UNKNOWN -> INSUFFICIENT DATA forever, although withdrawability is a known fact — and the token's burned PumpSwap pool was never weighed. ($DOPE) | UNKNOWN vs FAIL confusion |
| 8 | A **measured** partial LP burn also stayed UNKNOWN. | UNKNOWN vs FAIL confusion |
| 9 | DexScreener and RugCheck disagreeing on price FAILED a critical gate and rejected the coin before any analysis — a provider conflict treated as a proven hazard. | conflicting providers |
| 10 | Scoring checks and veto flags for the same fact were not linked (`hold:creator` vs `flag:dev_holds`, `wal:fresh` vs `flag:fresh_*`, `liq:churn` vs the two wash-trading flags, `exit:impact` vs slippage flags...). Wash trading seen by two detectors was two serious warnings; the verdict's level counted them twice again. | double counting |
| 11 | Clean safety checks earned up to 18 score points. Every migrated pump.fun coin earned the same points (no ranking power) and a RugCheck outage cost a coin points: an outage moved the *opportunity* score. Unknown critical non-trap gates also cost -3 points. | outage leaking into opportunity |
| 12 | `U_NARRATIVE_CAP` was 20 while narrative was worth at most 16: the documented rule "a good story cannot carry a bad chart" could never bind. | rule that never fires |
| 13 | A coin down 90%+ was not detected: "fading" needed a +100% 6h pump first. ($WORTHLESS) | missing check |
| 14 | Exit **cost** was scored but never restricted a call: a sell that routes at 9% impact on $1,000 could still be STRONG. ($WORTHLESS) | missing gate |
| 15 | When the deepest pool was a DLMM pool, trades were sampled from it, but only PumpSwap swap events can be decoded — so the demand sample was unverifiable; and with RugCheck down a second pool's vault was counted as a whale holder. | data quality |
| 16 | No funnel telemetry: there was no count of how many coins stopped at each stage, or why. | telemetry |

### What changed

* **One source of truth** — `northstar/final.py`. `final_view()` derives action, score, risk and
  confidence from the unified result; `apply_final()` writes them onto the outcome, the report,
  the snapshot row (`Database.update_research_final`) and the candidate row, after the brain and
  AI have had their say. The v2.3 score stays in the report as `final.legacy_score_v23` (the
  brain uses it as a feature) and is never shown as *the* score. The brain and tuner keep reading
  the old risk level from `report.legacy_risk`, so their features do not silently change meaning.
* **Six separate answers** in `report.unified` and `report.final`: `safety` (PASS/WARN/FAIL/
  UNRESOLVED from vetoes alone), `score` (opportunity), `early_entry` (EARLY / ON TIME / LATE /
  COLLAPSED / UNKNOWN, judged from the move already made and never added to the score),
  `confidence`, `completeness`, `decision` with a `reason_class`.
* **Two non-hazard severities.** `EVIDENCE_GAP` (narrative shortfall, unverified trade sample,
  unknown critical non-trap gate, unknown volume trend) and `TIMING` (extended, fading volume,
  weak buying) cap the decision at WATCH and lower confidence; they never raise the risk class,
  never count as serious warnings and never cost score points.
* **LP across all pools** — `security.resolve_lp_floor`. The `lp_lock` gate PASSES when the
  burned/locked liquidity alone (PumpSwap burns read on-chain, at most 3 reads) clears
  `MIN_LIQUIDITY_USD`; FAILS when it cannot even in the best case (e.g. DLMM-only liquidity) or
  the selected pool's own LP is measured as partly unburned; stays UNKNOWN otherwise. The
  withdrawable share is shown and costs a -3 suspicion; a secondary pool with partly unburned LP
  is a HIGH flag (never a call).
* **Unresolved causes.** An UNRESOLVED critical check records whether its provider FAILED this
  run (`PROVIDER_FAILURE`, retried) or the check could not tell (`CRITICAL_UNKNOWN`).
* **Single counting.** `HAZARD_ALIASES` now links gate, verdict flag AND scoring check for each
  fact; a vetoed fact's scoring check is *absorbed* (known for coverage/completeness, worth no
  points). The verdict level used for the risk class is recomputed with one entry per hazard.
* **New measured checks.** Price collapse (`market.py`, blocking serious warning, structure
  shape 0) and exit cost (serious at 5%, blocking at 15% impact on the largest filled size).
* **Safety out of the score.** Score weights are rescaled over the five opportunity categories
  (liquidity 24.4, holders 19.5, developer 12.2, structure 24.4, narrative 19.5). Safety still
  counts for coverage, completeness, confidence and the essential-category check.
* **Telemetry** — `northstar/telemetry.py`, table `funnel_events`, `python main.py funnel`, the
  dashboard's *Decision funnel* card and `/api/funnel`.

### What did NOT change

Every hard gate is intact: mint/freeze authority, Token-2022 traps, RugCheck rugged/danger,
liquidity removal, no sell route, liquidity under half the floor, threshold gates (holder
concentration, creator holdings, creator dumping, bundles, liquidity floor/ratio). An unresolved
trap check is still INSUFFICIENT DATA. The learning brain is still a bounded tilt that cannot
clear a veto. No threshold was lowered to produce more calls.

### Known limits of v2.5

* Thresholds `U_STRONG_MIN=68` / `U_WATCH_MIN=48` were kept, but the score scale changed (safety
  removed, narrative cap binding). Run `python scripts/validate_decision_engine.py
  data/northstar.db` on the live database before trusting the new ordering; it has not been
  re-validated against real outcomes.
* The self-learning tuner still replays the legacy `decide()` rules (it tunes
  `MIN_OPPORTUNITY_SCORE`, which the unified engine does not use). Its other tunables (momentum
  bars, callout market-cap minimum) still act.
* Tokens held inside withdrawable DLMM positions are not counted as holder supply.
* RugCheck "danger" risks remain a hard gate, except "LP unlocked" when Northstar has read the
  burn on-chain (then it is a recorded provider conflict). If the funnel shows `rugcheck_danger`
  dominating rejections of otherwise clean coins, the gate needs its own review.
* The liquidity/market-cap ratio uses the selected pool only, not the token's total liquidity.

## v2.5.1 — measuring false rejections and rugs called

`python main.py audit` reads the database and prints, per call type and per rejection reason,
how many coins ran (2x before 0.6x) and how many rugged (0.3x before 1.3x). The tables used:
`shadow_calls` (ordered minute paths for every post-migration decision), `decision_tracking`
(high/low only), `alerts` (posted calls) and `early_calls`. It also replays stored reports
through the current engine, so a change can be judged on coins it was not tuned on.

Paths that post without going through the unified engine, and what 2.5.1 changed in them:

| Path | Before | Now |
|---|---|---|
| Early narrative call (curve) | blocked only on a FAIL or verdict RUG LIKELY / HIGH RISK; unchecked holders passed | required checks must PASS; any high/critical red flag holds the call |
| Rug scalp | read gates only | also refuses no sell route, ruinous exit cost, collapse, trap flags |
| Curve research | missing SOL amounts counted as 0 SOL; missing trend counted as a miss | unmeasured inputs -> INSUFFICIENT DATA (re-check) |

### Live configuration notes (from the operator's env, 2026-10-21)

These are settings, not code, and were left as the operator set them. They are listed because
they bear directly on rugs called:

* `MAX_SINGLE_HOLDER_ADJUSTED_PCT=15`, `MAX_CLUSTER_SUPPLY_PCT=35`, `MAX_CREATOR_HOLDING_PCT=15`,
  `MAX_CREATOR_SELL_PCT_OF_SUPPLY=10` are far above the levels the rug check measured as deadly
  (one wallet 8%+ crashed 85-100% of the time). The unified engine still treats those red flags
  as serious warnings, and 2.5.1 makes early calls respect them, but the gates themselves only
  fire far later. Re-check with `python main.py audit` before loosening further.
* `U_NARRATIVE_CAP=20` in the env overrides the 2.5 default of 10, so the narrative cap does not
  bind on that deployment. Remove the line (or set 10) to use the fix.
* `MAX_PRICE_AGE_SECONDS=100000` effectively disables the price-freshness gate.
* `EARLY_CALL_MIN_AI_SCORE=55` (default 70) lets weaker AI reads become early calls.
* `ALERT_COOLDOWN_HOURS=0` and `MAX_DAILY_ALERTS=12000` switch publication limits off.

## Rollback

v2.5 adds one table (`funnel_events`, created with IF NOT EXISTS) and no columns. Score-only
rollback: `NORTHSTAR_U_SCORE_INCLUDES_SAFETY=true`, `NORTHSTAR_U_NARRATIVE_BLOCKS=true`,
`NORTHSTAR_U_NARRATIVE_CAP=20` restore the v2.4 scoring. Older code ignores the new table and
the new report keys.

The v2.4 engine added no tables and no columns. To go back: set `NORTHSTAR_UNIFIED_ENABLED=false` and
restart — research falls back to `decision.decide()` and the brain to its old veto/promote path.
Reports written while it was on keep their extra `report.unified` key, which older code ignores.
To roll back the code instead, run the previous version's folder against the same `data/`
directory; nothing in the database needs changing.


## Upside vs rug: what the stored price paths show (v2.5.5)

Source: 318 coins with a complete, ordered 4-hour price path (learning shadows) in the 21 Oct
backup, liquidity $10K+, no proven trap. One path per coin. Outcomes:
- **clean runner:** 2x before 0.6x, and never 0.3x afterwards within the path;
- **pump-then-rug:** 2x first, then 0.3x;
- **rug:** 0.3x before 1.3x;
- **flat:** everything else.

| Group | n | clean runner | any 2x | rug | rug + pump-then-rug |
|---|---|---|---|---|---|
| all of them | 318 | 10.4% | 28.0% | 23.6% | 41.2% |
| pool at least 5 min old | 218 | 11.5% | 24.3% | 20.6% | 33.5% |
| liquidity $50K+ and pool at least 5 min | 46 | 8.7% | 19.6% | 10.9% | 21.7% |
| liquidity $100K+ | 22 | 9.1% | 9.1% | 4.5% | 4.5% |
| liquidity $100K+ and pool at least 1 h | 17 | 0% | 0% | 0% | 0% |

**Pump-then-rug is common.** 56 of the 89 coins that reached 2x first fell to 0.3x within 4 hours.

**What separated outcomes, inside the same set:**
- **Pool age.** Pools under 5 minutes old were the worst group: 58.5% rug or pump-then-rug.
- **Liquidity.** The top third ($33K+) rugged less, 17% against 27%.
- **Volume against market cap.** Very high 1-hour volume relative to market cap rugged more,
  33% against 13%.

**What did not separate them:**
- top-10 holders and the largest single holder;
- the unified score and the narrative category;
- the learning brain, which scores 0.49 on unseen coins.

**Missing data.** Cluster, insider and creator shares were 0 for every coin in this set. Those
detectors gave no usable signal here, so their absence is not evidence of safety.

**Bias.** These are 4 days of v2.4-era decisions. Shadows exist only for coins that reached
research, and the samples above 40 coins are small. Treat these as reasons to shadow-test, not
as a strategy. The shadow policies compare:
- `pool_age_5m`;
- `liquidity_50k,pool_age_5m`;
- `deep_liquidity` ($100K, kept as the safe extreme);
- the audit's relaxations.

`python main.py shadow` reports, per policy:
- calls per day;
- runners split into clean, pump-then-rug and order unknown;
- rugs;
- outcomes of every analysed coin the profile admits against the ones it excludes, a much larger
  sample than the calls alone.
