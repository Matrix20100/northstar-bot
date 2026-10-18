"""Creator / deployer history.

Creator identity comes from RugCheck (`creator`) or the pump.fun launch event (`traderPublicKey`);
both are OBSERVED. Prior launches come from RugCheck `creatorTokens` when present. A prior token
that died is NOT treated as a rug; only RugCheck's explicit `rugged` flag counts as rug evidence,
and even that is reported as OBSERVED third-party analysis.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from ..models import DataStatus, EvidenceStrength, Flag, Severity
from ..net import ProviderError

log = logging.getLogger("northstar.creator")


@dataclass
class CreatorHistory:
    status: DataStatus
    creator: Optional[str] = None
    creator_source: str = ""
    prior_launches: Optional[int] = None
    prior_checked: int = 0
    prior_inactive: int = 0
    prior_rugged: int = 0
    summary: str = "UNKNOWN"
    flags: list[Flag] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "flags"} | {"status": self.status.value}


async def analyze_creator(mint: str, creator: Optional[str], creator_source: str, rc: Optional[dict],
                          dex, rugcheck, db, max_check: int = 3) -> CreatorHistory:
    if not creator:
        ch = CreatorHistory(DataStatus.UNKNOWN, summary="UNKNOWN — creator could not be identified")
        _flags(ch)
        return ch
    cached = db.get_creator_cache(creator, max_age=6 * 3600) if db else None
    if cached:
        ch = CreatorHistory(DataStatus(cached["status"]), creator, creator_source,
                            cached.get("prior_launches"), cached.get("prior_checked", 0),
                            cached.get("prior_inactive", 0), cached.get("prior_rugged", 0), cached["summary"])
        _flags(ch)
        return ch
    prior = (rc or {}).get("creatorTokens")
    if not isinstance(prior, list):
        ch = CreatorHistory(DataStatus.UNKNOWN, creator, creator_source,
                            summary="UNKNOWN — no launch history available for this creator")
        _flags(ch)
        return ch
    mints = [p.get("mint") for p in prior if isinstance(p, dict) and p.get("mint") and p.get("mint") != mint]
    ch = CreatorHistory(DataStatus.OBSERVED, creator, creator_source, prior_launches=len(mints))
    if mints and dex is not None:
        try:
            pairs = await dex.tokens_batch(mints[:30])
            live = {p.base_mint for p in pairs if (p.liquidity_usd or 0) >= 1000}
            ch.prior_inactive = sum(1 for m in mints[:30] if m not in live)
        except ProviderError as e:
            log.info("creator prior-token lookup failed: %s", e)
    if mints and rugcheck is not None:
        for m in mints[:max_check]:
            try:
                r = await rugcheck.summary(m)
            except ProviderError:
                continue
            ch.prior_checked += 1
            if any(str(x.get("name", "")).lower() in ("rugged", "rug pull") for x in (r.get("risks") or [])
                   if isinstance(x, dict)):
                ch.prior_rugged += 1
    if ch.prior_launches == 0:
        ch.summary = "No prior launches reported (OBSERVED via RugCheck)"
    else:
        ch.summary = (f"{ch.prior_launches} prior launch(es); {ch.prior_inactive} without meaningful liquidity "
                      f"now" + (f"; {ch.prior_rugged}/{ch.prior_checked} checked flagged rugged by RugCheck"
                                if ch.prior_checked else "") + " (OBSERVED)")
    _flags(ch)
    if db:
        db.put_creator_cache(creator, {k: v for k, v in ch.to_dict().items()})
    return ch


def _flags(ch: CreatorHistory) -> None:
    ch.flags = []
    if ch.status == DataStatus.UNKNOWN:
        ch.flags.append(Flag("CREATOR_HISTORY_UNKNOWN", Severity.MEDIUM, ch.summary, DataStatus.UNKNOWN))
        return
    if ch.prior_rugged:
        ch.flags.append(Flag("CREATOR_PRIOR_RUGS", Severity.HIGH,
                             f"{ch.prior_rugged} prior token(s) by this creator flagged rugged by RugCheck",
                             DataStatus.OBSERVED, EvidenceStrength.STRONG))
    if (ch.prior_launches or 0) >= 10:
        ch.flags.append(Flag("SERIAL_LAUNCHER", Severity.HIGH,
                             f"creator has {ch.prior_launches} prior launches (repeat-launch pattern)",
                             DataStatus.OBSERVED, EvidenceStrength.WEAK))
    elif (ch.prior_launches or 0) >= 3 and ch.prior_inactive >= ch.prior_launches * 0.8:
        ch.flags.append(Flag("CREATOR_PRIOR_INACTIVE", Severity.MEDIUM,
                             f"{ch.prior_inactive}/{ch.prior_launches} prior launches have no meaningful liquidity "
                             "(inactive is not the same as rugged)", DataStatus.OBSERVED, EvidenceStrength.WEAK))
