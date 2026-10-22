"""Tiny buys and sells in the live trades (v2.5.9, operator request 22 Oct).

Bundlers and top holders often trade dust amounts back and forth: it keeps the coin moving on
the trades tab and on "most active" lists while they hold the supply. Measured on the verified
recent-trade sample the research already takes (event-decoded swaps only):

  micro          trades worth less than `micro_trade_sol` SOL
  insider_micro  micro trades by insiders: top-10 holders, launch snipers / bundlers, and members
                 of linked-wallet clusters

Gate `insider_micro_trades` (risk threshold): FAIL when insiders made at least
`micro_insider_min_trades` micro trades in the sample. The generic share of micro trades is
recorded (and flagged when most of a large sample is dust) but does not fail on its own: a
volume bot alone does not prove who is behind it.
"""
from __future__ import annotations

from typing import Iterable, Optional


def measure(trades: Iterable, insiders: set, micro_sol: float = 0.01) -> dict:
    rows = [t for t in trades or [] if getattr(t, "sol_amount", None) is not None]
    micro = [t for t in rows if t.sol_amount < micro_sol]
    ins = [t for t in micro if t.wallet in insiders]
    wallets: dict[str, int] = {}
    for t in ins:
        wallets[t.wallet] = wallets.get(t.wallet, 0) + 1
    return {
        "status": "OBSERVED" if rows else "UNAVAILABLE",
        "sample": len(rows),
        "micro": len(micro),
        "micro_share": round(len(micro) / len(rows), 3) if rows else None,
        "micro_sol": micro_sol,
        "insider_micro": len(ins),
        "insider_micro_buys": sum(1 for t in ins if t.side == "buy"),
        "insider_micro_sells": sum(1 for t in ins if t.side == "sell"),
        "insider_wallets": sorted(wallets, key=lambda w: -wallets[w])[:10],
        "insiders_known": len(insiders),
    }


def gate(m: Optional[dict], min_trades: int = 3) -> tuple[str, str]:
    if not m or m.get("status") != "OBSERVED":
        return "UNKNOWN", "no verified trade sample"
    if not m.get("insiders_known"):
        return "UNKNOWN", "no insider wallets known (holders / launch buyers / clusters)"
    n = m.get("insider_micro") or 0
    if min_trades and n >= min_trades:
        return "FAIL", (f"{n} tiny trades (< {m['micro_sol']:g} SOL) by top holders / bundlers / snipers "
                        f"({m['insider_micro_buys']} buys, {m['insider_micro_sells']} sells) in the last "
                        f"{m['sample']} trades: insiders faking activity")
    return "PASS", (f"{n} tiny insider trades in the last {m['sample']} trades "
                    f"({m['micro']} tiny trades in total)")
