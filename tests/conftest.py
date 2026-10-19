"""Shared MOCK fixtures.

EVERYTHING IN THIS FILE IS SYNTHETIC TEST DATA. Mints, wallets, prices and transactions are
generated for unit tests and do not describe any real token. Passing tests built on these
fakes is NOT live validation of the data providers.
"""
from __future__ import annotations

import copy
import os
import random
import time
from dataclasses import dataclass, field
from typing import Optional

import base58
import struct
import pytest

# The trade-plan tests were written against these levels (TP1 1.5x/50%, TP2 2x/25%, stop 0.7,
# trail 30%, time stop 30 min at 1.3x). Pin them for every Settings() in the tests so the plan logic
# keeps being checked with the same numbers; the shipped runner defaults have their own test.
OLD_PLAN = {"NORTHSTAR_PLAN_TP1": "1.5", "NORTHSTAR_PLAN_TP1_SELL_PCT": "50", "NORTHSTAR_PLAN_TP2": "2.0",
            "NORTHSTAR_PLAN_TP2_SELL_PCT": "25", "NORTHSTAR_PLAN_STOP": "0.7", "NORTHSTAR_PLAN_TRAIL_PCT": "30",
            "NORTHSTAR_PLAN_TIME_STOP_MINUTES": "30", "NORTHSTAR_PLAN_TIME_STOP_MULTIPLE": "1.3"}
os.environ.update(OLD_PLAN)

from northstar.config import Settings
from northstar.db import Database
from northstar.models import PairInfo
from northstar.net import ProviderError
from northstar.providers.solana_rpc import TOKEN_2022_PROGRAM, TOKEN_PROGRAM, WSOL_MINT
from northstar.research import Providers
from northstar.analysis.pump_trades import EVENT_IX_TAG
from northstar.analysis.pumpswap_trades import (BUY_EVENT_DISCRIMINATOR, PUMPSWAP_PROGRAM,
                                                SELL_EVENT_DISCRIMINATOR)

NOW = time.time()


def addr(seed: str) -> str:
    """Deterministic fake base58 address (32 bytes) — MOCK."""
    raw = (seed.encode() * 32)[:32]
    return base58.b58encode(raw).decode()


MINT = addr("mockmint")
POOL = addr("mockpool")
CREATOR = addr("creator")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, data_dir=str(tmp_path), dry_run=True, discord_enabled=False,
                    enable_pumpportal=False, log_json=False)


@pytest.fixture
def db(tmp_path) -> Database:
    d = Database(tmp_path / "test.db")
    d.open()
    yield d
    d.close()


# --------------------------------------------------------------------------- market fixtures
def make_pair(**over) -> PairInfo:
    base = dict(
        pair_address=POOL, dex_id="raydium", base_mint=MINT, base_symbol="MOCK", base_name="Mock Frog",
        quote_mint=WSOL_MINT, quote_symbol="SOL", price_usd=0.0012, price_native=0.000006,
        liquidity_usd=120_000.0, fdv=600_000.0, market_cap=600_000.0, pair_created_at=NOW - 6 * 3600,
        vol={"m5": 9_000.0, "h1": 150_000.0, "h6": 700_000.0, "h24": 900_000.0},
        txns={"m5": {"buys": 40, "sells": 25}, "h1": {"buys": 400, "sells": 250},
              "h6": {"buys": 2000, "sells": 1400}, "h24": {"buys": 3000, "sells": 2100}},
        price_change={"m5": 2.0, "h1": 15.0, "h6": 60.0, "h24": 80.0},
        url=f"https://dexscreener.com/solana/{POOL}", labels=[], image_url="https://example.invalid/i.png",
        websites=["https://example.invalid"], socials=[{"type": "twitter", "url": "https://x.invalid/mock"}],
        boosts_active=0, fetched_at=NOW)
    base.update(over)
    return PairInfo(**base)


