"""One decision engine: one score, one risk class, one decision, with its evidence.

Before this module Northstar had four systems answering overlapping questions: the 100-point
opportunity score (scoring.py), the risk level (assess_risk), the rug verdict (verdict.py) and
the learning brain (brain.py) — plus the AI narrative judge on the early-call path only. They
penalised the same things more than once (holder concentration was counted in the score, again
in the risk level and a third time in the verdict), the most calibrated of them (the verdict)
ran AFTER the decision and only fed the Discord embed, and the brain was bolted on in engine.py
where it matched blocker strings.

How this works instead
----------------------
1. **Evidence ledger.** Every observation becomes one `Check` with a canonical `key`, an owning
   `category` and a provenance `grade`:

       FACT         measured directly (on-chain, a decoded account, a real quote)
       INFERENCE    derived from several observations, or a provider's own opinion
       UNKNOWN      we looked and could not tell
       UNAVAILABLE  the provider could not be reached / the check never ran

   A key appears **once**. If a signal becomes a veto it is removed from scoring entirely, so a
   danger is never both a veto and a points deduction.

2. **Category scores.** Each category is scored only over the checks that actually have a grade
   of FACT or INFERENCE:

       quality  = earned / available_weight        (0..1, how good what we know is)
       coverage = available_weight / total_weight  (how much of the category we know)
       points   = category_weight * quality * (0.5 + 0.5 * coverage)

   So a check that did not run earns nothing, and thin evidence quietly costs score instead of
   silently passing. Absence of a detected risk is never worth points on its own.

3. **Vetoes live outside the score.** Safety is not compensatory: narrative and volume can never
   mathematically outweigh a confirmed critical hazard. The veto layer grades each hazard:

       CONFIRMED_CRITICAL    proven trap            -> REJECT, risk EXTREME
       STRONG_WARNING        serious, well evidenced-> decision capped at CAUTION
       UNVERIFIED_SUSPICION  suspected, not proven  -> noted, costs category points
       UNRESOLVED            provider down / no data on a CRITICAL check -> INSUFFICIENT DATA

   A provider timeout is never evidence of safety.

4. **The brain is a bounded tilt.** `apply_model` moves the score by at most `u_model_tilt`
   points and only when the model is trusted (walk-forward AUC above its bar). It can lower a
   score; it cannot create a STRONG call out of a thin one, and it cannot clear a veto.

5. **The AI is capped evidence.** `apply_ai` writes narrative evidence. A STRONG decision must
   also stand up with at most `u_narrative_cap` points of narrative help, so a good story on a
   bad chart cannot be called.

Score, risk, confidence and completeness stay four separate numbers on purpose. The score is NOT
a probability of reaching 2x — the calibrated probability, when the brain has one, is reported
separately as `model.probability`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

FACT, INFERENCE, UNKNOWN, UNAVAILABLE = "FACT", "INFERENCE", "UNKNOWN", "UNAVAILABLE"
GRADED = (FACT, INFERENCE)

CONFIRMED_CRITICAL = "CONFIRMED_CRITICAL"
STRONG_WARNING = "STRONG_WARNING"
UNVERIFIED_SUSPICION = "UNVERIFIED_SUSPICION"
UNRESOLVED = "UNRESOLVED"

CATEGORIES = ("safety", "liquidity", "holders", "dev", "structure", "narrative")
CATEGORY_LABELS = {
    "safety": "Safety checks that ran clean",
    "liquidity": "Liquidity and exit",
    "holders": "Holders and wallets",
    "dev": "Developer history",
    "structure": "Demand and price structure",
    "narrative": "Narrative and attention",
}

# A call needs real evidence in each of these. Narrative and dev history can be thin; these
# cannot, because nothing else substitutes for them.
ESSENTIAL = ("safety", "liquidity", "holders", "structure")
ESSENTIAL_MIN_COVERAGE = 0.4
ESSENTIAL_MIN_QUALITY = 0.45

# Gates whose UNKNOWN cannot be shrugged off: without these, "clean" is unknowable.
CRITICAL_CHECKS = ("mint_authority", "freeze_authority", "token_extensions", "lp_lock")

# A FAIL here is a property of the token itself — a proven hazard.
SECURITY_GATES = {"mint_authority", "freeze_authority", "token_extensions", "mint_account",
                  "rugcheck_danger", "liquidity_removal", "lp_lock"}
# A FAIL here means a configured threshold was crossed. Serious, but not proof of a trap, so it
# warns and caps the decision instead of rejecting outright.
THRESHOLD_GATES = {"holder_concentration", "creator_holdings", "creator_dumping", "launch_bundle",
                   "liquidity", "price_fresh", "bundled_supply"}

# One hazard, one veto: the gate and the flag that describe the same thing share a canonical key.
HAZARD_ALIASES = {
    "gate:holder_concentration": "haz:concentration", "flag:top10_big": "haz:concentration",
    "flag:top10": "haz:concentration", "flag:whale_holder": "haz:concentration",
    "gate:creator_holdings": "haz:dev_holdings", "flag:dev_holds_big": "haz:dev_holdings",
    "gate:creator_dumping": "haz:dev_selling", "flag:creator_dumping": "haz:dev_selling",
    "flag:dev_sold": "haz:dev_selling",
    "gate:launch_bundle": "haz:bundle", "flag:bundle": "haz:bundle",
    "gate:bundled_supply": "haz:bundle",
    "gate:lp_lock": "haz:lp", "flag:lp_not_locked": "haz:lp",
    "gate:liquidity": "haz:liquidity", "liquidity:thin": "haz:liquidity",
    "liquidity:severe": "haz:liquidity", "liquidity:unknown": "haz:liquidity",
    "exit:no_route": "haz:exit", "exit:limited": "haz:exit", "exit:unchecked": "haz:exit",
    "flag:cannot_sell": "haz:exit", "flag:sell_limit": "haz:exit",
}
SEVERITY_RANK = {UNVERIFIED_SUSPICION: 0, UNRESOLVED: 1, STRONG_WARNING: 2, CONFIRMED_CRITICAL: 3}

# Verdict flags that are proven traps rather than suspicions (verdict.CRITICAL_FLAGS, lowercased).
PROVEN_TRAPS = {
    "rugcheck_rugged", "permanent_delegate", "transfer_hook", "transfer_fee_high",
    "non_transferable", "paused", "pausable", "default_frozen", "cannot_sell",
}
# Serious, well-evidenced, but not proof of a trap.
SERIOUS = {
    "lp_not_locked", "creator_prior_rugs", "creator_dumping", "recycled_socials",
    "serial_launcher", "lp_unlock_soon", "liquidity_removal", "possible_wash_trading",
    "volume_liquidity_implausible", "buying_concentrated", "possible_copycat", "sell_limit",
    "high_slippage",
}

RISK_EXTREME, RISK_HIGH, RISK_MODERATE, RISK_LOW = "EXTREME", "HIGH", "MODERATE", "LOW"
STRONG, WATCH, CAUTION, REJECT, INSUFFICIENT = (
    "STRONG OPPORTUNITY", "WATCH", "CAUTION", "REJECT", "INSUFFICIENT DATA")


@dataclass
class Check:
    """One piece of evidence. `score` is 0..1 quality and only counts when graded."""
    key: str
    category: str
    weight: float
    grade: str
    score: Optional[float] = None
    text: str = ""

    @property
    def counts(self) -> bool:
        return self.grade in GRADED and self.score is not None

    def to_dict(self) -> dict:
        return {"key": self.key, "category": self.category, "weight": self.weight,
                "grade": self.grade, "score": self.score, "text": self.text}


@dataclass
class Veto:
    key: str
    severity: str
    text: str
    grade: str = FACT
    blocking: bool = False      # disqualifies a call without claiming a proven trap

    def to_dict(self) -> dict:
        return {"key": self.key, "severity": self.severity, "text": self.text,
                "grade": self.grade, "blocking": self.blocking}


@dataclass
class Ledger:
    checks: list[Check] = field(default_factory=list)
    vetoes: list[Veto] = field(default_factory=list)
    claimed: set = field(default_factory=set)

    def add(self, key: str, category: str, weight: float, grade: str,
            score: Optional[float] = None, text: str = "") -> None:
        if key in self.claimed:           # a signal is counted once, by its first owner
            return
        self.claimed.add(key)
        self.checks.append(Check(key, category, weight, grade, score, text))

    def veto(self, key: str, severity: str, text: str, grade: str = FACT,
             blocking: bool = False) -> None:
        canonical = HAZARD_ALIASES.get(key, key)
        for v in self.vetoes:
            if v.key == canonical:        # same hazard seen twice: keep the stronger reading
                if SEVERITY_RANK[severity] > SEVERITY_RANK[v.severity]:
                    v.severity, v.text, v.grade = severity, text, grade
                v.blocking = v.blocking or blocking
                return
        self.vetoes.append(Veto(canonical, severity, text, grade, blocking))
        self.claimed.add(canonical)       # vetoed hazards never also cost points
        self.claimed.add(key)
        self.checks = [c for c in self.checks if c.key not in (key, canonical)]


# ---------------------------------------------------------------------------- helpers
def _num(x: Any) -> Optional[float]:
    if isinstance(x, dict):                 # reports wrap values as {"value":…,"status":…}
        x = x.get("value")
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return None if isinstance(x, float) and (math.isnan(x) or math.isinf(x)) else float(x)


def _band(value: Optional[float], bands: list[tuple[float, float]], reverse: bool = False) -> Optional[float]:
    """Map a number to 0..1 through (threshold, score) bands. reverse=True for 'lower is better'."""
    v = _num(value)
    if v is None:
        return None
    for threshold, sc in bands:
        if (v <= threshold) if reverse else (v >= threshold):
            return sc
    return 0.0


def _text(x: Any) -> str:
    """A report field that may be a plain string or an {"value":…} observation."""
    if isinstance(x, dict):
        x = x.get("value")
    return str(x or "")


def _gate(gates: list[dict], name: str) -> Optional[dict]:
    for g in gates or []:
        if isinstance(g, dict) and g.get("name") == name:
            return g
    return None


# ---------------------------------------------------------------------------- the veto layer
def build_vetoes(report: dict, led: Ledger, s: Any = None) -> None:
    """Hazards, graded. Nothing here is compensatory and nothing here can be outscored."""
    gates = [g for g in report.get("gates") or [] if isinstance(g, dict)]
    verdict = report.get("verdict") if isinstance(report.get("verdict"), dict) else {}

    for g in gates:
        name, status, reason = g.get("name") or "gate", g.get("status"), g.get("reason") or ""
        critical = bool(g.get("critical", True))
        if status == "FAIL":
            trap = name in SECURITY_GATES or (critical and name not in THRESHOLD_GATES)
            led.veto(f"gate:{name}", CONFIRMED_CRITICAL if trap else STRONG_WARNING,
                     f"{name.replace('_', ' ')}: {reason}", blocking=not trap)
        elif status == "UNKNOWN" and name in CRITICAL_CHECKS:
            # without a trap check there is no way to call anything clean
            led.veto(f"gate:{name}", UNRESOLVED,
                     f"{name.replace('_', ' ')} could not be verified: {reason}", grade=UNAVAILABLE)
        elif status == "UNKNOWN" and critical:
            # important but not a trap check: restrict rather than reject
            led.veto(f"gate:{name}", UNVERIFIED_SUSPICION,
                     f"{name.replace('_', ' ')} unresolved: {reason}", grade=UNKNOWN)

    # The rug verdict is the component measured against real outcomes, so its red flags are
    # hazards here rather than decoration on the embed.
    for r in verdict.get("red") or []:
        if not isinstance(r, dict):
            continue
        key, level, text = str(r.get("key") or ""), r.get("level"), str(r.get("text") or "")
        if not key:
            continue
        crash, seen = _num(r.get("crash_rate")), _num(r.get("seen")) or 0
        if key in PROVEN_TRAPS or level == "critical":
            led.veto(f"flag:{key}", CONFIRMED_CRITICAL, text)
        elif crash is not None and seen >= 10:
            # measured against Northstar's own tracked outcomes, which beats any hand-set list
            sev = STRONG_WARNING if crash >= 0.8 else UNVERIFIED_SUSPICION
            led.veto(f"flag:{key}", sev,
                     f"{text} (crashed {crash * 100:.0f}% of {seen:.0f} times)", grade=INFERENCE)
        elif key in SERIOUS or level == "high":
            led.veto(f"flag:{key}", STRONG_WARNING, text, grade=INFERENCE)
        else:
            led.veto(f"flag:{key}", UNVERIFIED_SUSPICION, text, grade=INFERENCE)

    # Exit: a coin you cannot sell is a trap however good it looks.
    sell = report.get("sellability") if isinstance(report.get("sellability"), dict) else {}
    checked = sell.get("checked") or []
    if checked:
        smallest = min((_num(c.get("usd")) or 0.0) for c in checked if isinstance(c, dict))
        blocked = _num(sell.get("no_route_at_usd"))
        if blocked is not None and smallest and blocked <= smallest:
            led.veto("exit:no_route", CONFIRMED_CRITICAL,
                     f"cannot sell even ${smallest:,.0f} back to SOL right now")
        elif blocked is not None:
            led.veto("exit:limited", STRONG_WARNING,
                     f"a ${blocked:,.0f} sell cannot be filled")
    elif getattr(s, "enable_jupiter", False):
        led.veto("exit:unchecked", UNVERIFIED_SUSPICION,
                 "exit cost could not be checked — size down", grade=UNAVAILABLE)

    # Liquidity floor: a hard policy line, not a score input.
    liq = _num((report.get("market") or {}).get("liquidity_usd"))
    floor = _num(getattr(s, "min_liquidity_usd", None)) or 0.0
    if liq is not None and floor and liq < floor * 0.5:
        led.veto("liquidity:severe", CONFIRMED_CRITICAL,
                 f"liquidity ${liq:,.0f} is less than half the ${floor:,.0f} minimum")
    elif liq is not None and floor and liq < floor:
        led.veto("liquidity:thin", STRONG_WARNING,
                 f"liquidity ${liq:,.0f} below the ${floor:,.0f} minimum", blocking=True)
    elif liq is None:
        led.veto("liquidity:unknown", UNRESOLVED, "liquidity is unknown", grade=UNAVAILABLE)
        # liquidity is the one market fact nothing else can substitute for


def contradictions(report: dict, led: Ledger, s: Any) -> list[str]:
    """Where two sources disagree. A contradiction lowers confidence; it never silently averages.

    Averaging two sources that disagree invents a number neither of them reported, so each
    disagreement is recorded, costs confidence, and is shown to the reader instead.
    """
    out: list[str] = []
    m = report.get("market") or {}
    sec = report.get("security") or {}
    sell = report.get("sellability") if isinstance(report.get("sellability"), dict) else {}
    liq = _num(m.get("liquidity_usd"))

    # deep liquidity on paper but nothing will route: one of the two is wrong
    if liq is not None and liq >= 10_000 and sell.get("checked") and _num(sell.get("no_route_at_usd")) is not None:
        out.append(f"${liq:,.0f} of liquidity is reported but a "
                   f"${_num(sell['no_route_at_usd']):,.0f} sell will not route")
    # provider says the LP is locked while the on-chain read says it is not
    lp = _text(sec.get("lp_status"))
    onchain = report.get("lp_onchain") if isinstance(report.get("lp_onchain"), dict) else {}
    burned = onchain.get("burned_pct")
    if lp.startswith(("BURNED", "LOCKED")) and _num(burned) is not None and _num(burned) < 95:
        out.append(f"RugCheck reports the LP locked while the pool shows {_num(burned):.0f}% burned")
    # market cap and liquidity that cannot both be right
    mcap = _num(m.get("market_cap"))
    if liq is not None and mcap is not None and mcap > 0 and liq > mcap:
        out.append(f"liquidity ${liq:,.0f} is larger than the reported ${mcap:,.0f} market cap")
    for text in out:
        led.veto(f"conflict:{abs(hash(text)) % 10 ** 8}", UNVERIFIED_SUSPICION,
                 f"sources disagree — {text}", grade=UNKNOWN)
    return out


def policy_checks(report: dict, led: Ledger, s: Any) -> None:
    """Thresholds the operator (and the learning loop) set in config, kept intact from v2.3.0.

    These are not new opinions: they are the same bars `decision.decide()` applied, carried over
    so a tuned bot behaves the same way. They disqualify a call without claiming a proven trap.
    """
    ms = report.get("market_structure") or {}
    nr = report.get("narrative") or {}
    tr = report.get("trades") or {}

    if ms.get("extended") and getattr(s, "block_extended_moves", True):
        led.veto("policy:extended", STRONG_WARNING,
                 "price already extended — wait for consolidation", grade=INFERENCE, blocking=True)
    items = _num(nr.get("evidence_items"))
    need = _num(getattr(s, "min_narrative_evidence_items", None)) or 0
    if need and items is not None and items < need:
        led.veto("policy:narrative", STRONG_WARNING,
                 f"only {items:.0f} independent narrative evidence item(s), need {need:.0f}",
                 grade=INFERENCE, blocking=True)
    min_acc = _num(getattr(s, "min_volume_acceleration", None)) or 0
    acc = _num(ms.get("volume_acceleration"))
    if min_acc and (acc is None or acc < min_acc):
        led.veto("policy:volume", STRONG_WARNING,
                 "volume fading: 1h volume is "
                 + ("UNKNOWN" if acc is None else f"{acc:.2f}x")
                 + f" its 6h hourly average (need {min_acc:g}x)", grade=INFERENCE, blocking=True)
    min_bs = _num(getattr(s, "min_buy_sell_ratio_h1", None)) or 0
    bs = _num(ms.get("buy_sell_ratio_h1"))
    if min_bs and bs is not None and bs < min_bs:
        led.veto("policy:pressure", STRONG_WARNING,
                 f"weak buying: 1h buy/sell ratio {bs:.2f} (need {min_bs:g})",
                 grade=INFERENCE, blocking=True)
    status = str(tr.get("status") or "")
    if status and status not in ("VERIFIED", "UNAVAILABLE"):
        # demand that was never verified on-chain is not good enough to call on
        led.veto("data:unverified_trades", STRONG_WARNING,
                 f"the demand sample is {status}, not verified on-chain", grade=INFERENCE)


# ---------------------------------------------------------------------------- category evidence
def build_checks(report: dict, led: Ledger, s: Any = None) -> None:
    m = report.get("market") or {}
    gates = [g for g in report.get("gates") or [] if isinstance(g, dict)]
    sec = report.get("security") or {}
    h = report.get("holders") or {}
    cl = report.get("clusters") or {}
    tr = report.get("trades") or {}
    ms = report.get("market_structure") or {}
    ch = report.get("creator") or {}
    nr = report.get("narrative") or {}
    sell = report.get("sellability") if isinstance(report.get("sellability"), dict) else {}

    # ---- safety: ONLY checks that ran and came back clean earn points here
    for name, weight in (("mint_authority", 3.0), ("freeze_authority", 3.0),
                         ("token_extensions", 2.0), ("liquidity_removal", 1.0)):
        g = _gate(gates, name)
        if g is None:
            led.add(f"clean:{name}", "safety", weight, UNAVAILABLE, text=f"{name} not checked")
        elif g.get("status") == "PASS":
            led.add(f"clean:{name}", "safety", weight, FACT, 1.0, g.get("reason") or name)
        elif g.get("status") == "UNKNOWN":
            led.add(f"clean:{name}", "safety", weight, UNKNOWN, text=g.get("reason") or name)
        # FAIL is already a veto; `claimed` keeps it out of here

    lp = _text(sec.get("lp_status")) or _text((report.get("liquidity") or {}).get("lp_status"))
    if lp.startswith(("BURNED", "LOCKED", "BONDING")):
        led.add("clean:lp", "safety", 4.0, FACT, 1.0, f"LP {lp.split(' — ')[0].lower()}")
    elif lp.startswith(("PARTIAL", "CONCENTRATED")):
        led.add("clean:lp", "safety", 4.0, INFERENCE, 0.3, f"LP {lp.split(' — ')[0].lower()}")
    else:
        led.add("clean:lp", "safety", 4.0, UNKNOWN, text="LP lock/burn status unknown")

    rc_gate = _gate(gates, "rugcheck_danger")
    rc_ran = report.get("rugcheck") is not None or (rc_gate or {}).get("status") == "PASS"
    led.add("clean:rugcheck", "safety", 2.0, FACT if rc_ran else UNAVAILABLE,
            1.0 if rc_ran else None,
            "RugCheck returned a clean report" if rc_ran else "RugCheck unavailable")

    # ---- liquidity and exit
    liq, mcap = _num(m.get("liquidity_usd")), _num(m.get("market_cap"))
    floor = _num(getattr(s, "min_liquidity_usd", None)) or 1.0
    if liq is not None:
        led.add("liq:absolute", "liquidity", 4.0, FACT,
                max(0.0, min(1.0, math.log10(max(liq, 1) / floor))), f"liquidity ${liq:,.0f}")
    else:
        led.add("liq:absolute", "liquidity", 4.0, UNAVAILABLE, text="liquidity unknown")

    ratio = _num(sec.get("liquidity_to_mcap"))
    if ratio is None:
        ratio = (liq / mcap) if (liq and mcap) else None
    led.add("liq:ratio", "liquidity", 3.0, FACT if ratio is not None else UNAVAILABLE,
            _band(ratio, [(0.2, 1.0), (0.12, 0.75), (0.06, 0.45), (0.0, 0.15)]),
            f"liquidity is {ratio * 100:.1f}% of market cap" if ratio is not None
            else "liquidity/market-cap ratio unknown")

    worst = _num(sell.get("worst_impact_pct"))
    if worst is not None:
        led.add("exit:impact", "liquidity", 5.0, FACT,
                _band(worst, [(2.0, 1.0), (5.0, 0.7), (10.0, 0.4), (15.0, 0.2)], reverse=True),
                f"worst price impact {worst:.1f}% on the sizes tested")
        best = _num(sell.get("max_sellable_usd"))
        if best is not None:
            led.add("exit:depth", "liquidity", 3.0, FACT,
                    _band(best, [(1000.0, 1.0), (500.0, 0.7), (100.0, 0.4)]),
                    f"${best:,.0f} sells with a route")
    else:
        led.add("exit:impact", "liquidity", 5.0, UNAVAILABLE, text="exit cost not measured")

    vtl = _num(sec.get("volume_to_liquidity_h1"))
    if vtl is None:
        vtl = _num((report.get("liquidity") or {}).get("vol_to_liq_h1"))
    if vtl is None and liq:
        v1 = _num((m.get("volume") or {}).get("h1"))
        vtl = v1 / liq if v1 is not None else None
    led.add("liq:churn", "liquidity", 2.0, INFERENCE if vtl is not None else UNAVAILABLE,
            None if vtl is None else (1.0 if 0.2 <= vtl <= 3 else 0.5 if vtl <= 8 else 0.0),
            f"1h volume is {vtl:.1f}x liquidity" if vtl is not None else "volume/liquidity unknown")

    # ---- holders and wallets
    usable = str(h.get("status") or "") in ("VERIFIED", "OBSERVED", "INFERRED")
    top10 = _num(h.get("top10_adj")) if usable else None
    led.add("hold:top10", "holders", 5.0, FACT if top10 is not None else UNAVAILABLE,
            _band(top10, [(15.0, 1.0), (25.0, 0.8), (35.0, 0.5), (50.0, 0.2)], reverse=True),
            f"adjusted top-10 hold {top10:.1f}%" if top10 is not None else "holder distribution unavailable")
    single = _num(h.get("max_single_adj")) if usable else None
    led.add("hold:single", "holders", 3.0, FACT if single is not None else UNAVAILABLE,
            _band(single, [(3.0, 1.0), (5.0, 0.7), (8.0, 0.3)], reverse=True),
            f"largest wallet holds {single:.2f}%" if single is not None else "largest holder unknown")
    creator_pct = _num(h.get("creator_pct"))
    led.add("hold:creator", "holders", 3.0, FACT if creator_pct is not None else UNKNOWN,
            _band(creator_pct, [(1.0, 1.0), (3.0, 0.7), (5.0, 0.4)], reverse=True),
            f"creator holds {creator_pct:.2f}%" if creator_pct is not None else "creator holding unknown")

    fresh = _num(cl.get("fresh_supply_pct"))
    if fresh is None:
        fresh = _num(cl.get("fresh_wallet_share"))
    led.add("wal:fresh", "holders", 3.0, INFERENCE if fresh is not None else UNAVAILABLE,
            _band(fresh, [(20.0, 1.0), (40.0, 0.6), (60.0, 0.3)], reverse=True),
            f"{fresh:.0f}% fresh-wallet presence among traced wallets" if fresh is not None
            else "wallet ages not traced")
    cluster = _num(cl.get("max_cluster_supply_pct"))
    led.add("wal:clusters", "holders", 2.0, INFERENCE if cluster is not None else UNAVAILABLE,
            _band(cluster, [(3.0, 1.0), (6.0, 0.6), (10.0, 0.3)], reverse=True),
            f"largest connected wallet group holds {cluster:.1f}%" if cluster is not None
            else "wallet clustering unavailable")

    # ---- developer
    status = str(ch.get("status") or "")
    summary = str(ch.get("summary") or "")
    if status in ("", "UNKNOWN"):
        led.add("dev:history", "dev", 6.0, UNAVAILABLE, text="developer history unknown")
    elif (_num(ch.get("prior_rugged")) or 0) > 0:
        led.add("dev:history", "dev", 6.0, FACT, 0.0, summary or "developer has rugged before")
    elif ch.get("flags"):
        led.add("dev:history", "dev", 6.0, INFERENCE, 0.4, summary or "developer history has flags")
    else:
        led.add("dev:history", "dev", 6.0, FACT, 1.0, summary or "no prior rugs found for this developer")
    prior = _num(ch.get("prior_launches"))
    if prior is None:
        prior = _num(ch.get("prior_tokens"))
    led.add("dev:volume", "dev", 2.0, FACT if prior is not None else UNAVAILABLE,
            _band(prior, [(10.0, 0.0), (4.0, 0.4), (1.0, 0.8), (0.0, 1.0)], reverse=True),
            f"{prior:.0f} earlier tokens by this developer" if prior is not None
            else "earlier-token count unknown")

    # ---- demand and structure
    sample = _num(tr.get("sample_size")) or 0
    buyers = _num(tr.get("unique_buyers"))
    if sample >= 15 and buyers is not None:
        led.add("str:buyers", "structure", 4.0, FACT,
                _band(buyers, [(60.0, 1.0), (30.0, 0.7), (15.0, 0.4)]),
                f"{buyers:.0f} different buyers in a {sample:.0f}-swap on-chain sample")
        buys = _num(tr.get("buys")) or 0
        div = (buyers / buys) if buys else None
        led.add("str:diversity", "structure", 2.0, FACT if div is not None else UNKNOWN,
                None if div is None else max(0.0, min(1.0, div / 0.7)),
                f"{buyers:.0f} buyers across {buys:.0f} buys" if div is not None else "")
        trend = str(tr.get("buyer_trend") or "").lower()
        led.add("str:trend", "structure", 3.0,
                FACT if trend in ("rising", "flat", "declining") else UNKNOWN,
                {"rising": 1.0, "flat": 0.5, "declining": 0.0}.get(trend),
                f"unique buyers {trend}" if trend else "buyer trend not measurable")
    else:
        led.add("str:buyers", "structure", 4.0, UNAVAILABLE,
                text="on-chain trade sample unavailable — unique buyers unknown")

    acc = _num(ms.get("volume_acceleration"))
    led.add("str:volume", "structure", 3.0, INFERENCE if acc is not None else UNAVAILABLE,
            None if acc is None else (1.0 if 1.0 <= acc <= 6 else 0.6 if acc < 1.0 else 0.2),
            f"1h volume is {acc:.2f}x its 6h hourly average" if acc is not None
            else "volume trend unknown")
    bs = _num(ms.get("buy_sell_ratio_h1"))
    led.add("str:pressure", "structure", 3.0, INFERENCE if bs is not None else UNAVAILABLE,
            _band(bs, [(1.4, 1.0), (1.1, 0.75), (0.9, 0.45), (0.0, 0.1)]),
            f"1h buy/sell transaction ratio {bs:.2f}" if bs is not None else "buy/sell pressure unknown")

    if ms.get("status") == "UNAVAILABLE":
        led.add("str:shape", "structure", 4.0, UNAVAILABLE, text="price structure unavailable")
    else:
        shape, note = 0.5, "price structure read from stored snapshots"
        if ms.get("extended"):
            shape, note = 0.05, "price already extended"
        elif ms.get("fading"):
            shape, note = 0.1, "fading after a pump"
        elif ms.get("vertical_thin"):
            shape, note = 0.1, "vertical move on thin liquidity"
        elif ms.get("consolidating") or ms.get("constructive"):
            shape, note = 1.0, "constructive: consolidating rather than vertical"
        led.add("str:shape", "structure", 4.0, INFERENCE, shape, note)
        dd = _num(ms.get("observed_drawdown_pct"))
        led.add("str:drawdown", "structure", 2.0, INFERENCE if dd is not None else UNKNOWN,
                _band(dd, [(15.0, 1.0), (35.0, 0.6), (60.0, 0.3)], reverse=True),
                f"observed drawdown {dd:.0f}% in stored snapshots" if dd is not None else "")

    # ---- narrative and attention (real data only; social momentum needs paid X access)
    items = _num(nr.get("evidence_items"))
    led.add("nar:evidence", "narrative", 4.0, INFERENCE if items is not None else UNAVAILABLE,
            _band(items, [(3.0, 1.0), (2.0, 0.7), (1.0, 0.4), (0.0, 0.0)]),
            f"{items:.0f} independent narrative evidence item(s)" if items is not None
            else "narrative evidence not gathered")
    meta = str(nr.get("meta_status") or "")
    led.add("nar:meta", "narrative", 2.0, INFERENCE if meta == "OBSERVED" else UNAVAILABLE,
            1.0 if nr.get("meta_match") else 0.3 if meta == "OBSERVED" else None,
            "matches a trending meta" if nr.get("meta_match")
            else ("trending metas checked, no match" if meta == "OBSERVED" else "trending metas unavailable"))
    socials = report.get("socials") or nr.get("socials") or {}
    if isinstance(socials, dict) and socials:
        has = bool(socials.get("twitter") or socials.get("telegram") or socials.get("website"))
        led.add("nar:socials", "narrative", 2.0, FACT, 1.0 if has else 0.2,
                "socials present" if has else "no socials found")
    elif str(nr.get("social_status") or "") == "OBSERVED":
        led.add("nar:socials", "narrative", 2.0, INFERENCE, 0.6, "socials checked")
    else:
        led.add("nar:socials", "narrative", 2.0, UNAVAILABLE,
                text="social links unavailable (follower/tweet velocity needs paid X access)")
    crowd = _num((report.get("context") or {}).get("ticker_crowding_6h"))
    if crowd is None:
        crowd = _num(nr.get("competing_tokens"))
    led.add("nar:crowding", "narrative", 2.0, FACT if crowd is not None else UNAVAILABLE,
            _band(crowd, [(3.0, 1.0), (1.0, 0.7), (0.0, 0.3)]),
            f"{crowd:.0f} other launches share this ticker in 6h" if crowd is not None
            else "ticker crowding unknown")
    # nar:ai is written by apply_ai(); unavailable until then
    led.add("nar:ai", "narrative", 6.0, UNAVAILABLE, text="AI narrative read not available")


# ---------------------------------------------------------------------------- aggregation
def _weights(s: Any) -> dict:
    g = lambda n, d: _num(getattr(s, n, None)) if _num(getattr(s, n, None)) is not None else d  # noqa: E731
    return {"safety": g("u_w_safety", 18.0), "liquidity": g("u_w_liquidity", 20.0),
            "holders": g("u_w_holders", 16.0), "dev": g("u_w_dev", 10.0),
            "structure": g("u_w_structure", 20.0), "narrative": g("u_w_narrative", 16.0)}


def score_categories(checks: list[Check], s: Any = None) -> dict:
    """points, quality, coverage and the evidence behind every category."""
    weights, out = _weights(s), {}
    floor = _num(getattr(s, "u_thin_category_floor", None))
    floor = 0.5 if floor is None else floor
    for cat in CATEGORIES:
        mine = [c for c in checks if c.category == cat]
        total = sum(c.weight for c in mine) or 1.0
        counted = [c for c in mine if c.counts]
        avail = sum(c.weight for c in counted)
        earned = sum(c.weight * (c.score or 0.0) for c in counted)
        quality = (earned / avail) if avail else None
        coverage = avail / total
        pts = 0.0 if quality is None else weights[cat] * quality * (0.5 + 0.5 * coverage)
        out[cat] = {
            "label": CATEGORY_LABELS[cat], "weight": round(weights[cat], 1),
            "points": round(pts, 2), "quality": None if quality is None else round(quality, 3),
            "coverage": round(coverage, 3), "thin": coverage < floor,
            "evidence": [c.text for c in counted if c.text and (c.score or 0) >= 0.5][:4],
            "weak": [c.text for c in counted if c.text and (c.score or 0) < 0.5][:4],
            "missing": [c.text for c in mine if not c.counts and c.text][:4],
        }
    return out


def _completeness(checks: list[Check]) -> float:
    total = sum(c.weight for c in checks) or 1.0
    return round(sum(c.weight for c in checks if c.counts) / total, 3)


def _confidence(completeness: float, cats: dict, vetoes: list[Veto], model: Optional[dict]) -> str:
    unresolved = sum(1 for v in vetoes if v.severity == UNRESOLVED)
    thin = sum(1 for c in cats.values() if c["thin"])
    if completeness >= 0.75 and unresolved == 0 and thin <= 1:
        conf = "HIGH"
    elif completeness >= 0.55 and unresolved <= 1:
        conf = "MEDIUM"
    else:
        conf = "LOW"
    if model and model.get("trusted") is False and conf == "HIGH":
        conf = "MEDIUM"      # without a trusted model the read is thinner than it looks
    return conf


def _risk(vetoes: list[Veto], verdict: dict, completeness: float) -> str:
    if any(v.severity == CONFIRMED_CRITICAL for v in vetoes):
        return RISK_EXTREME
    strong = [v for v in vetoes if v.severity == STRONG_WARNING]
    level = str((verdict or {}).get("level") or "")
    if any(v.blocking for v in vetoes) or len(strong) >= 2 or level == "HIGH RISK":
        return RISK_HIGH
    if strong or level == "CAUTION":
        return RISK_MODERATE
    if completeness < 0.55 or any(v.severity == UNRESOLVED for v in vetoes):
        return RISK_MODERATE     # never "low risk" on data we do not have
    return RISK_LOW


def _entry(report: dict, cats: dict) -> dict:
    ms = report.get("market_structure") or {}
    sell = report.get("sellability") if isinstance(report.get("sellability"), dict) else {}
    reasons, ok = [], True
    if ms.get("extended"):
        ok, _ = False, reasons.append("price is extended — wait for consolidation")
    if ms.get("fading"):
        ok, _ = False, reasons.append("momentum is fading")
    if ms.get("vertical_thin"):
        ok, _ = False, reasons.append("vertical move on thin liquidity")
    best = _num(sell.get("max_sellable_usd"))
    if best is not None and best < 500:
        ok, _ = False, reasons.append(f"only about ${best:,.0f} can be sold at once")
    if cats["liquidity"]["thin"]:
        reasons.append("exit depth not fully measured")
    if ok and not reasons:
        reasons.append("structure and exit depth both look acceptable right now")
    return {"ok": ok, "notes": reasons[:4]}


def _invalidation(report: dict, s: Any) -> list[str]:
    m = report.get("market") or {}
    out = []
    liq = _num(m.get("liquidity_usd"))
    if liq:
        out.append(f"liquidity falls below ${liq * 0.6:,.0f} (40% of the ${liq:,.0f} seen now)")
    out.append("LP stops being fully burned/locked, or an unlock appears")
    h = _num((report.get("holders") or {}).get("creator_pct"))
    if h is not None:
        out.append(f"the developer's {h:.2f}% starts moving")
    out.append("a sell of the size you intend stops routing, or impact doubles")
    price = _num(m.get("price_usd"))
    if price:
        out.append(f"price closes below ${price * 0.65:.8g} (0.65x of the callout price)")
    return out[:5]


def _exit_triggers(report: dict) -> list[str]:
    sell = report.get("sellability") if isinstance(report.get("sellability"), dict) else {}
    out = ["the rug verdict worsens on a re-check (new red flag or a red flag upgraded)",
           "liquidity drops sharply from its recent high",
           "unique buyers turn from rising to declining"]
    worst = _num(sell.get("worst_impact_pct"))
    if worst is not None:
        out.append(f"price impact on the same sell size rises above {max(5.0, worst * 2):.0f}%")
    out.append("the developer's wallet or a connected group starts distributing")
    return out


def _essential_gaps(cats: dict) -> list[str]:
    """Essential categories that are too blind or too weak for a call."""
    gaps = []
    for cat in ESSENTIAL:
        c = cats.get(cat) or {}
        cov, q = _num(c.get("coverage")) or 0.0, c.get("quality")
        if cov < ESSENTIAL_MIN_COVERAGE:
            gaps.append(f"{CATEGORY_LABELS[cat].lower()}: only {cov * 100:.0f}% of it is known")
        elif q is not None and q < ESSENTIAL_MIN_QUALITY:
            gaps.append(f"{CATEGORY_LABELS[cat].lower()}: what is known is weak "
                        f"({q * 100:.0f}% of the available points)")
    return gaps


def _decide(score: float, score_ex_nar: float, risk: str, confidence: str, completeness: float,
            vetoes: list[Veto], s: Any, cats: Optional[dict] = None,
            entry: Optional[dict] = None) -> tuple[str, list[str]]:
    """Safety first, then evidence, then the score. Caps are graded, not all-or-nothing."""
    why: list[str] = []
    crit = [v for v in vetoes if v.severity == CONFIRMED_CRITICAL]
    if crit:
        return REJECT, [f"hard veto: {v.text}" for v in crit[:3]]
    unresolved = [v for v in vetoes if v.severity == UNRESOLVED]
    if unresolved:
        return INSUFFICIENT, [f"unresolved critical check: {v.text}" for v in unresolved[:3]]
    blocking = [v for v in vetoes if v.blocking]
    if blocking:
        return REJECT, [v.text for v in blocking[:3]]

    strong_min = _num(getattr(s, "u_strong_min", None)) or 68.0
    watch_min = _num(getattr(s, "u_watch_min", None)) or 48.0
    min_comp = _num(getattr(s, "u_min_completeness", None))
    min_comp = 0.55 if min_comp is None else min_comp
    nar_cap = _num(getattr(s, "u_narrative_cap", None)) or 20.0

    strong_warnings = [v for v in vetoes if v.severity == STRONG_WARNING]
    suspicions = [v for v in vetoes if v.severity == UNVERIFIED_SUSPICION]
    gaps = _essential_gaps(cats or {})

    # Graded caps, matching what the verdict measured on real outcomes: two serious red flags
    # behaved like HIGH RISK (95% crashed), one behaved like CAUTION (67%).
    ceiling = STRONG
    if entry is not None and not entry.get("ok", True):
        # a call says "this is worth entering now"; if entry is not justified, it is a WATCH
        ceiling = WATCH
        why.append("entry conditions are not right now: "
                   + "; ".join((entry.get("notes") or [])[:2]))
    if gaps:
        ceiling = WATCH
        why.append("not enough to call on — " + "; ".join(gaps[:2]))
    if len(strong_warnings) >= 2:
        ceiling = CAUTION
        why.append("two or more serious warnings: " + "; ".join(v.text for v in strong_warnings[:2]))
    elif strong_warnings:
        ceiling = WATCH
        why.append(f"serious warning: {strong_warnings[0].text}")
    elif len(suspicions) >= 3:
        ceiling = WATCH
        why.append(f"{len(suspicions)} unverified concerns, none proven")

    if score < watch_min:
        why.append(f"score {score:.1f} below the {watch_min:.0f} watch line")
        return (CAUTION if ceiling == CAUTION else REJECT), why

    if score >= strong_min and ceiling == STRONG and risk in (RISK_LOW, RISK_MODERATE) \
            and confidence != "LOW" and completeness >= min_comp \
            and score_ex_nar >= strong_min - nar_cap:
        return STRONG, [f"score {score:.1f}, {risk.lower()} risk, {confidence.lower()} confidence, "
                        f"{completeness * 100:.0f}% of the evidence in hand, no serious warnings"]

    if score >= strong_min and ceiling == STRONG:
        if score_ex_nar < strong_min - nar_cap:
            why.append("the score leans too heavily on narrative to be a STRONG call")
        if completeness < min_comp:
            why.append(f"only {completeness * 100:.0f}% of the evidence was obtained "
                       f"(need {min_comp * 100:.0f}%)")
        if confidence == "LOW":
            why.append("confidence is LOW")
        if risk == RISK_HIGH:
            why.append("risk is HIGH")

    if ceiling == CAUTION or risk == RISK_HIGH:
        return CAUTION, why
    if not why:
        why.append(f"score {score:.1f} is between the {watch_min:.0f} watch line "
                   f"and the {strong_min:.0f} call line")
    return WATCH, why


# ---------------------------------------------------------------------------- public API
def evaluate(report: dict, s: Any = None) -> dict:
    """The whole unified result for one research report. JSON-serialisable."""
    led = Ledger()
    build_vetoes(report, led, s)          # vetoes first: they claim their keys
    conflicts = contradictions(report, led, s)
    policy_checks(report, led, s)
    build_checks(report, led, s)
    return _assemble(led, report, s, model=None, ai=None, conflicts=conflicts)


def _assemble(led: Ledger, report: dict, s: Any, model: Optional[dict], ai: Optional[dict],
              conflicts: Optional[list[str]] = None) -> dict:
    cats = score_categories(led.checks, s)
    base = sum(c["points"] for c in cats.values())
    ex_nar = base - cats["narrative"]["points"]
    tilt, tilt_note = 0.0, None
    if model and model.get("trusted") and _num(model.get("probability")) is not None:
        p = _num(model["probability"])
        baseline = _num(model.get("baseline")) or 0.15
        cap = _num(getattr(s, "u_model_tilt", None)) or 12.0
        # how far the odds sit from the base rate, in units of the base rate, clamped to +/-1
        rel = (p - baseline) / max(baseline, 0.02)
        tilt = round(cap * max(-1.0, min(1.0, rel)), 2)
        tilt_note = (f"learning brain: {p * 100:.0f}% learned odds vs {baseline * 100:.0f}% base rate "
                     f"-> {tilt:+.1f} points")
    suspicions = [v for v in led.vetoes if v.severity == UNVERIFIED_SUSPICION]
    penalty = round(min(9.0, 3.0 * len(suspicions)), 1)   # unproven concerns still cost something
    # 99 is the ceiling on purpose: a score of 100 reads as certainty, and nothing here is
    # certain. The brain's tilt can otherwise push an already strong coin past the top.
    score = round(max(0.0, min(99.0, base + tilt - penalty)), 1)
    completeness = _completeness(led.checks)
    verdict = report.get("verdict") if isinstance(report.get("verdict"), dict) else {}
    risk = _risk(led.vetoes, verdict, completeness)
    confidence = _confidence(completeness, cats, led.vetoes, model)
    conflicts = list(conflicts or [])
    if conflicts and confidence == "HIGH":
        confidence = "MEDIUM"      # sources disagree: do not claim high confidence
    entry = _entry(report, cats)
    decision, why = _decide(score, round(ex_nar + tilt - penalty, 1), risk, confidence,
                            completeness, led.vetoes, s, cats, entry)

    # Most decision-relevant first: weight x quality, with real signals ahead of the routine
    # "authority revoked" confirmations, which matter but are true of most tokens.
    counted = [c for c in led.checks if c.counts and c.text]
    strong_first = sorted(counted, key=lambda c: -(c.weight * (c.score or 0.0)))
    good = [c for c in strong_first if (c.score or 0) >= 0.6]
    positives = ([c.text for c in good if not c.key.startswith("clean:")][:6]
                 + [c.text for c in good if c.key.startswith("clean:")][:2])
    negatives = ([v.text for v in led.vetoes if v.severity != UNRESOLVED]
                 + [c.text for c in sorted(counted, key=lambda c: -c.weight)
                    if (c.score or 0) < 0.4])

    return {
        "version": 1,
        "score": score, "base_score": round(base, 1),
        "score_ex_narrative": round(ex_nar + tilt - penalty, 1),
        "penalty": penalty,
        "penalty_reasons": [v.text for v in suspicions][:4],
        "risk": risk, "decision": decision, "confidence": confidence,
        "completeness": completeness, "why": why,
        "gaps": _essential_gaps(cats),
        "categories": cats,
        "model": ({**model, "tilt": tilt, "note": tilt_note} if model else
                  {"probability": None, "trusted": False, "tilt": 0.0,
                   "note": "learning brain not consulted"}),
        "ai": ai or {"used": False},
        "conflicts": conflicts,
        "vetoes": [v.to_dict() for v in led.vetoes],
        "hard_vetoes": [v.text for v in led.vetoes if v.severity == CONFIRMED_CRITICAL],
        "warnings": [v.text for v in led.vetoes if v.severity == STRONG_WARNING],
        "blocking": [v.text for v in led.vetoes if v.blocking],
        "unresolved": [v.text for v in led.vetoes if v.severity == UNRESOLVED],
        "suspicions": [v.text for v in led.vetoes if v.severity == UNVERIFIED_SUSPICION],
        "positives": positives[:8], "negatives": negatives[:8],
        "entry": entry,
        "invalidation": _invalidation(report, s),
        "exit_triggers": _exit_triggers(report),
        "checks": [c.to_dict() for c in led.checks],
    }


def _reload(u: dict) -> Ledger:
    led = Ledger()
    led.checks = [Check(**{k: c[k] for k in ("key", "category", "weight", "grade", "score", "text")})
                  for c in u.get("checks") or []]
    led.vetoes = [Veto(v["key"], v["severity"], v["text"], v.get("grade", FACT),
                       bool(v.get("blocking"))) for v in u.get("vetoes") or []]
    led.claimed = {c.key for c in led.checks} | {v.key for v in led.vetoes}
    return led


def apply_model(u: dict, report: dict, probability: Optional[float], trusted: bool,
                baseline: Optional[float] = None, auc: Optional[float] = None,
                s: Any = None) -> dict:
    """Fold the learning brain in as a bounded tilt and re-derive the decision."""
    model = {"probability": None if probability is None else round(float(probability), 4),
             "trusted": bool(trusted), "baseline": baseline, "auc": auc}
    return _assemble(_reload(u), report, s, model=model, ai=u.get("ai"),
                     conflicts=u.get("conflicts"))


def apply_ai(u: dict, report: dict, ai: Optional[dict], s: Any = None) -> dict:
    """Fold an AI narrative read in as capped evidence and re-derive the decision."""
    led = _reload(u)
    if isinstance(ai, dict) and ai.get("verdict"):
        sc = _num(ai.get("score"))
        quality = {"CALL": 1.0, "WATCH": 0.5, "PASS": 0.0}.get(str(ai["verdict"]).upper())
        if sc is not None:
            quality = max(0.0, min(1.0, sc / 100.0))
        led.claimed.discard("nar:ai")
        led.checks = [c for c in led.checks if c.key != "nar:ai"]
        led.add("nar:ai", "narrative", 6.0, INFERENCE, quality,
                f"AI read: {str(ai['verdict']).upper()}"
                + (f" ({sc:.0f}/100)" if sc is not None else "")
                + (f" — {str(ai.get('reason'))[:120]}" if ai.get("reason") else ""))
        used = {"used": True, "verdict": str(ai["verdict"]).upper(), "score": sc,
                "reason": ai.get("reason"), "provider": ai.get("provider")}
    else:
        used = {"used": False, "note": "AI judge unavailable or gave no usable answer"}
    model = u.get("model") if isinstance(u.get("model"), dict) and u["model"].get("probability") is not None else None
    return _assemble(led, report, s, model=model, ai=used, conflicts=u.get("conflicts"))


# ---------------------------------------------------------------------------- presentation
EMOJI = {STRONG: "🟢", WATCH: "🟡", CAUTION: "🟠", REJECT: "🔴", INSUFFICIENT: "⚪"}
RISK_EMOJI = {RISK_LOW: "🟢", RISK_MODERATE: "🟡", RISK_HIGH: "🟠", RISK_EXTREME: "☠️"}


def panel_text(u: dict, max_lines: int = 4) -> str:
    """The one decision panel used on Discord and in the dashboard."""
    d = str(u.get("decision") or INSUFFICIENT)
    risk = str(u.get("risk") or RISK_MODERATE)
    lines = [f"{EMOJI.get(d, '')} **{d}** · score **{u.get('score', 0):.0f}/100** · "
             f"risk {RISK_EMOJI.get(risk, '')} {risk} · confidence {u.get('confidence', 'LOW')} · "
             f"evidence {float(u.get('completeness') or 0) * 100:.0f}%"]
    for t in (u.get("hard_vetoes") or [])[:2]:
        lines.append(f"⛔ {t}")
    for t in (u.get("warnings") or [])[:2]:
        lines.append(f"🔺 {t}")
    for t in (u.get("unresolved") or [])[:2]:
        lines.append(f"⚪ {t}")
    for t in (u.get("conflicts") or [])[:1]:
        lines.append(f"⚠️ sources disagree — {t}")
    for t in (u.get("penalty_reasons") or [])[:2]:
        lines.append(f"❔ {t}")
    for t in (u.get("positives") or [])[:max_lines]:
        lines.append(f"✅ {t}")
    note = (u.get("model") or {}).get("note")
    if note:
        lines.append(f"🧠 {note}")
    if (u.get("ai") or {}).get("used"):
        ai = u["ai"]
        lines.append(f"🤖 AI {ai.get('verdict')}"
                     + (f" {ai['score']:.0f}/100" if _num(ai.get("score")) is not None else ""))
    why = u.get("why") or []
    if why and d != STRONG:
        lines.append(f"ℹ️ {why[0]}")
    return "\n".join(lines)


def category_text(u: dict) -> str:
    rows = []
    for cat in CATEGORIES:
        c = (u.get("categories") or {}).get(cat) or {}
        pts, w = _num(c.get("points")) or 0.0, _num(c.get("weight")) or 0.0
        cov = (_num(c.get("coverage")) or 0.0) * 100
        rows.append(f"`{c.get('label', cat)[:26]:<26}` {pts:>5.1f}/{w:<4.0f} "
                    f"{'(thin: ' + f'{cov:.0f}%)' if c.get('thin') else f'({cov:.0f}%)'}")
    return "\n".join(rows)


LEGACY_ACTION = {STRONG: "AUTO_POST", WATCH: "WATCHLIST", CAUTION: "WATCHLIST",
                 REJECT: "REJECT", INSUFFICIENT: "INSUFFICIENT_DATA"}
