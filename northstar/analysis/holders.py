"""Holder concentration (raw vs adjusted) and creator holdings.

Sources, in order of trust:
  1. RPC getTokenLargestAccounts (top 20 token accounts) + getMultipleAccounts for owners -> VERIFIED
  2. RugCheck topHolders -> OBSERVED
Only the top 20 accounts are visible through either source, so holder data is never "complete";
the long tail is unknown.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..config import Settings
from ..models import DataStatus, EvidenceStrength, Flag, GateResult, GateStatus, Severity
from ..providers.solana_rpc import BURN_ADDRESSES

INFRA_TYPES = {"amm", "pool", "dex", "cex", "exchange", "locker"}


@dataclass
class Holder:
    token_account: str
    owner: Optional[str]
    amount: float
    pct_raw: float
    excluded: Optional[str] = None  # reason if excluded from ordinary-holder math
    insider: Optional[bool] = None


@dataclass
class HolderAnalysis:
    status: DataStatus
    source: str
    holders: list[Holder] = field(default_factory=list)
    top5_raw: Optional[float] = None
    top10_raw: Optional[float] = None
    top20_raw: Optional[float] = None
    top10_adj: Optional[float] = None
    top5_adj: Optional[float] = None
    max_single_adj: Optional[float] = None
    creator_pct: Optional[float] = None
    creator_pct_status: DataStatus = DataStatus.UNKNOWN
    insider_pct: Optional[float] = None
    exclusions_verified: bool = False
    flags: list[Flag] = field(default_factory=list)
    gates: list[GateResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k not in ("holders", "flags", "gates")} | {
            "status": self.status.value, "creator_pct_status": self.creator_pct_status.value,
            "holders": [h.__dict__ for h in self.holders[:20]]}


def build_holders_from_rpc(largest: list[dict], owner_accounts: list[Optional[dict]],
                           supply_ui: float) -> list[Holder]:
    out = []
    for acc, info in zip(largest, owner_accounts + [None] * (len(largest) - len(owner_accounts))):
        try:
            amt = float(acc.get("uiAmount") if acc.get("uiAmount") is not None else acc.get("uiAmountString"))
        except (TypeError, ValueError):
            continue
        owner = None
        if info and isinstance(info.get("data"), dict):
            owner = ((info["data"].get("parsed") or {}).get("info") or {}).get("owner")
        out.append(Holder(acc.get("address", ""), owner, amt, amt / supply_ui * 100 if supply_ui else 0))
    return out


def build_holders_from_rugcheck(rc: dict) -> list[Holder]:
    out = []
    for h in rc.get("topHolders") or []:
        if not isinstance(h, dict):
            continue
        try:
            pct = float(h.get("pct") or 0)
            amt = float(h.get("uiAmount") or 0)
        except (TypeError, ValueError):
            continue
        out.append(Holder(h.get("address", ""), h.get("owner"), amt, pct, insider=h.get("insider")))
    return out


def creator_balance_pct(rc: Optional[dict], supply_ui: Optional[float]) -> Optional[float]:
    """RugCheck ``creatorBalance`` as a % of supply, or None when it cannot be converted safely.

    ``creatorBalance`` is in RAW base units (e.g. 7_264_970_000_000 = 7.26M tokens at 6 decimals),
    so it is divided by the raw supply, or converted with the token's decimals. Dividing it by the
    UI supply (the old code) inflated it by 10**decimals and produced values like "726496.9%".
    A result outside 0-100% is treated as UNKNOWN rather than shown.
    """
    if not rc:
        return None
    cb = rc.get("creatorBalance")
    if isinstance(cb, bool) or not isinstance(cb, (int, float)) or cb < 0:
        return None
    tok = rc.get("token") or {}
    pct = None
    try:
        supply_raw = int(tok["supply"])
        if supply_raw > 0:
            pct = float(cb) / supply_raw * 100
    except (KeyError, TypeError, ValueError):
        pass
    if pct is None and supply_ui:
        try:
            pct = float(cb) / 10 ** int(tok["decimals"]) / supply_ui * 100
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return None
    if pct is None or not 0 <= pct <= 100:
        return None
    return round(pct, 3)


def analyze_holders(holders: list[Holder], status: DataStatus, source: str, s: Settings, *,
                    pair_address: Optional[str], creator: Optional[str], rc: Optional[dict],
                    supply_ui: Optional[float], share_of_total: bool = False) -> HolderAnalysis:
    """Holder concentration.

    ``share_of_total=False`` (post-migration): adjusted shares are of the supply outside the pool.
    ``share_of_total=True`` (bonding curve): shares are of TOTAL supply with the curve vault excluded
    from the list - the curve still holds most of the supply, so a "circulating" denominator would
    make every early token look concentrated. This matches how Axiom/GMGN show pre-migration holders.
    """
    ha = HolderAnalysis(status=status, source=source)
    if not holders:
        ha.status = DataStatus.UNAVAILABLE
        ha.gates.append(GateResult("holder_concentration", GateStatus.UNKNOWN, "holder data unavailable"))
        ha.flags.append(Flag("HOLDERS_UNKNOWN", Severity.HIGH, "holder distribution could not be retrieved",
                             DataStatus.UNAVAILABLE))
        return ha

    rc = rc or {}
    pool_vaults = set()
    for m in rc.get("markets") or []:
        if isinstance(m, dict):
            pool_vaults.update(x for x in (m.get("liquidityA"), m.get("liquidityB")) if x)
    known = rc.get("knownAccounts") or {}

    verified_pool = False
    for h in sorted(holders, key=lambda x: -x.amount):
        if h.owner and h.owner in BURN_ADDRESSES:
            h.excluded = "burn address"
        elif pair_address and h.owner == pair_address:
            h.excluded = "pool vault (owned by pool account)"
            verified_pool = True
        elif h.token_account in pool_vaults:
            h.excluded = "pool vault (RugCheck market)"
            verified_pool = True
        else:
            k = known.get(h.owner or "") or known.get(h.token_account) or {}
            if isinstance(k, dict) and str(k.get("type", "")).lower() in INFRA_TYPES:
                h.excluded = f"infrastructure ({k.get('type')}: {k.get('name')})"
                verified_pool = verified_pool or str(k.get("type", "")).lower() in {"amm", "pool"}
        ha.holders.append(h)
    ha.exclusions_verified = verified_pool

    def top(n: int, adjusted: bool) -> float:
        rows = [h for h in ha.holders if not (adjusted and h.excluded)]
        return round(sum(h.pct_raw for h in rows[:n]), 2)

    ha.top5_raw, ha.top10_raw, ha.top20_raw = top(5, False), top(10, False), top(20, False)
    excluded_pct = sum(h.pct_raw for h in ha.holders if h.excluded)
    circ = max(100 - excluded_pct, 1e-9)
    ordinary = [h for h in ha.holders if not h.excluded]
    # adjusted = share of the non-excluded supply
    denom = 100.0 if share_of_total else circ
    ha.top10_adj = round(sum(h.pct_raw for h in ordinary[:10]) / denom * 100, 2)
    ha.top5_adj = round(sum(h.pct_raw for h in ordinary[:5]) / denom * 100, 2)
    ha.max_single_adj = round(ordinary[0].pct_raw / denom * 100, 2) if ordinary else 0.0
    if not verified_pool:
        ha.notes.append("pool vault not positively identified; adjusted figures may include the pool")
        ha.flags.append(Flag("POOL_VAULT_UNIDENTIFIED", Severity.LOW,
                             "could not verify which holder is the liquidity pool; concentration may be overstated",
                             DataStatus.UNKNOWN))
    ins = [h for h in ha.holders if h.insider and not h.excluded]
    if any(h.insider is not None for h in ha.holders):
        ha.insider_pct = round(sum(h.pct_raw for h in ins), 2)
        if ha.insider_pct > 10:
            ha.flags.append(Flag("INSIDER_HOLDERS", Severity.HIGH,
                                 f"RugCheck flags top holders holding {ha.insider_pct:.1f}% as insiders",
                                 DataStatus.OBSERVED, EvidenceStrength.STRONG))

    # creator holdings
    if creator:
        held = [h for h in ha.holders if h.owner == creator]
        if held:
            ha.creator_pct = round(sum(h.pct_raw for h in held), 2)
            ha.creator_pct_status = status
        else:
            pct = creator_balance_pct(rc, supply_ui)
            if pct is not None:
                ha.creator_pct = pct
                ha.creator_pct_status = DataStatus.OBSERVED
            else:
                # not in top 20 => below the 20th holder's share, but exact amount unknown
                ha.creator_pct = None
                ha.creator_pct_status = DataStatus.UNKNOWN
                ha.notes.append("creator not among top 20 holders; exact holding unknown")

    # gates
    reasons = []
    if ha.top10_adj > s.max_top10_adjusted_pct:
        reasons.append(f"{'top-10 holders own' if share_of_total else 'adjusted top-10 holders own'} "
                       f"{ha.top10_adj:.1f}%{' of total supply' if share_of_total else ''} "
                       f"(> {s.max_top10_adjusted_pct:.0f}%)")
    if ha.max_single_adj > s.max_single_holder_adjusted_pct:
        reasons.append(f"largest ordinary holder owns {ha.max_single_adj:.1f}%"
                       f"{' of total supply' if share_of_total else ''} "
                       f"(> {s.max_single_holder_adjusted_pct:.0f}%)")
    ha.gates.append(GateResult("holder_concentration", GateStatus.FAIL if reasons else GateStatus.PASS,
                               "; ".join(reasons) or f"adjusted top-10 {ha.top10_adj:.1f}%"))
    for r in reasons:
        ha.flags.append(Flag("HOLDER_CONCENTRATION", Severity.CRITICAL, r, status, EvidenceStrength.CONFIRMED
                             if status == DataStatus.VERIFIED else EvidenceStrength.STRONG))
    if ha.creator_pct is not None and ha.creator_pct > s.max_creator_holding_pct:
        ha.gates.append(GateResult("creator_holdings", GateStatus.FAIL,
                                   f"creator holds {ha.creator_pct:.1f}% (> {s.max_creator_holding_pct:.0f}%)"))
        ha.flags.append(Flag("CREATOR_HOLDS_LARGE", Severity.CRITICAL, f"creator holds {ha.creator_pct:.1f}%",
                             ha.creator_pct_status))
    else:
        ha.gates.append(GateResult("creator_holdings",
                                   GateStatus.PASS if ha.creator_pct is not None or creator else GateStatus.UNKNOWN,
                                   "creator holding within limit" if ha.creator_pct is not None
                                   else "creator holding unknown (not in top 20)" if creator
                                   else "creator unknown", critical=False))
    return ha