def mint_account(program=TOKEN_PROGRAM, mint_auth=None, freeze_auth=None, supply=1_000_000_000,
                 decimals=6, extensions=None) -> dict:
    info = {"decimals": decimals, "supply": str(supply * 10 ** decimals), "isInitialized": True,
            "mintAuthority": mint_auth, "freezeAuthority": freeze_auth}
    if program == TOKEN_2022_PROGRAM:
        info["extensions"] = extensions or []
    return {"owner": program, "lamports": 1461600, "executable": False,
            "data": {"program": "spl-token", "parsed": {"type": "mint", "info": info}}}


def largest_and_owners(holder_pcts: list[tuple[str, float]], supply: float = 1_000_000_000):
    largest, owners = [], []
    for i, (owner, pct) in enumerate(holder_pcts):
        amt = supply * pct / 100
        largest.append({"address": addr(f"ta{i}{owner}"), "uiAmount": amt, "uiAmountString": str(amt)})
        owners.append({"owner": TOKEN_PROGRAM, "data": {"parsed": {"info": {"owner": owner}}}})
    return largest, owners


def healthy_holders() -> list[tuple[str, float]]:
    rows = [(POOL, 20.0)]
    rows += [(addr(f"holder{i}"), p) for i, p in enumerate([2.4, 2.2, 2.0, 1.9, 1.8, 1.7, 1.6, 1.5, 1.4, 1.3,
                                                            1.2, 1.1, 1.0, 0.9, 0.9, 0.8, 0.8, 0.7, 0.7])]
    return rows


def pumpswap_event_ix(wallet: str, side: str, base_raw: int, quote_raw: int, block_time: int,
                      pool: str = POOL) -> dict:
    """Synthetic PumpSwap BuyEvent/SellEvent self-CPI (documented layout) — MOCK."""
    disc = BUY_EVENT_DISCRIMINATOR if side == "buy" else SELL_EVENT_DISCRIMINATOR
    u64s = [base_raw, quote_raw, 0, 0, 10**15, 10**12, quote_raw, 20, 0, 5, 0, quote_raw, quote_raw]
    body = struct.pack("<q", block_time) + struct.pack("<13Q", *u64s)
    body += base58.b58decode(pool) + base58.b58decode(wallet) + bytes(32 * 5)
    data = EVENT_IX_TAG + disc + body
    return {"programId": PUMPSWAP_PROGRAM, "accounts": [], "data": base58.b58encode(data).decode()}


def swap_tx(wallet: str, side: str, amount: float, slot: int, block_time: int, mint: str = MINT,
            pool_owner: str = POOL, sol_lamports: int = 500_000_000, pumpswap_event: bool = True) -> dict:
    """Synthetic jsonParsed PumpSwap swap transaction — MOCK.

    With ``pumpswap_event`` the transaction carries the program-emitted swap event that Northstar
    can verify; without it the swap is only an unverified balance-delta candidate.
    """
    raw = int(round(amount * 1_000_000))
    d = raw if side == "buy" else -raw

    def bal(idx, owner, raw_amount):
        return {"accountIndex": idx, "mint": mint, "owner": owner,
                "uiTokenAmount": {"amount": str(raw_amount), "decimals": 6,
                                  "uiAmountString": f"{raw_amount / 1_000_000:.6f}"}}

    pre = [bal(1, wallet, 1_000_000_000_000), bal(2, pool_owner, 500_000_000_000_000)]
    post = [bal(1, wallet, 1_000_000_000_000 + d), bal(2, pool_owner, 500_000_000_000_000 - d)]
    inner = []
    top = []
    if pumpswap_event:
        top = [{"programId": PUMPSWAP_PROGRAM, "accounts": [], "data": base58.b58encode(bytes(8)).decode()}]
        inner = [{"index": 0, "instructions": [pumpswap_event_ix(wallet, side, raw, sol_lamports, block_time,
                                                                 pool_owner)]}]
    return {"slot": slot, "blockTime": block_time,
            "meta": {"err": None, "preTokenBalances": pre, "postTokenBalances": post, "innerInstructions": inner,
                     "preBalances": [10_000_000_000, 0, 0], "postBalances": [10_000_000_000 - sol_lamports, 0, 0]},
            "transaction": {"signatures": [f"sig-{wallet}-{slot}-{side}-{amount:.0f}"],
                            "message": {"accountKeys": [{"pubkey": wallet, "signer": True},
                                                        {"pubkey": addr("tokacc"), "signer": False},
                                                        {"pubkey": pool_owner, "signer": False}],
                                        "instructions": top}}}


