"""Core data types shared by every module.

Design rule: every important value travels with a status, a source and a timestamp, so that
UNKNOWN/UNAVAILABLE can never be silently read as zero risk or as a good number.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Generic, Optional, TypeVar

T = TypeVar("T")


class DataStatus(str, Enum):
    VERIFIED = "VERIFIED"        # confirmed directly from an appropriate source (e.g. on-chain)
    OBSERVED = "OBSERVED"        # returned by a source, not independently verified
    INFERRED = "INFERRED"        # estimated from multiple signals
    UNKNOWN = "UNKNOWN"          # insufficient evidence
    STALE = "STALE"              # older than the freshness threshold
    UNAVAILABLE = "UNAVAILABLE"  # provider could not supply it

    @property
    def usable(self) -> bool:
        return self in (DataStatus.VERIFIED, DataStatus.OBSERVED, DataStatus.INFERRED)


class CandidateState(str, Enum):
    DISCOVERED = "DISCOVERED"
    RESEARCHING = "RESEARCHING"
    REJECTED = "REJECTED"
    WATCHLIST = "WATCHLIST"
    QUALIFIED = "QUALIFIED"
    PUBLISHED = "PUBLISHED"
    WARNING = "WARNING"
    STALE = "STALE"
    CLOSED = "CLOSED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class Action(str, Enum):
    AUTO_POST = "AUTO_POST"
    WATCHLIST = "WATCHLIST"
    REJECT = "REJECT"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class GateStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class RiskLevel(str, Enum):
    CRITICAL = "CRITICAL"
    ELEVATED = "ELEVATED"
    MODERATE = "MODERATE"
    LOW_DETECTED = "LOW_DETECTED"
    INCOMPLETE = "INCOMPLETE"  # critical information missing — never displayed as low risk

    @property
    def label(self) -> str:
        return {
            "CRITICAL": "CRITICAL RISK",
            "ELEVATED": "ELEVATED RISK",
            "MODERATE": "MODERATE DETECTED RISK — NOT RUG-PROOF",
            "LOW_DETECTED": "LOW DETECTED RISK — NOT RUG-PROOF",
            "INCOMPLETE": "INCOMPLETE — CRITICAL DATA MISSING",
        }[self.value]


class Confidence(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

    @property
    def rank(self) -> int:
        return {"LOW": 0, "MEDIUM": 1, "HIGH": 2}[self.value]


class EvidenceStrength(str, Enum):
    CONFIRMED = "CONFIRMED"       # direct on-chain relationship
    STRONG = "STRONG"             # strongly supported inference
    WEAK = "WEAK"                 # weak correlation
    UNKNOWN = "UNKNOWN"


@dataclass
class Datum(Generic[T]):
    """A value plus provenance."""
    value: Optional[T]
    status: DataStatus
    source: str
    ts: float = field(default_factory=time.time)
    note: str = ""

    @classmethod
    def unknown(cls, source: str, note: str = "") -> "Datum[Any]":
        return cls(None, DataStatus.UNKNOWN, source, note=note)

    @classmethod
    def unavailable(cls, source: str, note: str = "") -> "Datum[Any]":
        return cls(None, DataStatus.UNAVAILABLE, source, note=note)

    @property
    def usable(self) -> bool:
        return self.value is not None and self.status.usable

    def age(self, now: Optional[float] = None) -> float:
        return (now or time.time()) - self.ts

    def freshen(self, max_age: float, now: Optional[float] = None) -> "Datum[T]":
        if self.value is not None and self.status.usable and self.age(now) > max_age:
            return Datum(self.value, DataStatus.STALE, self.source, self.ts, self.note)
        return self

    def to_dict(self) -> dict:
        return {"value": self.value, "status": self.status.value, "source": self.source,
                "ts": self.ts, "note": self.note}


@dataclass
class Flag:
    """A risk finding."""
    code: str
    severity: Severity
    message: str
    status: DataStatus = DataStatus.OBSERVED
    strength: EvidenceStrength = EvidenceStrength.UNKNOWN

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity.value, "message": self.message,
                "status": self.status.value, "strength": self.strength.value}


@dataclass
class GateResult:
    name: str
    status: GateStatus
    reason: str
    critical: bool = True  # an UNKNOWN on a critical gate blocks publication

    def to_dict(self) -> dict:
        return asdict(self) | {"status": self.status.value}


@dataclass
class PairInfo:
    pair_address: str
    dex_id: str
    base_mint: str
    base_symbol: str
    base_name: str
    quote_mint: str
    quote_symbol: str
    price_usd: Optional[float]
    price_native: Optional[float]
    liquidity_usd: Optional[float]
    fdv: Optional[float]
    market_cap: Optional[float]
    pair_created_at: Optional[float]  # unix seconds
    vol: dict[str, Optional[float]]
    txns: dict[str, dict[str, Optional[int]]]
    price_change: dict[str, Optional[float]]
    url: str = ""
    labels: list[str] = field(default_factory=list)
    image_url: Optional[str] = None
    websites: list[str] = field(default_factory=list)
    socials: list[dict] = field(default_factory=list)
    boosts_active: int = 0
    fetched_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Trade:
    signature: str
    slot: int
    block_time: Optional[int]
    wallet: str
    side: str  # "buy" | "sell"
    token_amount: float
    sol_amount: Optional[float]
    # "VERIFIED_PUMP_EVENT" only when decoded from a Pump.fun TradeEvent and confirmed by the
    # transaction's own token balances. Everything else is an unverified balance-delta guess and
    # must never be counted as a confirmed trade.
    verification: str = "UNVERIFIED_CANDIDATE"


@dataclass
class ScoreComponent:
    name: str
    points: float
    max_points: float
    evidence: list[str] = field(default_factory=list)
    penalties: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
