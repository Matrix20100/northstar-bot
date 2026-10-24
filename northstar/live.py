"""Live trading with real money (v5.0.0). OFF by default.

Operator: "I want to be able to trade with real money I put in an account for a bot and control the
account, but the bot trades with the funds for me. We need to stop taking big hits."

How it works
  * A NEW, bot-only Solana wallet. The operator funds it with what he is prepared to lose and puts its
    private key in NORTHSTAR_TRADE_WALLET_PRIVATE_KEY on the server himself. The key never leaves the
    server, is never logged and never shown (solana_tx.py). Withdraw at any time from a wallet app.
  * BUY: when the bot posts a call it follows (`trade_follow`: "runner" = runner calls, "all" = every
    call except rug scalps), it buys `trade_sol_per_trade` SOL of the coin through PumpPortal's Local
    Transaction API (pump.fun curve, PumpSwap, Raydium: pool "auto"), signs it locally and sends it
    through the bot's own RPC. A coin already up `trade_max_chase_pct` (25%) since the call is skipped.
  * SELL: every open position is managed by the exit engine (exits.py) on a fresh read of its pool:
    stop loss, trailing stop, sellers taking over, fading volume, no progress, take profit in steps.
  * HARD LIMITS, checked before every buy: trading switched on; at most `trade_max_open` positions; no
    new buys once today's result (UTC) reaches -`trade_daily_loss_limit_sol`; `trade_min_sol_reserve`
    always kept for fees.
  * KILL SWITCH (dashboard Trading tab): switches trading off and sells everything at once.
  * TEST MODE (`trade_test_mode`, ON by default): every trade is built, signed and SIMULATED on-chain
    (simulateTransaction) but never sent. Positions are recorded as test positions, so the whole loop
    can be checked with the real wallet before one lamport moves.

Every amount comes from the chain (the confirmed transaction's balance changes), not from estimates.
This is not financial advice; memecoins can go to zero in minutes.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Optional

from . import exits as EX

log = logging.getLogger("northstar.live")

PUMPPORTAL_TRADE_URL = "https://pumpportal.fun/api/trade-local"
LAMPORTS = 1_000_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    test INTEGER NOT NULL DEFAULT 0,
    opened_at REAL NOT NULL,
    entry_mcap REAL,
    sol_in REAL NOT NULL DEFAULT 0,
    tokens REAL NOT NULL DEFAULT 0,
    tokens_left REAL NOT NULL DEFAULT 0,
    sol_out REAL NOT NULL DEFAULT 0,
    last_mcap REAL,
    peak_mcap REAL,
    stop_x REAL,
    done TEXT NOT NULL DEFAULT '[]',
    fills TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'OPEN',
    exit_reason TEXT,
    closed_at REAL,
    error TEXT,
    fails INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_live_ref ON live_trades(ref, test);
CREATE INDEX IF NOT EXISTS ix_live_open ON live_trades(status);
"""


def init_schema(db) -> None:
    with db._lock:
        db._conn.executescript(SCHEMA)
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(live_trades)").fetchall()}
        for col, decl in (("conviction", "REAL"), ("conv", "TEXT"), ("grace_since", "REAL"), ("entry_liq", "REAL"),
                          ("engine_note", "TEXT")):
            if col not in cols:
                db._conn.execute(f"ALTER TABLE live_trades ADD COLUMN {col} {decl}")


def held_share(r: dict) -> float:
    """Share of the position still held: from the token balance for real trades, from the shares sold for
    test trades (a simulated buy receives no tokens)."""
    if r.get("test") or not r.get("tokens"):
        fills = r["fills"] if isinstance(r.get("fills"), list) else json.loads(r.get("fills") or "[]")
        return max(0.0, 1.0 - sum(f.get("share", 0) for f in fills if f.get("kind") != "BUY"))
    return max(0.0, (r.get("tokens_left") or 0) / r["tokens"])


class TradeError(Exception):
    pass