def funding_tx(wallet: str, funder: str) -> dict:
    return {"slot": 1, "blockTime": int(NOW - 90 * 86400), "meta": {"err": None, "innerInstructions": []},
            "transaction": {"signatures": [f"fund-{wallet}"],
                            "message": {"accountKeys": [{"pubkey": funder, "signer": True}],
                                        "instructions": [{"parsed": {"type": "transfer", "info": {
                                            "source": funder, "destination": wallet, "lamports": 10 ** 9}}}]}}}


def healthy_trades(n: int = 120, start_slot: int = 1000) -> list[dict]:
    """Many distinct wallets, more buyers in the second half (rising) — MOCK."""
    rnd = random.Random(7)
    txs = []
    t0 = int(NOW - 3000)
    for i in range(n):
        side = "buy" if i % 4 != 3 else "sell"
        # more distinct buyers later in the window
        w = addr(f"trader{i if i > n // 3 else i % 25}")
        txs.append(swap_tx(w, side, rnd.uniform(20_000, 400_000), start_slot + i * 3, t0 + i * 25))
    return txs


# --------------------------------------------------------------------------- fake providers
@dataclass
class FakeDex:
    pairs: list = field(default_factory=lambda: [make_pair()])
    metas: Optional[list] = None
    competitors: list = field(default_factory=list)
    fail: Optional[Exception] = None
    calls: int = 0

    async def token_pairs(self, mint):
        self.calls += 1
        if self.fail:
            raise self.fail
        return [copy.deepcopy(p) for p in self.pairs if p.base_mint == mint or p.quote_mint == mint]

    async def tokens_batch(self, mints):
        if self.fail:
            raise self.fail
        return [copy.deepcopy(p) for p in self.pairs if p.base_mint in mints]

    async def trending_metas(self):
        if self.metas is None:
            raise ProviderError("dexscreener", "MOCK: metas unavailable")
        return self.metas

    async def search(self, q):
        return self.competitors

    async def latest_profiles(self):
        return []

    async def latest_boosts(self):
        return []

    async def top_boosts(self):
        return []

    async def community_takeovers(self):
        return []


@dataclass
class FakeRpc:
    account: Optional[dict] = field(default_factory=mint_account)
    holders: list = field(default_factory=healthy_holders)
    trades: list = field(default_factory=healthy_trades)
    launch: list = field(default_factory=list)
    wallet_info: dict = field(default_factory=dict)  # wallet -> (age_seconds, funder)
    fail: Optional[Exception] = None
    largest_fail: bool = False

    def _chk(self):
        if self.fail:
            raise self.fail

    async def call(self, method, params, cache_ttl=0):
        self._chk()
        return 123

    async def get_account_info(self, a):
        self._chk()
        return self.account

    async def get_token_supply(self, mint):
        self._chk()
        return {"uiAmountString": "1000000000"}

    async def get_token_largest_accounts(self, mint):
        self._chk()
        if self.largest_fail:
            raise ProviderError("solana_rpc", "MOCK: largest accounts disabled")
        self._largest, self._owners = largest_and_owners(self.holders)
        return self._largest

    async def get_multiple_accounts(self, addrs):
        self._chk()
        return self._owners[: len(addrs)]

    def _all_tx(self):
        return self.trades + self.launch

    async def get_signatures(self, address, limit=100, before=None):
        self._chk()
        if address == POOL:
            # newest first; launch trades are the oldest
            txs = sorted(self._all_tx(), key=lambda t: -t["slot"])
            return [{"signature": t["transaction"]["signatures"][0], "slot": t["slot"], "err": None,
                     "blockTime": t["blockTime"]} for t in txs][:limit]
        info = self.wallet_info.get(address)
        if info is None:
            return [{"signature": f"old-{address}", "slot": 1, "err": None,
                     "blockTime": int(NOW - 120 * 86400)}]
        age, _ = info
        return [{"signature": f"fund-{address}", "slot": 1, "err": None, "blockTime": int(NOW - age)}]

    async def get_transaction(self, sig):
        self._chk()
        for t in self._all_tx():
            if t["transaction"]["signatures"][0] == sig:
                return t
        if sig.startswith("fund-"):
            for w, (_, funder) in self.wallet_info.items():
                if sig == f"fund-{w}" and funder:
                    return funding_tx(w, funder)
        return None


