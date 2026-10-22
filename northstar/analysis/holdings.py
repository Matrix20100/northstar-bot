"""Exact current holdings of a set of wallets in one token, read on-chain (v2.5.9).

Used for two things:
  * the operator's tracked wallets (an Axiom wallet-tracker export): which of them hold the coin;
  * launch snipers / bundlers: how much of the supply they still hold (Axiom's "Snipers H").

Method: each wallet's associated token account (ATA) for the mint is derived (analysis/pda.py,
checked against 1,191 real holder accounts) and read with getMultipleAccounts, 100 per call.
  * account with a balance > 0      -> HOLDING (balance read on-chain)
  * account exists with 0 balance   -> EXITED (it held the token and sold / moved it)
  * no account                      -> not found in its ATA (a wallet CAN hold elsewhere, so this
                                       is "not seen", never proof of zero)
A read error leaves the result UNAVAILABLE; nothing is guessed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .pda import TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID, associated_token_address

log = logging.getLogger("northstar.holdings")

B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


@dataclass
class TrackedWallet:
    address: str
    name: str = ""
    group: str = ""


def load_tracked_wallets(path) -> list[TrackedWallet]:
    """Read a wallet list. Accepted formats:
      * Axiom wallet-tracker export: [{"trackedWalletAddress", "name", "groups": [...]}, ...]
      * a JSON list of addresses, or of {"address"/"wallet", "name", "group"} objects
      * plain text: one address per line, optionally "address,name,group"
    Invalid addresses are skipped; duplicates keep the first entry. A missing file = no wallets."""
    p = Path(path)
    if not p.is_file():
        return []
    text = p.read_text(encoding="utf-8", errors="replace").strip()
    rows: list[TrackedWallet] = []
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        data = data.get("wallets") or list(data.values())
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                rows.append(TrackedWallet(item.strip()))
            elif isinstance(item, dict):
                addr = item.get("trackedWalletAddress") or item.get("address") or item.get("wallet") or ""
                groups = item.get("groups")
                group = (groups[0] if isinstance(groups, list) and groups else item.get("group")) or ""
                rows.append(TrackedWallet(str(addr).strip(), str(item.get("name") or "")[:40], str(group)[:40]))
    else:
        for line in text.splitlines():
            parts = [x.strip() for x in line.split(",")]
            if parts and parts[0] and not parts[0].startswith("#"):
                rows.append(TrackedWallet(parts[0], *(parts[1:3] + ["", ""])[:2]))
    out, seen = [], set()
    for w in rows:
        if B58.match(w.address) and w.address not in seen:
            seen.add(w.address)
            out.append(w)
    return out


@dataclass
class Holdings:
    status: str = "UNAVAILABLE"          # OBSERVED / UNAVAILABLE / NOT_CONFIGURED
    checked: int = 0
    holding: list[dict] = field(default_factory=list)   # {wallet, name, group, amount, pct}
    exited: list[dict] = field(default_factory=list)    # had an account, balance now 0
    held_pct: Optional[float] = None                     # % of supply held by the wallets found
    note: str = ""

    def to_dict(self) -> dict:
        return {"status": self.status, "checked": self.checked, "held_pct": self.held_pct,
                "holding": self.holding[:25], "holding_count": len(self.holding),
                "exited": self.exited[:25], "exited_count": len(self.exited), "note": self.note}


def _balance(acc: Optional[dict], mint: str) -> Optional[float]:
    """UI balance of a parsed token account for `mint`; None if it is not one."""
    if not isinstance(acc, dict) or not isinstance(acc.get("data"), dict):
        return None
    parsed = acc["data"].get("parsed") or {}
    info = parsed.get("info") or {}
    if info.get("mint") != mint:
        return None
    amt = (info.get("tokenAmount") or {})
    try:
        v = amt.get("uiAmount")
        return float(v if v is not None else amt.get("uiAmountString") or 0)
    except (TypeError, ValueError):
        return None


def _derive(wallets: list[TrackedWallet], mint: str, program: str) -> list[Optional[str]]:
    return [associated_token_address(w.address, mint, program) for w in wallets]


async def wallet_holdings(rpc, mint: str, wallets: list[TrackedWallet], supply_ui: Optional[float],
                          token_2022: Optional[bool]) -> Holdings:
    """Which of `wallets` hold `mint` right now (on-chain). Never raises."""
    h = Holdings(checked=len(wallets))
    if not wallets:
        h.status, h.note = "NOT_CONFIGURED", "no wallets to check"
        return h
    if rpc is None:
        h.note = "no Solana RPC"
        return h
    program = TOKEN_2022_PROGRAM_ID if token_2022 else TOKEN_PROGRAM_ID
    try:
        atas = await asyncio.to_thread(_derive, wallets, mint, program)
        pairs = [(w, a) for w, a in zip(wallets, atas) if a]
        accounts = await rpc.get_multiple_accounts([a for _, a in pairs])
    except Exception as e:  # noqa: BLE001 - a failed read is UNAVAILABLE, never a result
        h.note = f"read failed: {type(e).__name__}"
        return h
    if len(accounts) != len(pairs):
        h.note = f"incomplete read ({len(accounts)} of {len(pairs)} accounts)"
        return h
    total = 0.0
    for (w, _), acc in zip(pairs, accounts):
        bal = _balance(acc, mint)
        if bal is None:
            continue
        row = {"wallet": w.address, "name": w.name, "group": w.group, "amount": bal,
               "pct": round(bal / supply_ui * 100, 3) if supply_ui else None}
        if bal > 0:
            h.holding.append(row)
            total += bal
        else:
            h.exited.append(row)
    h.holding.sort(key=lambda r: -r["amount"])
    h.status = "OBSERVED"
    h.held_pct = round(total / supply_ui * 100, 3) if supply_ui else None
    return h


def summary(h: Optional[dict], limit: int = 4) -> Optional[str]:
    """'orangie, saale +3 (2.1% of supply)' for posts and logs; None when nobody holds it."""
    if not h or h.get("status") != "OBSERVED" or not h.get("holding_count"):
        return None
    names = [r.get("name") or (r["wallet"][:4] + "…" + r["wallet"][-4:]) for r in h.get("holding") or []]
    extra = h["holding_count"] - min(limit, len(names))
    txt = ", ".join(names[:limit]) + (f" +{extra}" if extra > 0 else "")
    if h.get("held_pct") is not None:
        txt += f" ({h['held_pct']:.2f}% of supply)"
    return txt
