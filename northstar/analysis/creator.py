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
    prior_migrated: Optional[int] = None        # v2.5.9: prior launches that graduated (PumpPortal feed)
    feed_since: Optional[float] = None          # the feed has watched this creator since (unix time)
    summary: str = "UNKNOWN"
    flags: list[Flag] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "flags"} | {"status": self.status.value}


def feed_history(db, mint: str, creator: Optional[str]) -> Optional[dict]:
    """Prior pump.fun launches and graduations by `creator`, counted from the PumpPortal feed
    (every launch the bot has seen since the feed records began; a lower bound, OBSERVED).
    The coin itself is not counted as prior."""
    if not db or not creator or not hasattr(db, "creator_stats"):
        return None
    try:
        row = db.creator_stats(creator)
        if not row:
            return None
        own_launch = db.launch_creator(mint) == creator
        own_migr = own_launch and db.has_discovery_source(mint, "pumpportal_migration")
    except Exception:  # noqa: BLE001
        return None
    return {"prior_launches": max(0, int(row["launches"]) - (1 if own_launch else 0)),
            "prior_migrated": max(0, int(row["migrated"]) - (1 if own_migr else 0)),
            "since": row.get("first_ts")}


def _feed_summary(ch: "CreatorHistory") -> str:
    return (f"{ch.prior_launches} other pump.fun launch(es) by this creator seen on the launch feed, "
            f"{ch.prior_migrated} of them migrated (OBSERVED, a lower bound)")


async def analyze_creator(mint: str, creator: Optional[str], creator_source: str, rc: Optional[dict],
                          dex, rugcheck, db, max_check: int = 3) -> CreatorHistory:
    feed = feed_history(db, mint, creator)
    if feed and not isinstance((rc or {}).get("creatorTokens"), list):
        ch = CreatorHistory(DataStatus.OBSERVED, creator, creator_source, prior_launches=feed["prior_launches"],
                            prior_migrated=feed["prior_migrated"], feed_since=feed["since"])
        ch.summary = _feed_summary(ch)
        _flags(ch)
        return ch
    if not creator:
        ch = CreatorHistory(DataStatus.UNKNOWN, summary="UNKNOWN — creator could not be identified")
        _flags(ch)
        return ch
    cached = db.get_creator_cache(creator, max_age=6 * 3600) if db else None
    if cached:
        ch = CreatorHistory(DataStatus(cached["status"]), creator, creator_source,
                            prior_launches=cached.get("prior_launches"), prior_checked=cached.get("prior_checked", 0),
                            prior_inactive=cached.get("prior_inactive", 0), prior_rugged=cached.get("prior_rugged", 0),
                            prior_migrated=cached.get("prior_migrated"), feed_since=cached.get("feed_since"),
                            summary=cached["summary"])
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
    if feed:
        ch.prior_migrated, ch.feed_since = feed["prior_migrated"], feed["since"]
        ch.prior_launches = max(ch.prior_launches or 0, feed["prior_launches"])
    _flags(ch)
    if db:
        db.put_creator_cache(creator, {k: v for k, v in ch.to_dict().items()})
    return ch


def creator_gate(ch: CreatorHistory, s) -> tuple[str, str]:
    """`creator_history` gate (v2.5.9): a serial launcher, or a creator who already graduated
    several coins (a launch farm), is a risk threshold. UNKNOWN when the history is unknown."""
    if ch.status == DataStatus.UNKNOWN or ch.prior_launches is None:
        return "UNKNOWN", ch.summary
    max_l = int(getattr(s, "creator_max_prior_launches", 5) or 0)
    max_m = int(getattr(s, "creator_max_prior_migrations", 2) or 0)
    if max_l and ch.prior_launches >= max_l:
        return "FAIL", (f"creator launched {ch.prior_launches} other coins"
                        + (f" ({ch.prior_migrated} migrated)" if ch.prior_migrated else "")
                        + ": serial launcher")
    if max_m and (ch.prior_migrated or 0) >= max_m:
        return "FAIL", (f"creator already migrated {ch.prior_migrated} other coins "
                        f"(of {ch.prior_launches} launched): launch-farm pattern")
    if ch.prior_rugged:
        return "FAIL", f"{ch.prior_rugged} prior token(s) by this creator flagged rugged by RugCheck"
    return "PASS", ch.summary


def _flags(ch: CreatorHistory) -> None:
    ch.flags = []
    if ch.status == DataStatus.UNKNOWN:
        ch.flags.append(Flag("CREATOR_HISTORY_UNKNOWN", Severity.MEDIUM, ch.summary, DataStatus.UNKNOWN))
        return
    if ch.prior_rugged:
        ch.flags.append(Flag("CREATOR_PRIOR_RUGS", Severity.HIGH,
                             f"{ch.prior_rugged} prior token(s) by this creator flagged rugged by RugCheck",
                             DataStatus.OBSERVED, EvidenceStrength.STRONG))
    if (ch.prior_migrated or 0) >= 2:
        ch.flags.append(Flag("CREATOR_PRIOR_MIGRATIONS", Severity.HIGH,
                             f"creator already migrated {ch.prior_migrated} other coins (launch-farm pattern)",
                             DataStatus.OBSERVED, EvidenceStrength.WEAK))
    if (ch.prior_launches or 0) >= 10:
        ch.flags.append(Flag("SERIAL_LAUNCHER", Severity.HIGH,
                             f"creator has {ch.prior_launches} prior launches (repeat-launch pattern)",
                             DataStatus.OBSERVED, EvidenceStrength.WEAK))
    elif (ch.prior_launches or 0) >= 3 and ch.prior_inactive >= ch.prior_launches * 0.8:
        ch.flags.append(Flag("CREATOR_PRIOR_INACTIVE", Severity.MEDIUM,
                             f"{ch.prior_inactive}/{ch.prior_launches} prior launches have no meaningful liquidity "
                             "(inactive is not the same as rugged)", DataStatus.OBSERVED, EvidenceStrength.WEAK))
