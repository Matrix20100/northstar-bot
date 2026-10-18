"""Transparent 100-point opportunity score + separate risk assessment.

Weights are configurable starting assumptions, not fitted coefficients. Unavailable data earns
zero points in its component and is listed under `missing`. The risk level is derived only from
security findings and is never offset by a high opportunity score.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..config import Settings
from ..models import (Confidence, DataStatus, Flag, GateResult, GateStatus, PairInfo, RiskLevel,
                      ScoreComponent, Severity)
from .creator import CreatorHistory
from .holders import HolderAnalysis
from .market import MarketStructure
from .security import LiquidityAssessment
from .wallets import ClusterResult, TradeStats


@dataclass
class ScoreResult:
    total: float
    components: list[ScoreComponent]
    confidence: Confidence
    completeness: float
    could_be_wrong: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"total": self.total, "components": [c.to_dict() for c in self.components],
                "confidence": self.confidence.value, "completeness": self.completeness,
                "could_be_wrong": self.could_be_wrong}


def _scale(pts: float, maxpts: float, weight: float) -> float:
    return round(max(0.0, min(pts, maxpts)) / maxpts * weight, 2)


def score_demand(pair: PairInfo, ts: TradeStats, flags: list[Flag], w: float) -> ScoreComponent:
    c = ScoreComponent("demand", 0, w)
    pts = 0.0
    if ts.status == DataStatus.VERIFIED and ts.sample_size >= 15:
        span = f" over {ts.span_minutes:.0f} min" if ts.span_minutes else ""
        diversity = ts.unique_buyers / ts.buys if ts.buys else 0
        pts += 6 * min(diversity / 0.7, 1)
        c.evidence.append(f"{ts.unique_buyers} unique buyers / {ts.buys} buys in a {ts.sample_size}-swap "
                          f"on-chain sample{span}")
        pts += 5 if ts.unique_buyers >= 60 else 3.5 if ts.unique_buyers >= 30 else 2 if ts.unique_buyers >= 15 else 0
        if ts.buyer_trend == "rising":
            pts += 4
            c.evidence.append(f"unique buyers rising ({ts.first_half_buyers} -> {ts.second_half_buyers})")
        elif ts.buyer_trend == "flat":
            pts += 2
        elif ts.buyer_trend == "declining":
            c.penalties.append(f"unique buyers declining ({ts.first_half_buyers} -> {ts.second_half_buyers})")
        else:
            c.missing.append("buyer trend not measurable (sample too short)")
        if ts.unique_buyers > ts.unique_sellers:
            pts += 2
        if ts.top5_buyer_share is not None:
            pts += 3 if ts.top5_buyer_share < 40 else 1.5 if ts.top5_buyer_share < 60 else 0
            if ts.top5_buyer_share >= 60:
                c.penalties.append(f"top 5 buyers = {ts.top5_buyer_share:.0f}% of buy volume")
        maxpts = 20
    else:
        c.missing.append("on-chain trade sample unavailable — unique wallets unknown")
        b, se = pair.txns["h1"]["buys"], pair.txns["h1"]["sells"]
        if b is not None:
            pts += 2 if b >= 100 else 1 if b >= 40 else 0
            c.evidence.append(f"DexScreener: {b} buys / {se} sells in 1h (transaction counts, not unique wallets)")
        maxpts = 20  # same denominator: unavailable data earns nothing
    for f in flags:
        if f.code.startswith(("POSSIBLE_", "TXN_COUNT_VS_UNIQUE", "BUYING_CONCENTRATED")):
            pts -= 3
            c.penalties.append(f.message)
    c.points = _scale(pts, maxpts, w)
    return c


def score_liquidity(pair: PairInfo, la: LiquidityAssessment, s: Settings, w: float) -> ScoreComponent:
    c = ScoreComponent("liquidity", 0, w)
    pts = 0.0
    liq = la.liquidity_usd.value
    if liq:
        ratio = math.log10(max(liq, 1) / s.min_liquidity_usd) / 1.0  # 0 at min, 1 at 10x min
        pts += 7 * max(0.0, min(ratio, 1))
        c.evidence.append(f"liquidity ${liq:,.0f} ({la.liquidity_usd.status.value})")
    else:
        c.missing.append("liquidity unavailable")
    if la.liq_to_mcap is not None:
        r = la.liq_to_mcap
        pts += 6 if r >= 0.2 else 4 if r >= 0.12 else 2 if r >= s.min_liquidity_to_mcap else 0
        c.evidence.append(f"liquidity/market cap {r*100:.1f}%")
    else:
        c.missing.append("market cap unavailable; liquidity ratio unknown")
    if la.vol_to_liq_h1 is not None:
        v = la.vol_to_liq_h1
        pts += 4 if 0.2 <= v <= 3 else 2 if 3 < v <= 8 else 1 if v < 0.2 else 0
        if v > 8:
            c.penalties.append(f"1h volume {v:.1f}x liquidity (implausible)")
    lp = la.lp_status.value or ""
    if lp.startswith(("BURNED", "LOCKED", "BONDING")):
        pts += 3
    elif lp.startswith(("PARTIAL", "CONCENTRATED")):
        pts += 1
        c.penalties.append(lp.split(" — ")[0].lower() + " liquidity")
    else:
        c.missing.append("LP lock/burn status unknown")
    c.points = _scale(pts, 20, w)
    return c


def score_holders(ha: HolderAnalysis, ch: CreatorHistory, w: float) -> ScoreComponent:
    c = ScoreComponent("holders", 0, w)
    pts = 0.0
    if ha.top10_adj is not None and ha.status.usable:
        t = ha.top10_adj
        pts += 7 if t <= 15 else 5 if t <= 25 else 3 if t <= 35 else 1
        c.evidence.append(f"adjusted top-10 {t:.1f}% (raw {ha.top10_raw:.1f}%, {ha.status.value} via {ha.source})")
    else:
        c.missing.append("holder distribution unavailable")
    if ha.creator_pct is not None:
        pts += 4 if ha.creator_pct <= 1 else 2 if ha.creator_pct <= 5 else 0
        c.evidence.append(f"creator holds {ha.creator_pct:.2f}% ({ha.creator_pct_status.value})")
    elif ch.creator and "not among top 20" in " ".join(ha.notes):
        pts += 3
        c.evidence.append("creator not among top-20 holders")
    else:
        c.missing.append("creator holding unknown")
    if ch.status == DataStatus.UNKNOWN:
        c.missing.append("creator history unknown")
    elif ch.prior_rugged or any(f.code == "SERIAL_LAUNCHER" for f in ch.flags):
        c.penalties.append(ch.summary)
    elif ch.flags:
        pts += 2
    else:
        pts += 4
        c.evidence.append(f"creator history: {ch.summary}")
    c.points = _scale(pts, 15, w)
    return c


def score_momentum(pair: PairInfo, ms: MarketStructure, w: float) -> ScoreComponent:
    c = ScoreComponent("momentum", 0, w)
    pts = 0.0
    pc = pair.price_change
    if ms.status == DataStatus.UNAVAILABLE:
        c.missing.append("price change data unavailable")
    else:
        c.evidence.append("price change 5m/1h/6h: " + ", ".join(
            f"{pc[k]:+.0f}%" if pc.get(k) is not None else "n/a" for k in ("m5", "h1", "h6")))
        if ms.constructive:
            pts += 4
        if ms.consolidating or (ms.pullback_pct_from_high is not None and 10 <= ms.pullback_pct_from_high <= 40):
            pts += 3
            c.evidence.append("healthy pullback/consolidation observed in Northstar snapshots")
        elif ms.consolidating is None:
            c.missing.append("pullback/consolidation not assessed (thin snapshot history; no candle data)")
        if ms.volume_sustained:
            pts += 3
        elif ms.volume_sustained is False:
            c.penalties.append("1h volume below the 6h hourly average")
        if ms.buy_sell_ratio_h1 is not None:
            c.evidence.append(f"1h transaction buy/sell ratio: {ms.buy_sell_ratio_h1:.2f} (counts, not unique wallets)")
            if ms.buy_sell_ratio_h1 < 0.8:
                pts -= 2
                c.penalties.append("1h transaction counts are materially sell-dominant")
            elif ms.buy_sell_ratio_h1 >= 1.25:
                pts += 1
        if ms.observed_drawdown_pct is not None:
            c.evidence.append(f"Northstar-observed snapshot-path maximum drawdown: {ms.observed_drawdown_pct:.1f}%")
            if ms.observed_drawdown_pct >= 35:
                pts -= 2
                c.penalties.append("large drawdown observed in stored price snapshots")
        if ms.volume_acceleration is not None and ms.volume_acceleration > 8:
            pts -= 1
            c.penalties.append(f"1h volume is {ms.volume_acceleration:.1f}x the 6h hourly average; possible event spike")
        if ms.mcap_unsupported:
            pts -= 2
            c.penalties.append("market cap expansion is not supported by comparable liquidity growth")
        if ms.extended:
            pts = min(pts, 2)
            c.penalties.append("already extended")
        if ms.fading or ms.vertical_thin:
            pts = min(pts, 1)
            c.penalties.append("fading after pump" if ms.fading else "vertical move on thin liquidity")
        if ms.entry_quality == "POOR":
            pts = min(pts, 1)
        if ms.entry_quality == "INSUFFICIENT_HISTORY":
            c.missing.append("entry structure not confirmed from sufficient stored observations")
    c.points = _scale(pts, 10, w)
    return c


def score_wallet_quality(cr: ClusterResult, w: float) -> ScoreComponent:
    c = ScoreComponent("wallet_quality", 0, w)
    pts = 0.0
    if cr.fresh_wallet_share is not None:
        f = cr.fresh_wallet_share
        pts += 3 if f <= 20 else 2 if f <= 40 else 1 if f <= 60 else 0
        c.evidence.append(f"{f:.0f}% of {cr.wallets_aged} traced wallets are fresh (not a 'smart money' claim)")
    else:
        c.missing.append("wallet ages not traced")
    if cr.max_cluster_supply_pct is not None and cr.status != DataStatus.UNAVAILABLE:
        pts += 2 if cr.max_cluster_supply_pct < 5 else 1 if cr.max_cluster_supply_pct < 10 else 0
    else:
        c.missing.append("wallet-cluster analysis unavailable")
    c.points = _scale(pts, 5, w)
    return c


def evidence_completeness(fields: dict[str, bool]) -> float:
    return round(sum(fields.values()) / len(fields), 2) if fields else 0.0


def build_score(components: list[ScoreComponent], completeness: float, narrative_conf: Confidence,
                w_evidence: float, caveats: list[str]) -> ScoreResult:
    ev = ScoreComponent("evidence", round(completeness * w_evidence, 2), w_evidence,
                        [f"{completeness*100:.0f}% of critical fields available and fresh"])
    comps = components + [ev]
    total = round(sum(c.points for c in comps), 1)
    if completeness >= 0.8 and narrative_conf.rank >= 1:
        conf = Confidence.HIGH
    elif completeness >= 0.6:
        conf = Confidence.MEDIUM
    else:
        conf = Confidence.LOW
    if narrative_conf == Confidence.LOW and conf == Confidence.HIGH:
        conf = Confidence.MEDIUM
    return ScoreResult(total, comps, conf, completeness, caveats)


def assess_risk(flags: list[Flag], gates: list[GateResult]) -> RiskLevel:
    usable = [f for f in flags if f.status != DataStatus.UNKNOWN or f.severity == Severity.CRITICAL]
    if any(f.severity == Severity.CRITICAL for f in usable) or any(g.status == GateStatus.FAIL for g in gates):
        return RiskLevel.CRITICAL
    highs = sum(1 for f in usable if f.severity == Severity.HIGH)
    meds = sum(1 for f in usable if f.severity == Severity.MEDIUM)
    if highs >= 2:
        return RiskLevel.ELEVATED
    if any(g.status == GateStatus.UNKNOWN and g.critical for g in gates):
        return RiskLevel.INCOMPLETE
    if highs == 1 or meds >= 3:
        return RiskLevel.MODERATE
    return RiskLevel.LOW_DETECTED
