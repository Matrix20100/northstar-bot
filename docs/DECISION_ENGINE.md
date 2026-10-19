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
| `NORTHSTAR_U_MIN_COMPLETENESS` | 0.55 | evidence needed before a call is possible |
| `NORTHSTAR_U_MODEL_TILT` | 12 | most the learning brain may move the score |
| `NORTHSTAR_U_NARRATIVE_CAP` | 20 | narrative help a STRONG call must survive without |
| `NORTHSTAR_U_THIN_CATEGORY_FLOOR` | 0.5 | coverage below this marks a category thin |

## Rollback

The engine adds no tables and no columns. To go back: set `NORTHSTAR_UNIFIED_ENABLED=false` and
restart — research falls back to `decision.decide()` and the brain to its old veto/promote path.
Reports written while it was on keep their extra `report.unified` key, which older code ignores.
To roll back the code instead, run the previous version's folder against the same `data/`
directory; nothing in the database needs changing.