class LiveTrader:
    def __init__(self, s, db, rpc, dex, wallet=None, http=None, sleep=None, now=None):
        self.s, self.db, self.rpc, self.dex = s, db, rpc, dex
        self.wallet = wallet
        self._http = http                    # async (url, data) -> (status, bytes); injectable for tests
        self._sleep = sleep or asyncio.sleep
        self._now = now or time.time
        self.balance_sol: Optional[float] = None
        self.balance_at = 0.0
        self.last_error: Optional[str] = None
        self.kill_requested = False
        init_schema(db)

    # ------------------------------------------------------------------ setup
    @classmethod
    def from_settings(cls, s, db, rpc, dex) -> "LiveTrader":
        from .solana_tx import KeyError_, Wallet
        wallet, err = None, None
        key = getattr(s, "trade_wallet_private_key", None)
        if key is not None:
            try:
                wallet = Wallet.from_text(key.get_secret_value() if hasattr(key, "get_secret_value") else str(key))
            except (KeyError_, ImportError) as e:
                err = str(e) if isinstance(e, KeyError_) else "the 'cryptography' package is missing (pip install)"
        t = cls(s, db, rpc, dex, wallet)
        t.last_error = err
        if wallet:
            log.info("live trading wallet %s loaded (trading %s, %s)", wallet.address,
                     "ON" if getattr(s, "trade_enabled", False) else "OFF",
                     "TEST MODE: simulate only" if t.test_mode else "REAL MONEY")
        return t

    @property
    def test_mode(self) -> bool:
        return bool(getattr(self.s, "trade_test_mode", True))

    def enabled(self) -> bool:
        return bool(getattr(self.s, "trade_enabled", False) and self.wallet is not None)

    # ------------------------------------------------------------------ chain
    async def balance(self, force: bool = False) -> Optional[float]:
        now = self._now()
        if self.wallet is None:
            return None
        if force or now - self.balance_at > 30:
            try:
                r = await self.rpc.call("getBalance", [self.wallet.address, {"commitment": "confirmed"}])
                self.balance_sol = (r.get("value") if isinstance(r, dict) else r) / LAMPORTS
                self.balance_at = now
            except Exception as e:  # noqa: BLE001
                self.last_error = f"balance read failed: {type(e).__name__}"
        return self.balance_sol

    async def _build(self, action: str, mint: str, amount, in_sol: bool) -> bytes:
        data = {"publicKey": self.wallet.address, "action": action, "mint": mint, "amount": amount,
                "denominatedInSol": "true" if in_sol else "false",
                "slippage": float(getattr(self.s, "trade_slippage_pct", 15.0)),
                "priorityFee": float(getattr(self.s, "trade_priority_fee_sol", 0.0005)), "pool": "auto"}
        if self._http is not None:
            status, body = await self._http(PUMPPORTAL_TRADE_URL, data)
        else:
            import httpx
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(PUMPPORTAL_TRADE_URL, data=data)
                status, body = r.status_code, r.content
        if status != 200 or not body:
            raise TradeError(f"PumpPortal could not build the {action} ({status}): {body[:120]!r}")
        return bytes(body)

    async def _send(self, signed: bytes, simulate: bool) -> str:
        b64 = base64.b64encode(signed).decode()
        if simulate:
            r = await self.rpc.call("simulateTransaction", [b64, {"encoding": "base64", "sigVerify": True,
                                                                   "commitment": "confirmed"}])
            err = (r or {}).get("value", {}).get("err") if isinstance(r, dict) else None
            if err:
                logs = ((r or {}).get("value") or {}).get("logs") or []
                raise TradeError(f"simulation failed: {err} {' | '.join(logs[-2:])[:200]}")
            return "SIMULATED"
        return await self.rpc.call("sendTransaction", [b64, {"encoding": "base64", "skipPreflight": False,
                                                               "preflightCommitment": "confirmed", "maxRetries": 3}])

    async def _confirm(self, sig: str, timeout: float = 60.0) -> dict:
        """Wait for the transaction and read what it really did: SOL and token changes for the wallet."""
        deadline = self._now() + timeout
        while self._now() < deadline:
            st = await self.rpc.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
            v = ((st or {}).get("value") or [None])[0] if isinstance(st, dict) else None
            if v and v.get("err"):
                raise TradeError(f"transaction failed on-chain: {v['err']}")
            if v and v.get("confirmationStatus") in ("confirmed", "finalized"):
                break
            await self._sleep(2)
        else:
            raise TradeError("not confirmed in time (it may still land; the position is re-read next tick)")
        tx = await self.rpc.call("getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed",
                                                          "maxSupportedTransactionVersion": 0}])
        return self._deltas(tx or {})

    def _deltas(self, tx: dict) -> dict:
        meta = tx.get("meta") or {}
        keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
        addrs = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
        sol = 0.0
        if self.wallet.address in addrs:
            i = addrs.index(self.wallet.address)
            pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
            if i < len(pre) and i < len(post):
                sol = (post[i] - pre[i]) / LAMPORTS

        def toks(rows):
            return sum(float((r.get("uiTokenAmount") or {}).get("uiAmount") or 0) for r in rows or []
                       if r.get("owner") == self.wallet.address and r.get("mint") == self._mint)
        tokens = toks(meta.get("postTokenBalances")) - toks(meta.get("preTokenBalances"))
        return {"sol": sol, "tokens": tokens}

    async def _trade(self, action: str, mint: str, amount, in_sol: bool, simulate: bool) -> dict:
        """Build, check, sign and send (or simulate) one trade. A TradeError with `.sent` set means the
        transaction WAS sent but could not be confirmed in time: it may still land."""
        self._mint = mint
        raw = await self._build(action, mint, amount, in_sol)
        payer = self.wallet.fee_payer(raw)
        if payer != self.wallet.address:
            raise TradeError("PumpPortal returned a transaction for another wallet - refused")
        signed = self.wallet.sign_transaction(raw)
        sig = await self._send(signed, simulate)
        if sig == "SIMULATED":
            return {"sig": sig, "sol": None, "tokens": None}
        try:
            d = await self._confirm(sig)
        except TradeError as e:
            if "failed on-chain" not in str(e):
                e.sent = sig                         # sent, not confirmed: it may still land
            raise
        return {"sig": sig, **d}

    # ------------------------------------------------------------------ bookkeeping
    def today_result_sol(self, now: Optional[float] = None) -> float:
        """Today's (UTC) result in SOL: closed trades' result plus open positions marked at the last price."""
        now = now or self._now()
        day0 = now - (now % 86400)
        test = 1 if self.test_mode else 0
        rows = self.db.fetchall("SELECT * FROM live_trades WHERE test = ? AND status IN ('OPEN','CLOSED') "
                                "AND (closed_at IS NULL OR closed_at >= ?) AND opened_at >= ?", (test, day0, day0 - 86400))
        total = 0.0
        for r in rows:
            if r["status"] == "CLOSED" and (r["closed_at"] or 0) < day0:
                continue
            open_val = 0.0
            if r["status"] == "OPEN" and r["entry_mcap"] and r["last_mcap"]:
                open_val = r["sol_in"] * held_share(dict(r)) * (r["last_mcap"] / r["entry_mcap"])
            total += r["sol_out"] + open_val - r["sol_in"]
        return total

    def open_positions(self, all_modes: bool = False) -> list[dict]:
        if all_modes:
            return [dict(r) for r in self.db.fetchall("SELECT * FROM live_trades WHERE status = 'OPEN'")]
        return [dict(r) for r in self.db.fetchall("SELECT * FROM live_trades WHERE status = 'OPEN' AND test = ?",
                                                  (1 if self.test_mode else 0,))]

    async def can_buy(self) -> Optional[str]:
        """Why a new buy is not allowed right now (None = allowed)."""
        if not self.enabled():
            return "trading is off" if self.wallet else "no wallet key set"
        if self.kill_requested:
            return "kill switch"
        if len(self.open_positions()) >= int(getattr(self.s, "trade_max_open", 3)):
            return f"{getattr(self.s, 'trade_max_open', 3)} positions already open"
        limit = float(getattr(self.s, "trade_daily_loss_limit_sol", 0.5))
        today = self.today_result_sol()
        if limit and today <= -limit:
            return f"daily loss limit reached ({today:+.3f} SOL today, limit -{limit:g})"
        if not self.test_mode:
            bal = await self.balance(force=True)
            need = float(getattr(self.s, "trade_sol_per_trade", 0.1)) + float(getattr(self.s, "trade_min_sol_reserve", 0.03))
            if bal is None or bal < need:
                return f"wallet balance {bal if bal is not None else '?'} SOL under {need:g} (trade + fee reserve)"
        return None

    # ------------------------------------------------------------------ buying
    async def run_once(self, now: Optional[float] = None) -> dict:
        """Buy the calls the bot just posted (within the last 2 minutes), within the limits."""
        from .paper import calls_since, pick_pair
        counts = {"bought": 0, "skipped": 0}
        if not self.enabled():
            return counts
        now = now or self._now()
        follow = str(getattr(self.s, "trade_follow", "runner") or "runner")
        test = 1 if self.test_mode else 0
        for c in calls_since(self.db, now - 120, self.s):
            if c.get("kind") == "rug_scalp" or (follow == "runner" and c.get("kind") != "runner"):
                continue
            if self.db.fetchone("SELECT 1 AS x FROM live_trades WHERE ref = ? AND test = ?", (c["ref"], test)):
                continue
            if self.db.fetchone("SELECT 1 AS x FROM live_trades WHERE mint = ? AND status = 'OPEN'", (c["mint"],)):
                self._skip(c, "this coin is already held (one position per coin)", now, test)
                continue
            why = await self.can_buy()
            if why:
                self._skip(c, why, now, test)
                counts["skipped"] += 1
                continue
            pairs = await self.dex.tokens_batch([c["mint"]]) if self.dex else []
            pair = pick_pair(pairs, c["mint"])
            tick = EX.Tick.from_pair(pair, now) if pair else None
            if tick is None:
                self._skip(c, "no live price for the coin", now, test)
                counts["skipped"] += 1
                continue
            chase = float(getattr(self.s, "trade_max_chase_pct", 25.0))
            if c.get("entry") and tick.mcap > c["entry"] * (1 + chase / 100):
                self._skip(c, f"already up {100 * (tick.mcap / c['entry'] - 1):.0f}% since the call", now, test)
                counts["skipped"] += 1
                continue
            # v5.1.0: grade the setup. Weak setups are skipped; the rest get their own stop and a size of at most
            # trade_sol_per_trade (smaller for weaker setups and wider stops, never bigger)
            from . import conviction as CV
            conv, liq = None, None
            if CV.enabled(self.s):
                report = CV.report_for(self.db, c["ref"], c["mint"])
                conv, liq = CV.assess(report, self.s), CV.entry_liquidity(report)
                floor = float(getattr(self.s, "trade_min_conviction", 40.0) or 0)
                if floor and conv.score < floor:
                    self._skip(c, f"conviction {conv.score:.0f} ({conv.grade}) under {floor:.0f}: "
                                  + "; ".join(conv.why[:3]), now, test)
                    counts["skipped"] += 1
                    continue
            sol = float(getattr(self.s, "trade_sol_per_trade", 0.1))
            if conv is not None:
                sol = round(max(0.001, sol * conv.size_mult), 6)
            cj = json.dumps(conv.to_dict()) if conv is not None else None
            cs = conv.score if conv is not None else None
            try:
                res = await self._trade("buy", c["mint"], sol, in_sol=True, simulate=bool(test))
            except Exception as e:  # noqa: BLE001
                self.last_error = f"buy {c.get('symbol') or c['mint'][:6]} failed: {str(e)[:200]}"
                log.warning("live: %s", self.last_error)
                if getattr(e, "sent", None):
                    # sent but not confirmed: it may have landed. Track it so the exit engine sells
                    # whatever was bought (sells are a % of the wallet's balance of this coin)
                    self.db.execute(
                        "INSERT INTO live_trades(ref, mint, symbol, test, opened_at, entry_mcap, sol_in, last_mcap, "
                        "peak_mcap, fills, error, conviction, conv, entry_liq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (c["ref"], c["mint"], c.get("symbol"), test, now, tick.mcap, sol, tick.mcap, tick.mcap,
                         json.dumps([{"kind": "BUY", "at": now, "mcap": tick.mcap, "sol": sol, "sig": e.sent,
                                      "unconfirmed": True}]), "buy sent but not confirmed in time", cs, cj,
                         liq or tick.liquidity))
                else:
                    self._skip(c, self.last_error, now, test, status="FAILED")
                continue
            spent = -res["sol"] if res["sol"] is not None else sol
            tokens = res["tokens"] if res["tokens"] is not None else 0.0   # test mode: tracked by share sold
            self.db.execute(
                "INSERT INTO live_trades(ref, mint, symbol, test, opened_at, entry_mcap, sol_in, tokens, tokens_left, "
                "last_mcap, peak_mcap, fills, conviction, conv, entry_liq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (c["ref"], c["mint"], c.get("symbol"), test, now, tick.mcap, spent, tokens, tokens, tick.mcap, tick.mcap,
                 json.dumps([{"kind": "BUY", "at": now, "mcap": tick.mcap, "sol": round(spent, 6), "sig": res["sig"]}]),
                 cs, cj, tick.liquidity or liq))
            counts["bought"] += 1
            log.info("live%s: bought $%s for %.4f SOL at $%.0f market cap (%s)%s", " TEST" if test else "",
                     c.get("symbol") or c["mint"][:6], spent, tick.mcap, res["sig"],
                     f" - {conv.label()}" if conv is not None else "")
        return counts

    def _skip(self, c: dict, why: str, now: float, test: int, status: str = "SKIPPED") -> None:
        self.db.execute("INSERT OR IGNORE INTO live_trades(ref, mint, symbol, test, opened_at, status, error, closed_at) "
                        "VALUES (?,?,?,?,?,?,?,?)", (c["ref"], c["mint"], c.get("symbol"), test, now, status, why[:300], now))

    # ------------------------------------------------------------------ selling
    async def manage(self, now: Optional[float] = None) -> dict:
        """Run the exit engine on every open position (and sell everything after a kill)."""
        from .paper import pick_pair
        counts = {"open": 0, "sold": 0, "closed": 0}
        if self.wallet is None:
            return counts
        now = now or self._now()
        # every open position, each in the mode it was bought in: a real position is always sold for real,
        # a test position is only ever simulated, whatever the switch says now
        rows = self.open_positions(all_modes=True)
        counts["open"] = len(rows)
        if not rows:
            self.kill_requested = False
            return counts
        pairs = []
        mints = sorted({r["mint"] for r in rows})
        for i in range(0, len(mints), 30):
            try:
                pairs += await self.dex.tokens_batch(mints[i:i + 30])
            except Exception:  # noqa: BLE001
                log.info("live: price refresh failed", exc_info=True)
        for r in rows:
            pair = pick_pair(pairs, r["mint"])
            tick = EX.Tick.from_pair(pair, now) if pair else None
            if self.kill_requested:
                actions = [EX.Action("KILL", EX.ALL, "kill switch: sell everything")]
                mcap = tick.mcap if tick else (r["last_mcap"] or r["entry_mcap"])
            else:
                if tick is None:
                    continue
                from .paper import position_from_row
                pos = position_from_row(r, self.s)
                actions = EX.decide(pos, tick, self.s)
                mcap = tick.mcap
                self.db.execute("UPDATE live_trades SET last_mcap=?, peak_mcap=?, stop_x=?, grace_since=?, engine_note=? "
                                "WHERE id=?", (tick.mcap, pos.peak_mcap, pos.stop_x, pos.grace_since, pos.note, r["id"]))
                r.update(last_mcap=tick.mcap, peak_mcap=pos.peak_mcap, stop_x=pos.stop_x)
            for a in actions:
                closed = await self._sell(r, a, mcap, now)
                counts["sold"] += 1
                if closed:
                    counts["closed"] += 1
                    break
        return counts

    async def _sell(self, r: dict, a, mcap: float, now: float) -> bool:
        test = bool(r["test"])
        left_share = held_share(r)
        if left_share <= 1e-9:
            return True
        share = left_share if a.fraction >= EX.ALL else min(left_share, a.fraction)
        pct = 100.0 if a.fraction >= EX.ALL else min(100.0, 100.0 * share / left_share)
        try:
            res = await self._trade("sell", r["mint"], f"{pct:.0f}%", in_sol=False, simulate=test)
        except Exception as e:  # noqa: BLE001 - retried next tick
            if getattr(e, "sent", None) and a.fraction < EX.ALL:
                # a partial sell that was sent but not confirmed may have landed: do not sell that share
                # again. (A sell-everything is simply repeated: "100%" of what is left is always right.)
                done = set(json.loads(r["done"] or "[]")) | {a.kind}
                fills = json.loads(r["fills"] or "[]") + [{"kind": a.kind, "at": now, "mcap": mcap, "share": round(share, 4),
                                                           "sig": e.sent, "unconfirmed": True, "why": a.reason[:160]}]
                self.db.execute("UPDATE live_trades SET done=?, fills=?, error=? WHERE id=?",
                                (json.dumps(sorted(done)), json.dumps(fills[-40:]), "a sell was sent but not confirmed",
                                 r["id"]))
                r.update(done=json.dumps(sorted(done)), fills=json.dumps(fills))
                return False
            fails = (r.get("fails") or 0) + 1
            self.last_error = f"sell {r.get('symbol') or r['mint'][:6]} ({a.kind}) failed: {str(e)[:200]}"
            log.warning("live: %s (attempt %d)", self.last_error, fails)
            self.db.execute("UPDATE live_trades SET fails=?, error=? WHERE id=?", (fails, self.last_error[:300], r["id"]))
            return False
        x = mcap / r["entry_mcap"] if r["entry_mcap"] else None
        if res["sol"] is not None:
            got, sold_tokens = res["sol"], -res["tokens"]
        else:                                               # test mode: notional value at the read price
            got = r["sol_in"] * share * (x or 0)
            sold_tokens = 0.0
        fills = json.loads(r["fills"] or "[]") + [{"kind": a.kind, "at": now, "mcap": mcap, "x": round(x or 0, 4),
                                                   "sol": round(got, 6), "share": round(share, 4), "sig": res["sig"],
                                                   "why": a.reason[:160]}]
        done = set(json.loads(r["done"] or "[]")) | {a.kind}
        tokens_left = max(0.0, (r["tokens_left"] or 0) - sold_tokens)
        closed = a.fraction >= EX.ALL or (not test and r["tokens"] and tokens_left <= r["tokens"] * 1e-6) \
            or (test and left_share - share <= 1e-9)
        self.db.execute("UPDATE live_trades SET sol_out=sol_out+?, tokens_left=?, fills=?, done=?, status=?, exit_reason=?, "
                        "closed_at=?, last_mcap=?, error=NULL, fails=0 WHERE id=?",
                        (got, 0.0 if closed else tokens_left, json.dumps(fills[-40:]), json.dumps(sorted(done)),
                         "CLOSED" if closed else "OPEN", a.kind if closed else r.get("exit_reason"),
                         now if closed else None, mcap, r["id"]))
        r.update(sol_out=(r["sol_out"] or 0) + got, tokens_left=tokens_left, fills=json.dumps(fills), done=json.dumps(sorted(done)))
        log.info("live%s: sold %.0f%% of $%s (%s: %s) for %.4f SOL", " TEST" if test else "", pct,
                 r.get("symbol") or r["mint"][:6], a.kind, a.reason, got)
        return closed

    def kill(self) -> None:
        """Dashboard kill switch: trading off is done by the caller (a stored setting); sell everything now."""
        self.kill_requested = True

    def status(self) -> dict:
        test = 1 if self.test_mode else 0
        hist = [dict(r) for r in self.db.fetchall("SELECT * FROM live_trades WHERE test = ? ORDER BY id DESC LIMIT 40",
                                                  (test,))]
        for h in hist:
            try:
                h["fills"] = json.loads(h["fills"] or "[]")
            except ValueError:
                h["fills"] = []
            try:
                h["conv"] = json.loads(h.get("conv") or "null")
            except (TypeError, ValueError):
                h["conv"] = None
        return {"wallet": self.wallet.address if self.wallet else None, "enabled": self.enabled(),
                "test_mode": self.test_mode, "balance_sol": self.balance_sol, "last_error": self.last_error,
                "today_sol": round(self.today_result_sol(), 6), "kill_requested": self.kill_requested,
                "limits": {k: getattr(self.s, k, None) for k in ("trade_sol_per_trade", "trade_max_open",
                                                                 "trade_daily_loss_limit_sol", "trade_min_sol_reserve",
                                                                 "trade_slippage_pct", "trade_max_chase_pct",
                                                                 "trade_follow", "trade_min_conviction")},
                "trades": hist}
