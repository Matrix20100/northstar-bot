"""On-chain security: authorities, Token-2022 extensions, liquidity and LP status.

This engine is independent of the opportunity score. It produces flags (findings) and gate
results. A revoked authority is never described as "safe", only as "revoked".
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from ..config import Settings
from ..models import (DataStatus, Datum, EvidenceStrength, Flag, GateResult, GateStatus, PairInfo,
                      Severity)
from .identity import MintAccount

CLMM_HINTS = ("clmm", "dlmm", "whirlpool", "concentrated")


# --------------------------------------------------------------------------- authorities
def check_authorities(mint: MintAccount, s: Settings) -> tuple[list[Flag], list[GateResult], dict]:
    flags: list[Flag] = []
    gates: list[GateResult] = []
    display: dict = {}

    for label, datum, allow, sev_msg in (
        ("mint", mint.mint_authority, s.allow_active_mint_authority,
         "active mint authority can create more supply and dilute holders"),
        ("freeze", mint.freeze_authority, s.allow_active_freeze_authority,
         "active freeze authority can freeze holders' token accounts (blocking sells)"),
    ):
        gname = f"{label}_authority"
        if datum.status != DataStatus.VERIFIED:
            display[gname] = f"UNKNOWN ({datum.status.value})"
            gates.append(GateResult(gname, GateStatus.UNKNOWN, f"{label} authority not verified on-chain"))
            continue
        if datum.value:
            display[gname] = f"ACTIVE — {datum.value[:4]}…{datum.value[-4:]} (VERIFIED)"
            flags.append(Flag(f"{label.upper()}_AUTHORITY_ACTIVE", Severity.CRITICAL if not allow else Severity.HIGH,
                              f"{sev_msg} (authority {datum.value})", DataStatus.VERIFIED,
                              EvidenceStrength.CONFIRMED))
            gates.append(GateResult(gname, GateStatus.PASS if allow else GateStatus.FAIL,
                                    f"{label} authority active: {datum.value}"))
        else:
            display[gname] = "REVOKED (VERIFIED)"
            gates.append(GateResult(gname, GateStatus.PASS, f"{label} authority revoked"))
    return flags, gates, display


# --------------------------------------------------------------------------- Token-2022
EXT_EXPLAIN = {
    "transferFeeConfig": "charges a fee on every transfer, including sells",
    "transferHook": "runs a custom program on every transfer — that program can block or alter transfers",
    "permanentDelegate": "a delegate can move or burn tokens from ANY holder's account",
    "defaultAccountState": "new token accounts may start frozen",
    "nonTransferable": "tokens cannot be transferred at all",
    "mintCloseAuthority": "the mint account can be closed by an authority",
    "pausableConfig": "an authority can pause all transfers",
    "interestBearingConfig": "displayed balance changes over time (cosmetic, no transfer impact)",
    "scaledUiAmountConfig": "displayed amounts are scaled by an authority-set multiplier",
    "confidentialTransferMint": "supports confidential transfers (amounts hidden)",
    "metadataPointer": "points to token metadata (benign)",
    "tokenMetadata": "on-mint metadata (benign)",
    "groupPointer": "token group metadata (benign)",
    "groupMemberPointer": "token group membership (benign)",
    "tokenGroup": "token group (benign)",
    "tokenGroupMember": "token group member (benign)",
}
BENIGN = {"metadataPointer", "tokenMetadata", "groupPointer", "groupMemberPointer", "tokenGroup",
          "tokenGroupMember", "interestBearingConfig"}


def check_extensions(mint: MintAccount, s: Settings) -> tuple[list[Flag], list[GateResult], str]:
    flags: list[Flag] = []
    if mint.extensions.status != DataStatus.VERIFIED:
        return flags, [GateResult("token_extensions", GateStatus.UNKNOWN, "extensions not verified")], "UNKNOWN"
    if not mint.is_token_2022:
        return flags, [GateResult("token_extensions", GateStatus.PASS, "classic SPL token, no extensions")], \
            "None (classic SPL Token program)"
    exts = mint.extensions.value or []
    names: list[str] = []
    fail_reasons: list[str] = []
    for e in exts:
        if not isinstance(e, dict):
            continue
        name = e.get("extension", "unknown")
        state = e.get("state") or {}
        names.append(name)
        if name in BENIGN:
            continue
        explain = EXT_EXPLAIN.get(name, "unrecognized extension — impact not evaluated")
        if name == "transferFeeConfig":
            bps = _fee_bps(state)
            if bps is None:
                flags.append(Flag("TRANSFER_FEE_UNKNOWN", Severity.HIGH, "transfer fee present, rate unreadable",
                                  DataStatus.VERIFIED))
                fail_reasons.append("unreadable transfer fee")
            elif bps > s.max_transfer_fee_bps:
                flags.append(Flag("TRANSFER_FEE_HIGH", Severity.CRITICAL, f"transfer fee {bps/100:.2f}% {explain}",
                                  DataStatus.VERIFIED, EvidenceStrength.CONFIRMED))
                fail_reasons.append(f"transfer fee {bps/100:.2f}%")
            elif bps > 0:
                flags.append(Flag("TRANSFER_FEE", Severity.MEDIUM, f"transfer fee {bps/100:.2f}%", DataStatus.VERIFIED))
            if state.get("transferFeeConfigAuthority"):
                flags.append(Flag("TRANSFER_FEE_MUTABLE", Severity.HIGH,
                                  "transfer-fee authority is set — the fee can be raised later", DataStatus.VERIFIED))
        elif name == "transferHook":
            prog = state.get("programId")
            if prog:
                flags.append(Flag("TRANSFER_HOOK", Severity.CRITICAL, f"{explain} (program {prog})",
                                  DataStatus.VERIFIED, EvidenceStrength.CONFIRMED))
                fail_reasons.append("transfer hook program set")
            elif state.get("authority"):
                flags.append(Flag("TRANSFER_HOOK_AUTHORITY", Severity.HIGH,
                                  "no hook program now, but an authority can add one", DataStatus.VERIFIED))
        elif name == "permanentDelegate":
            if state.get("delegate"):
                flags.append(Flag("PERMANENT_DELEGATE", Severity.CRITICAL, f"{explain} ({state.get('delegate')})",
                                  DataStatus.VERIFIED, EvidenceStrength.CONFIRMED))
                fail_reasons.append("permanent delegate")
        elif name == "defaultAccountState":
            if str(state.get("accountState", "")).lower() == "frozen":
                flags.append(Flag("DEFAULT_FROZEN", Severity.CRITICAL, explain, DataStatus.VERIFIED,
                                  EvidenceStrength.CONFIRMED))
                fail_reasons.append("default account state frozen")
        elif name == "nonTransferable":
            flags.append(Flag("NON_TRANSFERABLE", Severity.CRITICAL, explain, DataStatus.VERIFIED,
                              EvidenceStrength.CONFIRMED))
            fail_reasons.append("non-transferable")
        elif name == "pausableConfig":
            if state.get("paused"):
                fail_reasons.append("transfers paused")
                flags.append(Flag("PAUSED", Severity.CRITICAL, "transfers currently paused", DataStatus.VERIFIED))
            elif state.get("authority"):
                flags.append(Flag("PAUSABLE", Severity.HIGH, explain, DataStatus.VERIFIED))
        elif name == "mintCloseAuthority":
            if state.get("closeAuthority"):
                flags.append(Flag("MINT_CLOSE_AUTHORITY", Severity.MEDIUM, explain, DataStatus.VERIFIED))
        else:
            flags.append(Flag(f"EXT_{name}", Severity.MEDIUM, f"{name}: {explain}", DataStatus.VERIFIED))
    summary = "Token-2022: " + (", ".join(names) if names else "no extensions")
    gate = GateResult("token_extensions", GateStatus.FAIL if fail_reasons else GateStatus.PASS,
                      "; ".join(fail_reasons) or "no extension that restricts ordinary holders")
    return flags, [gate], summary


def _fee_bps(state: dict) -> Optional[int]:
    """Worst case of the current and scheduled fee (the newer fee takes effect at an epoch)."""
    vals = []
    for key in ("newerTransferFee", "olderTransferFee"):
        fee = state.get(key) or {}
        if "transferFeeBasisPoints" in fee:
            try:
                vals.append(int(fee["transferFeeBasisPoints"]))
            except (TypeError, ValueError):
                return None
    return max(vals) if vals else None


# --------------------------------------------------------------------------- liquidity / LP
@dataclass
class LiquidityAssessment:
    liquidity_usd: Datum[float]
    liq_to_mcap: Optional[float]
    liq_to_fdv: Optional[float]
    lp_status: Datum[str]
    lp_locked_pct: Optional[float] = None
    pool_age_hours: Optional[float] = None
    max_recent_liquidity: Optional[float] = None
    drop_from_recent_max_pct: Optional[float] = None
    vol_to_liq_h1: Optional[float] = None
    flags: list[Flag] = field(default_factory=list)
    gates: list[GateResult] = field(default_factory=list)


def assess_liquidity(pair: PairInfo, rugcheck: Optional[dict], history: list[dict], s: Settings,
                     now: Optional[float] = None) -> LiquidityAssessment:
    now = now or time.time()
    flags: list[Flag] = []
    gates: list[GateResult] = []
    liq = pair.liquidity_usd
    liq_d: Datum[float] = Datum(liq, DataStatus.OBSERVED, "dexscreener", pair.fetched_at) if liq else \
        Datum.unavailable("dexscreener", "no liquidity field")
    mcap = pair.market_cap
    liq_mcap = (liq / mcap) if liq and mcap else None
    liq_fdv = (liq / pair.fdv) if liq and pair.fdv else None
    age_h = (now - pair.pair_created_at) / 3600 if pair.pair_created_at else None
    vol_liq = (pair.vol.get("h1") or 0) / liq if liq else None

    # liquidity floor scales with size: either absolute minimum or ratio of market cap
    if not liq:
        gates.append(GateResult("liquidity", GateStatus.UNKNOWN, "liquidity unavailable"))
    else:
        ok_abs = liq >= s.min_liquidity_usd
        ok_ratio = liq_mcap is None or liq_mcap >= s.min_liquidity_to_mcap
        reason = f"liquidity ${liq:,.0f}" + (f", {liq_mcap*100:.1f}% of market cap" if liq_mcap else "")
        if not ok_abs:
            reason += f" < minimum ${s.min_liquidity_usd:,.0f}"
        if not ok_ratio:
            reason += f" (ratio below {s.min_liquidity_to_mcap*100:.0f}%)"
        gates.append(GateResult("liquidity", GateStatus.PASS if ok_abs and ok_ratio else GateStatus.FAIL, reason))
        if liq_mcap is None:
            flags.append(Flag("MCAP_UNAVAILABLE", Severity.LOW, "market cap not provided; liquidity ratio unknown",
                              DataStatus.UNAVAILABLE))

    # implausible volume vs liquidity
    if vol_liq is not None and vol_liq > 8:
        flags.append(Flag("VOLUME_LIQUIDITY_IMPLAUSIBLE", Severity.HIGH,
                          f"1h volume is {vol_liq:.1f}x liquidity — possible wash trading", DataStatus.INFERRED,
                          EvidenceStrength.WEAK))

    # liquidity removal: compare to the highest liquidity we observed for this pool recently
    pool_hist = [h for h in history if h.get("pair_address") == pair.pair_address and h.get("liquidity_usd")]
    max_recent = max((h["liquidity_usd"] for h in pool_hist), default=None)
    drop = None
    if max_recent and liq:
        drop = (1 - liq / max_recent) * 100
        if drop >= s.liquidity_drop_reject_pct:
            pc = pair.price_change.get("h1")
            flags.append(Flag("LIQUIDITY_REMOVAL", Severity.CRITICAL,
                              f"liquidity fell {drop:.0f}% from recent ${max_recent:,.0f} to ${liq:,.0f}"
                              + (f" (price {pc:+.0f}% 1h)" if pc is not None else ""),
                              DataStatus.OBSERVED, EvidenceStrength.STRONG))
            gates.append(GateResult("liquidity_removal", GateStatus.FAIL,
                                    f"liquidity down {drop:.0f}% from recent observed maximum"))
        else:
            gates.append(GateResult("liquidity_removal", GateStatus.PASS,
                                    f"no liquidity drop beyond {s.liquidity_drop_reject_pct:.0f}% observed",
                                    critical=False))
    else:
        gates.append(GateResult("liquidity_removal", GateStatus.PASS,
                                "no prior liquidity observations for this pool (first look)", critical=False))

    lp_status, lp_pct = _lp_status(pair, rugcheck, flags)
    if s.require_lp_lock_verified:
        gates.append(lp_gate(lp_status.value, lp_pct, liq, s.min_liquidity_usd))
    return LiquidityAssessment(liq_d, liq_mcap, liq_fdv, lp_status, lp_pct, age_h, max_recent, drop, vol_liq,
                               flags, gates)


def lp_gate(lp_text: Optional[str], lp_pct: Optional[float], liq: Optional[float],
            min_liquidity: float) -> GateResult:
    """The lp_lock gate for ONE pool, keeping PASS / FAIL / UNKNOWN semantically distinct.

    v2.4 returned UNKNOWN for anything that was not fully locked — including a MEASURED partial
    lock and a concentrated-liquidity pool whose withdrawability is a known fact. UNKNOWN on
    lp_lock means INSUFFICIENT DATA, so those coins were re-researched forever and never judged.

      PASS     LP burned/locked (>= 95%) or still on the bonding curve
      FAIL     measured: only part of this pool's LP is burned/locked — whoever holds the rest
               can pull it. (The token's ONLY pool, usually held by the deployer: a real trap.)
      UNKNOWN  not measured (RugCheck down, pool not indexed), or a concentrated-liquidity pool
               whose verdict depends on the token's other pools — see resolve_lp_floor
    """
    text = lp_text or "unknown"
    if lp_text and lp_text.startswith(("LOCKED", "BURNED", "BONDING")):
        return GateResult("lp_lock", GateStatus.PASS, text)
    if lp_text and lp_text.startswith("PARTIAL") and lp_pct is not None:
        locked = f" (about ${liq * lp_pct / 100.0:,.0f} of ${liq:,.0f})" if liq else ""
        return GateResult("lp_lock", GateStatus.FAIL,
                          f"{text}{locked} — the unlocked LP can be withdrawn by its holder")
    return GateResult("lp_lock", GateStatus.UNKNOWN, text)


LOCKED_KINDS = ("BURNED", "LOCKED", "BONDING")


def resolve_lp_floor(pools: list[dict], min_liquidity: float) -> dict:
    """LP safety across ALL of a token's pools, not just the deepest one.

    A graduated pump.fun coin keeps its burned PumpSwap pool, but third parties often add
    Meteora DLMM / CLMM positions that end up deeper. v2.4 judged only the deepest pool, so a
    coin like $DOPE ($405K in a DLMM pool, $237K in a burned PumpSwap pool) had "unknown" LP and
    was stuck at INSUFFICIENT DATA. The question that matters for an exit is: how much liquidity
    can nobody pull?

    pools: [{address, dex, kind, liquidity_usd, locked_pct, verified}] where kind is one of
    BURNED / LOCKED / BONDING / PARTIAL / CONCENTRATED / UNKNOWN.
    Returns {status: PASS|FAIL|UNKNOWN, reason, total_usd, locked_usd, unknown_usd,
             withdrawable_usd, withdrawable_share, pools}.
    """
    total = locked = unknown = 0.0
    rows = []
    for p in pools:
        liq = float(p.get("liquidity_usd") or 0.0)
        if liq <= 0:
            continue
        kind = str(p.get("kind") or "UNKNOWN")
        pct = p.get("locked_pct")
        total += liq
        if kind in LOCKED_KINDS:
            part = liq * (float(pct) / 100.0 if pct is not None else 1.0)
            locked += part
        elif kind == "PARTIAL" and pct is not None:
            part = liq * float(pct) / 100.0
            locked += part
        elif kind == "CONCENTRATED":
            part = 0.0
        else:
            unknown += liq
            part = None
        rows.append({**p, "locked_usd": None if part is None else round(part, 2)})
    withdrawable = max(0.0, total - locked - unknown)
    share = (withdrawable / total) if total else None
    money = lambda v: f"${v:,.0f}"  # noqa: E731
    if locked >= min_liquidity:
        status = "PASS"
        reason = (f"LOCKED FLOOR — {money(locked)} of {money(total)} liquidity is burned/locked "
                  + ("(verified on-chain)" if any(r.get("verified") and r.get("kind") in LOCKED_KINDS
                                                  for r in rows) else "(provider-reported)")
                  + (f"; {share * 100:.0f}% sits in positions their owners can withdraw" if share else ""))
    elif locked + unknown >= min_liquidity:
        status = "UNKNOWN"
        reason = (f"{money(locked)} of {money(total)} liquidity is verified burned/locked and "
                  f"{money(unknown)} could not be checked — cannot tell whether the "
                  f"{money(min_liquidity)} floor is locked")
    else:
        status = "FAIL"
        reason = (f"only {money(locked)} of {money(total)} liquidity is burned/locked "
                  f"(floor {money(min_liquidity)}); {money(withdrawable)} can be withdrawn by "
                  "its owners at any time"
                  + (f" and {money(unknown)} could not be checked (not enough even if it were locked)"
                     if unknown else ""))
    return {"status": status, "reason": reason, "total_usd": round(total, 2),
            "locked_usd": round(locked, 2), "unknown_usd": round(unknown, 2),
            "withdrawable_usd": round(withdrawable, 2),
            "withdrawable_share": None if share is None else round(share, 4), "pools": rows}


def pool_kind(pair: PairInfo, rc: Optional[dict]) -> tuple[str, Optional[float]]:
    """What kind of LP a pool has, from what DexScreener/RugCheck say (no RPC)."""
    dex = (pair.dex_id or "").lower()
    labels = " ".join(pair.labels or []).lower()
    if dex == "pumpfun":
        return "BONDING", None
    if any(h in labels or h in dex for h in CLMM_HINTS):
        return "CONCENTRATED", 0.0
    for m in (rc or {}).get("markets") or []:
        if isinstance(m, dict) and m.get("pubkey") == pair.pair_address:
            try:
                pct = float((m.get("lp") or {}).get("lpLockedPct"))
            except (TypeError, ValueError):
                return "UNKNOWN", None
            return ("LOCKED" if pct >= 95 else "PARTIAL"), pct
    return "UNKNOWN", None


def _lp_status(pair: PairInfo, rc: Optional[dict], flags: list[Flag]) -> tuple[Datum[str], Optional[float]]:
    dex = (pair.dex_id or "").lower()
    labels = " ".join(pair.labels).lower()
    if dex == "pumpfun":
        return Datum("BONDING CURVE — liquidity held by the pump.fun program, no LP token (OBSERVED)",
                     DataStatus.OBSERVED, "dexscreener"), None
    if any(h in labels or h in dex for h in CLMM_HINTS):
        flags.append(Flag("CONCENTRATED_LIQUIDITY", Severity.MEDIUM,
                          "concentrated-liquidity pool: positions are owner-withdrawable NFTs/accounts, "
                          "not a lockable or burnable LP token", DataStatus.OBSERVED))
        return Datum("CONCENTRATED POSITION — no fungible LP token; withdrawable by position owner",
                     DataStatus.OBSERVED, "dexscreener"), None
    if not rc:
        flags.append(Flag("LP_STATUS_UNKNOWN", Severity.MEDIUM, "LP lock/burn status unavailable",
                          DataStatus.UNAVAILABLE))
        return Datum.unavailable("rugcheck", "report unavailable"), None
    market = None
    for m in rc.get("markets") or []:
        if isinstance(m, dict) and m.get("pubkey") == pair.pair_address:
            market = m
            break
    if market is None:
        flags.append(Flag("LP_STATUS_UNKNOWN", Severity.MEDIUM,
                          "RugCheck has no market entry for the selected pool", DataStatus.UNKNOWN))
        return Datum.unknown("rugcheck", "selected pool not in RugCheck markets"), None
    lp = market.get("lp") or {}
    try:
        pct = float(lp.get("lpLockedPct")) if lp.get("lpLockedPct") is not None else None
    except (TypeError, ValueError):
        pct = None
    if pct is None:
        flags.append(Flag("LP_STATUS_UNKNOWN", Severity.MEDIUM, "LP locked % not reported", DataStatus.UNKNOWN))
        return Datum.unknown("rugcheck"), None
    lockers = rc.get("lockers") or {}
    if pct >= 95:
        kind = "LOCKED in a locker program" if lockers else "BURNED or LOCKED (RugCheck reports locked supply)"
        if lockers:
            unlocks = [v.get("unlockDate") for v in lockers.values() if isinstance(v, dict) and v.get("unlockDate")]
            if unlocks:
                soonest = min(unlocks)
                days = (soonest - time.time()) / 86400
                kind += f"; earliest unlock in {days:.1f} days"
                if days < 7:
                    flags.append(Flag("LP_UNLOCK_SOON", Severity.HIGH, f"LP unlocks in {days:.1f} days",
                                      DataStatus.OBSERVED))
        return Datum(f"{kind} — {pct:.0f}% (OBSERVED via RugCheck)", DataStatus.OBSERVED, "rugcheck"), pct
    sev = Severity.HIGH if pct < 50 else Severity.MEDIUM
    flags.append(Flag("LP_NOT_LOCKED", sev, f"only {pct:.0f}% of LP reported locked/burned — "
                      "remaining liquidity is withdrawable", DataStatus.OBSERVED))
    return Datum(f"PARTIAL — {pct:.0f}% locked/burned (OBSERVED via RugCheck)", DataStatus.OBSERVED,
                 "rugcheck"), pct


# --------------------------------------------------------------------------- RugCheck cross-checks
def rugcheck_findings(rc: Optional[dict], s: Settings, price_usd: Optional[float],
                      supply_ui: Optional[float]) -> tuple[list[Flag], list[GateResult]]:
    flags: list[Flag] = []
    gates: list[GateResult] = []
    if not rc:
        gates.append(GateResult("rugcheck", GateStatus.UNKNOWN, "RugCheck report unavailable", critical=False))
        return flags, gates
    if rc.get("rugged") is True:
        flags.append(Flag("RUGCHECK_RUGGED", Severity.CRITICAL, "RugCheck marks this token as rugged",
                          DataStatus.OBSERVED, EvidenceStrength.STRONG))
        gates.append(GateResult("rugcheck_rugged", GateStatus.FAIL, "RugCheck: rugged=true"))
    dangers = []
    for r in rc.get("risks") or []:
        if not isinstance(r, dict):
            continue
        level = str(r.get("level", "")).lower()
        msg = f"RugCheck: {r.get('name')}" + (f" — {r.get('description')}" if r.get("description") else "")
        if level == "danger":
            dangers.append(r.get("name"))
            flags.append(Flag("RUGCHECK_DANGER", Severity.HIGH, msg[:200], DataStatus.OBSERVED))
        elif level == "warn":
            flags.append(Flag("RUGCHECK_WARN", Severity.LOW, msg[:200], DataStatus.OBSERVED))
    if dangers and s.reject_on_rugcheck_danger:
        gates.append(GateResult("rugcheck_danger", GateStatus.FAIL, "RugCheck danger: " + ", ".join(map(str, dangers))))
    else:
        gates.append(GateResult("rugcheck_danger", GateStatus.PASS, "no RugCheck danger-level risks", critical=False))
    # cross-source discrepancies
    try:
        rc_price = float(rc.get("price")) if rc.get("price") else None
    except (TypeError, ValueError):
        rc_price = None
    if rc_price and price_usd:
        dev = abs(rc_price / price_usd - 1) * 100
        if dev > s.max_source_price_discrepancy_pct:
            flags.append(Flag("PRICE_DISCREPANCY", Severity.HIGH,
                              f"DexScreener and RugCheck prices differ by {dev:.0f}%", DataStatus.OBSERVED))
            # Two providers disagreeing is a confidence problem, not proof the coin is unsafe
            # (RugCheck's price is often a cached value from an earlier pool). v2.4 FAILED a
            # critical gate here, which rejected the coin before any analysis ran. It is now
            # recorded as UNKNOWN (non-critical) and as a contradiction in the unified engine.
            gates.append(GateResult("source_consistency", GateStatus.UNKNOWN,
                                    f"price sources disagree by {dev:.0f}%", critical=False))
    tok = rc.get("token") or {}
    try:
        rc_supply = int(tok.get("supply")) / 10 ** int(tok.get("decimals"))
    except (TypeError, ValueError):
        rc_supply = None
    if rc_supply and supply_ui and abs(rc_supply / supply_ui - 1) > 0.05:
        flags.append(Flag("SUPPLY_DISCREPANCY", Severity.MEDIUM,
                          "RugCheck supply differs from on-chain supply by >5% (supply may have changed)",
                          DataStatus.OBSERVED))
    return flags, gates
