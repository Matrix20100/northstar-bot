"""Who bought at the very start of a pump.fun coin (v2.5.9, operator request 22 Oct).

"If a coin has a massive jump in buys at the very start, 9 times out of 10 it's a rug." The
launch-shape check (launch.py) reads price candles; this reads the actual first trades.

Source: the coin's pump.fun bonding curve (a PDA of the mint). Its signature history holds every
curve trade from the create transaction on, also after migration (the curve then goes quiet).
Signatures come newest-first, so the history is paged back to the create transaction; when it
cannot be reached within `max_pages` pages the result is INCOMPLETE and nothing is judged.
The first `first_n` successful transactions are decoded with the verified Pump.fun TradeEvent
decoder (pump_trades.py): only event-confirmed trades count.

Measured (all % of total supply):
  dev_buy_pct     the creator's buys in the opening window
  bundle_pct      buys by OTHER wallets in the create slot and the next slot (bundled launch)
  early_buy_pct   buys by other wallets within `early_seconds` of the create transaction (snipers)
  early_wallets   distinct wallets in that window, and their addresses (for the holdings check)
  early_price_x   curve price at the end of the window / price of the first trade

Gate `launch_buys` (a risk threshold: FAIL rejects the coin):
  FAIL     bundle_pct >= launch_max_bundle_pct, or early_buy_pct >= launch_max_early_buy_pct,
           or dev+early >= launch_max_opening_pct
  PASS     history reached the create transaction and none of the above
  UNKNOWN  history not reachable, RPC failure, or too few decodable trades
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from typing import Optional

from .pump_trades import decode_curve_transaction

log = logging.getLogger("northstar.launch_trades")

PUMP_SUPPLY = 1_000_000_000.0


@dataclass
class LaunchBuys:
    status: str = "UNAVAILABLE"            # OBSERVED / INCOMPLETE / UNAVAILABLE
    curve: Optional[str] = None
    pages: int = 0
    signatures_seen: int = 0
    reached_create: bool = False
    created_at: Optional[float] = None
    create_slot: Optional[int] = None
    trades_decoded: int = 0
    dev_buy_pct: Optional[float] = None
    bundle_pct: Optional[float] = None
    bundle_wallets: int = 0
    early_buy_pct: Optional[float] = None
    early_wallets: int = 0
    early_price_x: Optional[float] = None
    snipers: list = field(default_factory=list)       # early buyer addresses (not the creator)
    sniper_held_pct: Optional[float] = None           # filled by the holdings check
    sniper_holding: int = 0
    verdict: str = "UNKNOWN"                          # BUNDLED / SNIPED / ORGANIC / UNKNOWN
    reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["snipers"] = self.snipers[:20]
        return d


async def first_trades(rpc, curve: str, mint: str, *, first_n: int = 40, max_pages: int = 5,
                       concurrency: int = 8):
    """(trades, pages, signatures_seen, reached_create, first_block_time, first_slot)."""
    oldest: list[dict] = []           # the last two pages, newest-first
    before, pages, seen = None, 0, 0
    reached = False
    while pages < max_pages:
        page = await rpc.get_signatures(curve, limit=1000, before=before)
        pages += 1
        page = page or []
        seen += len(page)
        if page:
            oldest = oldest[-1000:] + page   # the create transaction may sit just past a page break
            before = page[-1].get("signature")
        if len(page) < 1000:
            reached = True
            break
    if not reached:
        return [], pages, seen, False, None, None
    # newest-first: the create transaction is the last entry
    chron = [s for s in reversed(oldest) if s.get("signature")]
    if not chron:
        return [], pages, seen, True, None, None
    first_bt, first_slot = chron[0].get("blockTime"), chron[0].get("slot")
    todo = [s["signature"] for s in chron if not s.get("err")][:first_n]
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(sig):
        async with sem:
            try:
                tx = await rpc.get_transaction(sig)
            except Exception:  # noqa: BLE001 - an unreadable transaction is skipped, never guessed
                return []
        try:
            return decode_curve_transaction(tx, mint, sig).trades
        except Exception:  # noqa: BLE001
            return []

    trades = [t for batch in await asyncio.gather(*(one(s) for s in todo)) for t in batch]
    trades.sort(key=lambda t: (t.slot, t.block_time or 0))
    return trades, pages, seen, True, first_bt, first_slot


def measure(trades: list, creator: Optional[str], created_at: Optional[float], create_slot: Optional[int],
            *, early_seconds: float = 10.0, supply: float = PUMP_SUPPLY) -> LaunchBuys:
    lb = LaunchBuys(created_at=created_at, create_slot=create_slot, trades_decoded=len(trades))
    if not trades or create_slot is None:
        lb.reasons.append("no decodable launch trades")
        return lb
    lb.status = "OBSERVED"
    t0 = created_at if created_at is not None else trades[0].block_time
    dev = bundle = early = 0.0
    bundle_w, early_w = set(), []
    for t in trades:
        if t.side != "buy":
            continue
        in_early = t0 is not None and t.block_time is not None and t.block_time <= t0 + early_seconds
        if creator and t.wallet == creator:
            if in_early:
                dev += t.token_amount
            continue
        if t.slot <= create_slot + 1:
            bundle += t.token_amount
            bundle_w.add(t.wallet)
        if in_early:
            early += t.token_amount
            if t.wallet not in early_w:
                early_w.append(t.wallet)
    lb.dev_buy_pct = round(dev / supply * 100, 2)
    lb.bundle_pct = round(bundle / supply * 100, 2)
    lb.bundle_wallets = len(bundle_w)
    lb.early_buy_pct = round(early / supply * 100, 2)
    lb.early_wallets = len(early_w)
    lb.snipers = early_w
    # price from SOL paid per token, first trade vs the last trade inside the window
    priced = [t for t in trades if t.sol_amount and t.token_amount]
    inside = [t for t in priced if t0 is not None and t.block_time is not None and t.block_time <= t0 + early_seconds]
    if priced and inside:
        p0 = priced[0].sol_amount / priced[0].token_amount
        p1 = inside[-1].sol_amount / inside[-1].token_amount
        if p0 > 0:
            lb.early_price_x = round(p1 / p0, 2)
    return lb


def judge(lb: LaunchBuys, *, max_bundle_pct: float = 10.0, max_early_buy_pct: float = 25.0,
          max_opening_pct: float = 35.0, min_trades: int = 3) -> None:
    if lb.status != "OBSERVED":
        lb.verdict = "UNKNOWN"
        return
    if lb.trades_decoded < min_trades:
        lb.status, lb.verdict = "INCOMPLETE", "UNKNOWN"
        lb.reasons.append(f"only {lb.trades_decoded} launch trades could be verified")
        return
    opening = (lb.dev_buy_pct or 0) + (lb.early_buy_pct or 0)
    if (lb.bundle_pct or 0) >= max_bundle_pct:
        lb.verdict = "BUNDLED"
        lb.reasons.append(f"{lb.bundle_wallets} wallet(s) bought {lb.bundle_pct:.1f}% of supply in the creation "
                          f"block (bundled launch)")
    elif (lb.early_buy_pct or 0) >= max_early_buy_pct or opening >= max_opening_pct:
        lb.verdict = "SNIPED"
        lb.reasons.append(f"{lb.early_wallets} wallet(s) bought {lb.early_buy_pct:.1f}% of supply in the first "
                          f"seconds" + (f" (plus {lb.dev_buy_pct:.1f}% by the dev)" if lb.dev_buy_pct else "")
                          + ": a giant jump in buys at the start, not organic")
    else:
        lb.verdict = "ORGANIC"
        lb.reasons.append(f"opening buys {lb.early_buy_pct:.1f}% by {lb.early_wallets} wallet(s), bundle "
                          f"{lb.bundle_pct:.1f}%, dev {lb.dev_buy_pct:.1f}%")


def gate(lb: LaunchBuys) -> tuple[str, str]:
    if lb.verdict in ("BUNDLED", "SNIPED"):
        return "FAIL", lb.reasons[0]
    if lb.verdict == "ORGANIC":
        return "PASS", lb.reasons[0]
    return "UNKNOWN", lb.reasons[0] if lb.reasons else "launch trades unavailable"


async def analyze(rpc, mint: str, curve: Optional[str], creator: Optional[str], s) -> LaunchBuys:
    """Full check. Never raises."""
    if not curve or rpc is None:
        return LaunchBuys(reasons=["no bonding curve for this coin"])
    try:
        trades, pages, seen, reached, bt, slot = await first_trades(
            rpc, curve, mint, first_n=int(getattr(s, "launch_trades_first_n", 40)),
            max_pages=int(getattr(s, "launch_trades_max_pages", 5)))
    except Exception as e:  # noqa: BLE001
        lb = LaunchBuys(curve=curve, reasons=[f"launch trades unavailable: {type(e).__name__}"])
        return lb
    if not reached:
        lb = LaunchBuys(curve=curve, pages=pages, signatures_seen=seen, status="INCOMPLETE",
                        reasons=[f"launch not reachable: more than {seen:,} curve transactions"])
        return lb
    lb = measure(trades, creator, bt, slot, early_seconds=float(getattr(s, "launch_early_seconds", 10.0)))
    lb.curve, lb.pages, lb.signatures_seen, lb.reached_create = curve, pages, seen, True
    judge(lb, max_bundle_pct=float(getattr(s, "launch_max_bundle_pct", 10.0)),
          max_early_buy_pct=float(getattr(s, "launch_max_early_buy_pct", 25.0)),
          max_opening_pct=float(getattr(s, "launch_max_opening_pct", 35.0)))
    log.info("launch buys %s: %s dev=%s bundle=%s early=%s wallets=%s trades=%s pages=%s", mint, lb.verdict,
             lb.dev_buy_pct, lb.bundle_pct, lb.early_buy_pct, lb.early_wallets, lb.trades_decoded, pages)
    return lb