@dataclass
class FakeRugCheck:
    report_data: Optional[dict] = None
    fail: Optional[Exception] = None

    async def report(self, mint):
        if self.fail:
            raise self.fail
        return copy.deepcopy(self.report_data or {})

    async def summary(self, mint):
        return {"risks": []}


def rc_report(**over) -> dict:
    base = {"mint": MINT, "creator": CREATOR, "creatorBalance": 0, "rugged": False, "risks": [],
            "price": 0.0012, "token": {"supply": 10 ** 15, "decimals": 6, "mintAuthority": None,
                                       "freezeAuthority": None},
            "markets": [{"pubkey": POOL, "marketType": "raydium", "liquidityA": addr("vaultA"),
                         "liquidityB": addr("vaultB"), "lp": {"lpLockedPct": 100.0}}],
            "lockers": {}, "knownAccounts": {}, "topHolders": None, "creatorTokens": []}
    base.update(over)
    return base


def strong_metas() -> list[dict]:
    return [{"name": "Frog memes", "slug": "frog", "tokenCount": 40,
             "marketCapChange": {"m5": 1, "h1": 5, "h6": 30, "h24": 50}}]


class FakeSocials:
    """Free social checks: the token's own Telegram reports a public member count."""
    def __init__(self, members: int = 2500):
        self.members = members

    async def metadata(self, uri):
        return None

    async def telegram_members(self, handle):
        return self.members


def strong_providers() -> Providers:
    return Providers(dex=FakeDex(metas=strong_metas()), rugcheck=FakeRugCheck(rc_report()), rpc=FakeRpc(),
                     socials=FakeSocials())


def seed_profile(db: Database, mint: str = MINT) -> None:
    db.upsert_discovery(mint, "dexscreener_profile", 2.0, payload={
        "description": "Mock Frog is a community frog meme on Solana with daily art drops.",
        "links": [{"type": "twitter", "url": "https://x.com/mockfrogsol"},
                  {"type": "telegram", "url": "https://t.me/mockfrogportal"}]})


# --------------------------------------------------------------------------- publisher
class MemoryPublisher:
    def __init__(self):
        self.messages: dict[int, dict] = {}
        self.edits: list[tuple[int, dict]] = []
        self.sent: list[tuple[Optional[int], dict]] = []
        self.fail_next: list[Exception] = []
        self._id = 1000

    async def start(self):
        return None

    async def publish(self, embed, channel_id=None):
        if self.fail_next:
            raise self.fail_next.pop(0)
        self._id += 1
        self.messages[self._id] = embed
        self.sent.append((channel_id, embed))
        return channel_id or 42, self._id

    async def edit(self, channel_id, message_id, embed):
        self.edits.append((message_id, embed))
        self.messages[message_id] = embed

    async def find_message_with(self, text, channel_id=None, limit=100):
        import json
        for mid, e in self.messages.items():
            if text in json.dumps(e):
                return mid
        return None

    async def close(self):
        return None


LIVE = os.environ.get("NORTHSTAR_LIVE_TESTS") == "1"
