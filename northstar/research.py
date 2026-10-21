"""Per-candidate research pipeline.

Cheap checks run first; expensive on-chain sampling only happens for candidates that survive.
Every external failure becomes UNAVAILABLE/UNKNOWN data — never a clean result.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .analysis import holders as H
from .analysis import wallets as W
from .analysis.pump_trades import b58encode
from .analysis.pda import (metaplex_metadata_address, parse_metaplex_metadata,
                           pump_bonding_curve_address)
from .providers.solana_rpc import QUOTE_MINTS, WSOL_MINT
from .analysis.creator import analyze_creator
from .analysis.decision import Decision, decide
from .analysis.identity import is_valid_mint_format, parse_mint_account, select_pool
from .analysis.market import analyze_market
from .analysis.narrative import analyze_narrative
from .analysis import socials as SOC
from .analysis.scoring import (
    assess_risk,
    build_score,
    evidence_completeness,
    score_demand,
    score_holders,
    score_liquidity,
    score_momentum,
    score_wallet_quality,
)
from .analysis import lp_onchain as LPC
from .analysis.lp_onchain import pumpswap_lp_burn
from .analysis.security import (
    assess_liquidity,
    check_authorities,
    check_extensions,
    lp_gate,
    pool_kind,
    resolve_lp_floor,
    rugcheck_findings,
)
from .config import Settings
from .db import Database
from .models import (
    Action,
    Confidence,
    DataStatus,
    Datum,
    Flag,
    GateResult,
    GateStatus,
    PairInfo,
    RiskLevel,
    Severity,
)
from .net import ProviderError

log = logging.getLogger("northstar.research")

RAYDIUM_V4_AUTHORITY = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_CURVE_DISCRIMINATOR = bytes([23, 183, 248, 55, 96, 216, 172, 96])


@dataclass
class Providers:
    dex: Any = None
    rugcheck: Any = None
    rpc: Any = None
    sol_price: Any = None  # SolUsdPrice: converts curve (SOL) values to USD for display
    socials: Any = None
    coingecko_markets: Any = None
    jupiter: Any = None    # FreeSocials: token metadata links + public Telegram counts (free)
    coingecko: Any = None  # CoinGeckoOnchain: 1-minute candles + trending pools


@dataclass
class ResearchOutcome:
    mint: str
    action: Action
    report: dict
    data_hash: Optional[str] = None
    pair: Optional[PairInfo] = None
    snapshot_id: Optional[int] = None
    score: Optional[float] = None
    risk: RiskLevel = RiskLevel.INCOMPLETE
    confidence: Confidence = Confidence.LOW
    reasons: list[str] = field(default_factory=list)
    skipped_unchanged: bool = False
    not_ready: bool = False      # nothing to judge yet: no pool, no trades. Not a decision.


def data_hash(pair: Optional[PairInfo]) -> Optional[str]:
    if not pair:
        return None

    key = [
        pair.pair_address,
        round(pair.liquidity_usd or 0, -2),
        f"{pair.price_usd or 0:.4g}",
        pair.txns["h1"]["buys"],
        pair.txns["h1"]["sells"],
    ]
    return hashlib.sha1(json.dumps(key).encode()).hexdigest()[:16]


class Researcher:
    def __init__(self, s: Settings, db: Database, p: Providers):
        self.s, self.db, self.p = s, db, p
        self._metas: Optional[list[dict]] = None
        self._metas_at = 0.0
        self._descriptions: dict[str, str] = {}   # token metadata descriptions, for the AI judge

    async def trending_metas(self) -> Optional[list[dict]]:
        if self.p.dex is None:
            return None

        if self._metas is None or time.time() - self._metas_at > 300:
            try:
                self._metas = await self.p.dex.trending_metas()
                self._metas_at = time.time()
            except ProviderError as e:
                log.info("trending metas unavailable: %s", e)
                return self._metas

        return self._metas

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    async def research(
        self,
        mint: str,
        cand: Optional[dict] = None,
        force: bool = False,
    ) -> ResearchOutcome:
        s = self.s
        now = time.time()
        report: dict = {
            "mint": mint,
            "ts": now,
            "sources": {},
            "missing": [],
        }

        # 1. Validate mint format.
        if not is_valid_mint_format(mint):
            return self._final(
                mint,
                Action.REJECT,
                report,
                ["invalid mint address format"],
            )

        # 2. Market data and pool selection. A Pump.fun token still on its bonding curve does
        # not need DexScreener: it is researched straight from the curve account on-chain.
        dex_error = None
        if self.p.dex is None:
            pairs = []
            dex_error = "DexScreener disabled"
        else:
            try:
                pairs = await self.p.dex.token_pairs(mint)
                report["sources"]["dexscreener"] = "OK"
            except ProviderError as e:
                report["sources"]["dexscreener"] = f"FAILED: {e}"
                pairs = []
                dex_error = f"market data unavailable: {e}"

        sel = select_pool(mint, pairs)

        report["pool"] = {
            "address": sel.pair.pair_address if sel.pair else None,
            "reason": sel.reason,
            "pools_for_mint": sel.candidates,
            "price_dispersion_pct": sel.price_dispersion_pct,
            "excluded_inconsistent": sel.inconsistent_pools,
            "inverted_pools_ignored": sel.inverted_pools,
        }

        pair = sel.pair

        if pair is None:
            curve = await self._probe_curve(mint, report)
            if curve is not None and not curve[1].get("complete"):
                return await self._research_pre_migration(
                    mint, report, cand, now,
                    dex_error or sel.reason, curve=curve, force=force,
                )

            if dex_error:
                return self._final(
                    mint,
                    Action.INSUFFICIENT_DATA,
                    report,
                    [dex_error],
                )

            sources = (cand or {}).get("sources") or []

            if isinstance(sources, str):
                try:
                    sources = json.loads(sources)
                except (TypeError, ValueError):
                    sources = []

            has_curve_evidence = bool(
                self.db.curve_history(mint, now - 300)
            )

            if (
                "pumpportal_new" in sources
                and "pumpportal_migration" not in sources
            ):
                return await self._research_pre_migration(
                    mint, report, cand, now, sel.reason
                )

            if (
                has_curve_evidence
                and "pumpportal_migration" not in sources
            ):
                return await self._research_pre_migration(
                    mint, report, cand, now, sel.reason
                )

            return self._not_ready(mint, report, [f"no usable pool: {sel.reason}"])

        report["identity"] = {
            "symbol": pair.base_symbol,
            "name": pair.base_name,
            "dex": pair.dex_id,
            "quote": pair.quote_symbol,
        }
        report["market"] = self._market_block(pair)

        dh = data_hash(pair)

        if (
            not force
            and cand
            and cand.get("state") == "REJECTED"
            and cand.get("last_data_hash") == dh
        ):
            return ResearchOutcome(
                mint,
                Action.REJECT,
                report,
                dh,
                pair,
                skipped_unchanged=True,
                reasons=["unchanged market data since last rejection"],
            )

        # 3. Cheap prefilters.
        cheap = []

        age_h = (
            (now - pair.pair_created_at) / 3600
            if pair.pair_created_at
            else None
        )

        if (
            s.max_token_age_hours
            and age_h is not None
            and age_h > s.max_token_age_hours
        ):
            cheap.append(
                f"pool age {age_h:.0f}h exceeds max "
                f"{s.max_token_age_hours:.0f}h"
            )

        if (pair.liquidity_usd or 0) < s.prefilter_min_liquidity_usd:
            cheap.append(
                f"liquidity ${pair.liquidity_usd or 0:,.0f} "
                f"below prefilter ${s.prefilter_min_liquidity_usd:,.0f}"
            )

        h1 = (
            (pair.txns["h1"]["buys"] or 0)
            + (pair.txns["h1"]["sells"] or 0)
        )

        if h1 < s.prefilter_min_h1_txns:
            cheap.append(f"only {h1} transactions in the last hour")

        if cheap:
            return self._final(
                mint,
                Action.REJECT,
                report,
                ["prefilter: " + reason for reason in cheap],
                pair=pair,
                dh=dh,
            )

        gates: list[GateResult] = []
        flags: list[Flag] = []

        # Price freshness.
        active = (
            (pair.txns["m5"]["buys"] or 0)
            + (pair.txns["m5"]["sells"] or 0)
            + h1
        )

        fresh = (
            pair.price_usd
            and active > 0
            and now - pair.fetched_at <= s.max_price_age_seconds
        )

        gates.append(
            GateResult(
                "price_fresh",
                GateStatus.PASS if fresh else GateStatus.FAIL,
                (
                    "current price with recent trades"
                    if fresh
                    else "no trustworthy current price"
                ),
            )
        )

        if (
            sel.price_dispersion_pct
            and sel.price_dispersion_pct
            > s.max_source_price_discrepancy_pct * 2
            and not sel.inconsistent_pools
        ):
            flags.append(
                Flag(
                    "POOL_PRICE_DISPERSION",
                    Severity.MEDIUM,
                    f"pools for this mint disagree on price by "
                    f"{sel.price_dispersion_pct:.0f}%",
                    DataStatus.OBSERVED,
                )
            )

        # 4. On-chain mint verification.
        mint_acc = None

        if self.p.rpc is not None:
            try:
                mint_acc = parse_mint_account(
                    await self.p.rpc.get_account_info(mint)
                )
                report["sources"]["rpc"] = "OK"
            except ProviderError as e:
                report["sources"]["rpc"] = f"FAILED: {e}"

        if mint_acc is None:
            gates.append(
                GateResult(
                    "mint_account",
                    GateStatus.UNKNOWN,
                    "on-chain mint account not verified (RPC unavailable)",
                )
            )

            from .analysis.identity import MintAccount
            from .models import Datum

            mint_acc = MintAccount(
                exists=Datum.unavailable("rpc"),
                program=Datum.unavailable("rpc"),
            )
            mint_acc.extensions = Datum.unavailable("rpc")
            mint_acc.mint_authority = Datum.unavailable("rpc")
            mint_acc.freeze_authority = Datum.unavailable("rpc")

        elif not mint_acc.verified_mint:
            return self._final(
                mint,
                Action.REJECT,
                report,
                [f"not a valid SPL mint: {mint_acc.exists.note}"],
                pair=pair,
                dh=dh,
            )

        else:
            gates.append(
                GateResult(
                    "mint_account",
                    GateStatus.PASS,
                    "verified SPL mint "
                    + (
                        "(Token-2022)"
                        if mint_acc.is_token_2022
                        else "(SPL Token)"
                    ),
                )
            )

        supply_ui = mint_acc.supply_ui.value

        # 5. RugCheck.
        rc = None

        if self.p.rugcheck is not None:
            try:
                rc = await self.p.rugcheck.report(mint)
                report["sources"]["rugcheck"] = "OK"
            except ProviderError as e:
                report["sources"]["rugcheck"] = f"FAILED: {e}"

        if supply_ui is None and rc:
            tok = rc.get("token") or {}

            try:
                supply_ui = int(tok["supply"]) / 10 ** int(tok["decimals"])
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                pass

        # 6. Security checks.
        f1, g1, auth_disp = check_authorities(mint_acc, s)
        f2, g2, ext_summary = check_extensions(mint_acc, s)

        if (
            mint_acc.mint_authority.status != DataStatus.VERIFIED
            and rc
            and "mintAuthority" in (rc.get("token") or {})
        ):
            ma = (rc.get("token") or {}).get("mintAuthority")
            fa = (rc.get("token") or {}).get("freezeAuthority")

            auth_disp["mint_authority"] = (
                ("ACTIVE" if ma else "REVOKED")
                + " (OBSERVED via RugCheck)"
            )
            auth_disp["freeze_authority"] = (
                ("ACTIVE" if fa else "REVOKED")
                + " (OBSERVED via RugCheck)"
            )

            for name, val in (
                ("mint_authority", ma),
                ("freeze_authority", fa),
            ):
                if val:
                    g1 = [g for g in g1 if g.name != name]
                    g1.append(
                        GateResult(
                            name,
                            GateStatus.FAIL,
                            f"{name} active per RugCheck",
                        )
                    )

        f3, g3 = rugcheck_findings(rc, s, pair.price_usd, supply_ui)
        history = self.db.market_history(mint, now - 24 * 3600)
        la = assess_liquidity(pair, rc, history, s, now)
        await self._verify_lp_onchain(pair, mint, la, report)
        await self._resolve_lp_floor(pair, pairs, mint, rc, la, report)
        self._reconcile_rugcheck_lp(la, g3, f3, report)
        await self._sellability(mint, mint_acc, pair, report)

        gates += g1 + g2 + g3 + la.gates
        flags += f1 + f2 + f3 + la.flags

        report["security"] = {
            **auth_disp,
            "token_program": mint_acc.program.value,
            "token_2022": mint_acc.is_token_2022,
            "extensions": ext_summary,
            "lp_status": (
                la.lp_status.value
                or f"UNKNOWN ({la.lp_status.status.value})"
            ),
            "liquidity_to_mcap": la.liq_to_mcap,
            "liquidity_to_fdv": la.liq_to_fdv,
            "pool_age_hours": la.pool_age_hours,
            "liquidity_drop_from_recent_max_pct": (
                la.drop_from_recent_max_pct
            ),
            "volume_to_liquidity_h1": la.vol_to_liq_h1,
        }

        if any(g.status == GateStatus.FAIL for g in gates):
            return self._finish(
                mint,
                pair,
                dh,
                report,
                gates,
                flags,
                partial=True,
            )

        # 7. Holders.
        creator, creator_src = None, ""

        if rc and rc.get("creator"):
            creator, creator_src = rc["creator"], "rugcheck"
        elif cand:
            creator = self._creator_from_discovery(mint)
            creator_src = "pump.fun launch event" if creator else ""

        # every pool of this mint is a pool account, not a holder — v2.4 only excluded the
        # selected pool, so with RugCheck down a second pool's vault read as a whale
        own_pools = [p.pair_address for p in pairs if p.base_mint == mint and p.pair_address]
        ha = await self._holders(mint, pair, rc, supply_ui, creator, extra_pools=own_pools)
        gates += ha.gates
        flags += ha.flags

        report["holders"] = ha.to_dict()

        # 8. Trades and wallet tracing. Swaps are decoded from PumpSwap events, so when the
        # deepest pool is a DLMM/CLMM pool the sample is taken from the token's PumpSwap pool.
        trade_pool = self._trade_pool(pair, pairs, mint)
        if trade_pool != pair.pair_address:
            report["trade_pool"] = {"address": trade_pool,
                                    "reason": "deepest pool is not decodable; sampled the PumpSwap pool"}
        ts, cr = await self._wallets(
            mint, pair, rc, ha, supply_ui, creator, age_h, trade_pool=trade_pool
        )

        wf, wg = W.wallet_findings(ts, cr, h1, s)
        gates += wg
        flags += wf

        report["trades"] = ts.to_dict()
        report["clusters"] = cr.to_dict()

        # 9. Creator history.
        ch = await analyze_creator(
            mint,
            creator,
            creator_src,
            rc,
            self.p.dex,
            self.p.rugcheck,
            self.db,
        )

        flags += ch.flags
        report["creator"] = ch.to_dict()

        # 10. Market structure.
        ms = analyze_market(pair, history, s)
        flags += ms.flags
        report["market_structure"] = ms.to_dict()

        # 11. Narrative.
        nr = await self._narrative(mint, pair)
        flags += nr.flags
        report["narrative"] = nr.to_dict()

        # 12. Score.
        comps = [
            nr.component,
            score_demand(pair, ts, wf, s.w_demand),
            score_liquidity(pair, la, s, s.w_liquidity),
            score_holders(ha, ch, s.w_holders),
            score_momentum(pair, ms, s.w_momentum),
            score_wallet_quality(cr, s.w_wallet_quality),
        ]

        complete = evidence_completeness(
            {
                "mint_verified": mint_acc.verified_mint,
                "price_fresh": bool(fresh),
                "liquidity": bool(pair.liquidity_usd),
                "market_cap": pair.market_cap is not None,
                "holders": ha.status.usable,
                "trade_sample": ts.status == DataStatus.VERIFIED,
                "rugcheck": rc is not None,
                "creator": ch.status != DataStatus.UNKNOWN,
                "lp_status": la.lp_status.status.usable,
                "trending_metas": nr.meta_status == DataStatus.OBSERVED,
                "social": nr.social_status == DataStatus.OBSERVED,
                "wallet_tracing": cr.status != DataStatus.UNAVAILABLE,
            }
        )

        caveats = self._caveats(ts, ha, nr, ms)
        sr = build_score(
            comps,
            complete,
            nr.confidence,
            s.w_evidence,
            caveats,
        )

        report["score"] = sr.to_dict()

        return self._finish(
            mint,
            pair,
            dh,
            report,
            gates,
            flags,
            extras={"ms": ms, "nr": nr, "sr": sr},
        )

    # ------------------------------------------------------------------
    # Pump.fun bonding-curve decoding
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_pump_curve_account(
        account: Optional[dict],
    ) -> Optional[dict]:
        """Decode a documented Pump.fun bonding-curve account."""
        if not isinstance(account, dict):
            return None

        if account.get("owner") != PUMP_FUN_PROGRAM:
            return None

        data = account.get("data")

        if (
            not isinstance(data, (list, tuple))
            or not data
            or not isinstance(data[0], str)
        ):
            return None

        try:
            raw = base64.b64decode(data[0], validate=True)
        except (ValueError, TypeError):
            return None

        if len(raw) < 81 or raw[:8] != PUMP_CURVE_DISCRIMINATOR:
            return None

        try:
            (
                virtual_tokens,
                virtual_sol,
                real_tokens,
                real_sol,
                total_supply,
            ) = struct.unpack_from("<QQQQQ", raw, 8)

            complete = raw[48] == 1

        except (struct.error, IndexError):
            return None

        if (
            not virtual_tokens
            or not virtual_sol
            or not total_supply
            or raw[48] not in (0, 1)
        ):
            return None

        # Pump.fun curves are SOL-quoted, and the caller has already checked that this account
        # belongs to the pump.fun program. An earlier version inferred the quote asset by
        # requiring the account's own lamports to cover `real_sol_reserves`; that was wrong.
        # Checked on-chain (curve CgfbhqzqSHoygUwdywzRo8o9wZEbLtRAVYw8fXkPjiaF, 19 Oct 2026):
        # the account held 1,493,520 lamports of rent while real_sol_reserves read 870,873,131,
        # because the reserves are not kept as the curve account's lamports. Every curve with
        # real buying therefore looked "not SOL-backed" and was dropped as INSUFFICIENT DATA —
        # exactly the coins the early-call path exists to find.
        # A non-SOL quote is honoured only when the optional quote-mint field decodes to a mint
        # we actually recognise; random bytes in a newer layout never match one.
        quote_mint = WSOL_MINT
        if len(raw) >= 115:
            field = b58encode(raw[83:115])
            if field in QUOTE_MINTS and field != WSOL_MINT:
                quote_mint = field
        sol_backed = quote_mint == WSOL_MINT

        # Reserves of a non-SOL curve are in another token's units: never report them as SOL.
        market_cap_sol = (
            (total_supply * virtual_sol / virtual_tokens) / 1_000_000_000
            if sol_backed
            else None
        )

        # Creator (offset 49..81 in current curve accounts); absent/zero on very old layouts.
        creator = None
        if len(raw) >= 81 and any(raw[49:81]):
            creator = b58encode(raw[49:81])

        return {
            "creator": creator,
            "quote_mint": quote_mint,
            "quote_is_sol": sol_backed,
            "virtual_token_reserves_raw": virtual_tokens,
            "virtual_sol_reserves_lamports": virtual_sol,
            "real_token_reserves_raw": real_tokens,
            "real_sol_reserves_lamports": real_sol,
            "token_total_supply_raw": total_supply,
            "complete": complete,
            "market_cap_sol": market_cap_sol,
        }

    async def _probe_curve(self, mint: str, report: dict) -> Optional[tuple[str, dict]]:
        """Find and verify this mint's Pump.fun bonding curve on-chain (no DexScreener needed).

        Returns (curve_address, parsed_state) only when the account exists, is owned by the
        Pump.fun program and has the bonding-curve discriminator.
        """
        if self.p.rpc is None:
            return None
        key = pump_bonding_curve_address(mint)
        if not key:
            return None
        try:
            state = self._parse_pump_curve_account(await self.p.rpc.get_account_info(key))
        except ProviderError as e:
            report["sources"]["pump_fun_curve_rpc"] = f"FAILED: {e}"
            return None
        if state is None:
            return None
        report["sources"]["pump_fun_curve_rpc"] = "OK (derived from mint)"
        return key, state

    async def _curve_holders(self, mint: str, mint_acc, curve_key: Optional[str],
                             creator: Optional[str]) -> H.HolderAnalysis:
        """Top-20 holders via RPC; the bonding curve's own token vault is excluded like a pool."""
        supply_ui = mint_acc.supply_ui.value if mint_acc is not None else None
        holders, status, src = [], DataStatus.UNAVAILABLE, "none"
        if supply_ui:
            try:
                largest = await self.p.rpc.get_token_largest_accounts(mint)
                owners = await self.p.rpc.get_multiple_accounts(
                    [a["address"] for a in largest if a.get("address")])
                holders = H.build_holders_from_rpc(largest, owners, supply_ui)
                status, src = DataStatus.VERIFIED, "solana-rpc"
            except ProviderError as e:
                log.info("pre-migration holders unavailable for %s: %s", mint, e)
        return H.analyze_holders(holders, status, src, self.s, pair_address=curve_key,
                                 creator=creator, rc=None, supply_ui=supply_ui, share_of_total=True)

    async def _onchain_identity(self, mint: str, mint_value: Optional[dict]) -> dict:
        """Name/symbol from the chain: Token-2022 metadata extension, else Metaplex metadata."""
        info = (((mint_value or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        for ext in info.get("extensions") or []:
            if isinstance(ext, dict) and ext.get("extension") == "tokenMetadata":
                st = ext.get("state") or {}
                if st.get("name") or st.get("symbol"):
                    return {"name": st.get("name"), "symbol": st.get("symbol"), "uri": st.get("uri")}
        meta_key = metaplex_metadata_address(mint)
        if self.p.rpc is None or not meta_key:
            return {}
        try:
            acc = await self.p.rpc.get_account_info(meta_key)
        except ProviderError:
            return {}
        data = (acc or {}).get("data")
        if isinstance(data, (list, tuple)) and data and isinstance(data[0], str):
            try:
                return parse_metaplex_metadata(base64.b64decode(data[0])) or {}
            except (ValueError, TypeError):
                return {}
        return {}

    # ------------------------------------------------------------------
    # Pre-migration research
    # ------------------------------------------------------------------

    async def _research_pre_migration(
        self,
        mint: str,
        report: dict,
        cand: Optional[dict],
        now: float,
        pool_reason: str,
        curve: Optional[tuple[str, dict]] = None,
        force: bool = False,
    ) -> ResearchOutcome:
        """Research a Pump.fun token before it has a usable DEX pool.

        RPC-inferred candidates are diagnostic evidence only unless the
        wallet parser independently verifies the swap. Incomplete holder
        and liquidity information can never be treated as clean security.
        """
        since = now - 300
        events = self.db.curve_history(mint, since)

        launch_row = self.db.fetchone(
            "SELECT payload FROM discovery_events "
            "WHERE mint=? AND source='pumpportal_new' "
            "ORDER BY ts ASC LIMIT 1",
            (mint,),
        )

        try:
            launch_payload = (
                json.loads(launch_row.get("payload") or "{}")
                if launch_row
                else {}
            )
        except (TypeError, ValueError):
            launch_payload = {}

        bonding_curve_key = launch_payload.get("bondingCurveKey")
        curve_state = None

        if curve is None:
            curve = await self._probe_curve(mint, report)
        if curve is not None:
            # The derived, on-chain-verified curve wins over a launch payload's key.
            bonding_curve_key, curve_state = curve
        elif self.p.rpc is not None and bonding_curve_key:
            try:
                curve_account = await self.p.rpc.get_account_info(
                    str(bonding_curve_key)
                )

                curve_state = self._parse_pump_curve_account(
                    curve_account
                )

                report["sources"]["pump_fun_curve_rpc"] = (
                    "OK"
                    if curve_state
                    else "UNAVAILABLE / account layout not verified"
                )

            except ProviderError as e:
                report["sources"]["pump_fun_curve_rpc"] = f"FAILED: {e}"

        trades = [
            e
            for e in events
            if str(e.get("tx_type") or "").lower() in {"buy", "sell"}
        ]

        stream_rows = list(trades)
        trade_source = "PumpPortal live trade stream"
        rpc_diagnostics: dict = {
            "attempted": False,
            "signatures_seen": 0,
            "signatures_in_window": 0,
            "verified_trades": 0,
            "accepted_recent_rows": 0,
            "window_covered": None,
            "sample_mode": "recent",
            "counters": {},
            "note": "not attempted",
        }

        # Free RPC fallback.
        #
        # Only trades decoded from a Pump.fun TradeEvent and confirmed against the
        # transaction's own token balances are accepted (Trade.verification ==
        # VERIFIED_PUMP_EVENT). Fetched transactions that could not be decoded are counted in the
        # diagnostics and are NEVER converted into trades. The sampler discards signatures
        # outside the 5-minute window before fetching, so quiet tokens cost one RPC call.
        if (
            len(trades) < self.s.min_curve_trade_events
            and self.p.rpc is not None
            and bonding_curve_key
        ):
            rpc_diagnostics["attempted"] = True
            rpc_diagnostics["note"] = "RPC recent-signature sampling started"

            try:
                sample = await W.sample_curve_trades(
                    self.p.rpc,
                    str(bonding_curve_key),
                    mint,
                    max(100, self.s.min_curve_trade_events * 4),
                    min_block_time=since,
                )

                rpc_diagnostics["signatures_seen"] = sample.signatures_examined
                rpc_diagnostics["signatures_in_window"] = sample.signatures_in_window
                rpc_diagnostics["verified_trades"] = len(sample.trades)
                rpc_diagnostics["window_covered"] = sample.window_covered
                rpc_diagnostics["counters"] = {
                    k: v for k, v in sample.counters.items() if v
                }

                rpc_rows = []

                for t in sample.trades:
                    if getattr(t, "verification", None) != W.VERIFIED_PUMP_EVENT:
                        continue

                    block_time = getattr(t, "block_time", None)

                    if not block_time:
                        continue

                    try:
                        timestamp = float(block_time)
                    except (TypeError, ValueError):
                        continue

                    # Only accept transactions inside the current five-minute window.
                    # Reject implausibly future timestamps.
                    if timestamp < since or timestamp > now + 30:
                        continue

                    side = str(getattr(t, "side", "") or "").lower()

                    if side not in {"buy", "sell"}:
                        continue

                    wallet = getattr(t, "wallet", None)

                    if not wallet:
                        continue

                    rpc_rows.append(
                        {
                            "tx_type": side,
                            "wallet": wallet,
                            "sol_amount": getattr(t, "sol_amount", None),
                            "token_amount": getattr(t, "token_amount", None),
                            "ts": timestamp,
                            "signature": getattr(t, "signature", None),
                            "market_cap_sol": None,
                            "source": "solana_rpc_pump_trade_event",
                            "verification": W.VERIFIED_PUMP_EVENT,
                        }
                    )

                rpc_diagnostics["accepted_recent_rows"] = len(rpc_rows)
                rpc_diagnostics["note"] = sample.describe()

                # Merge with any stored stream rows, de-duplicated by signature so the same
                # transaction is never counted twice.
                merged: dict = {}
                anonymous = []
                for row in stream_rows:
                    sig = row.get("signature")
                    if sig:
                        merged[sig] = row
                    else:
                        anonymous.append(row)
                for row in rpc_rows:
                    merged.setdefault(row["signature"] or id(row), row)
                combined = list(merged.values()) + anonymous

                if len(combined) > len(trades):
                    if stream_rows and rpc_rows:
                        trade_source = "PumpPortal stream + Solana RPC verified Pump.fun trades"
                    else:
                        trade_source = "Solana RPC verified Pump.fun trades"
                    trades = combined

                report["sources"]["pump_fun_trade_sample"] = (
                    "OK"
                    if rpc_rows
                    else "INCOMPLETE — no verified recent trades"
                )

            except ProviderError as e:
                rpc_diagnostics["note"] = f"RPC sampling failed: {e}"
                report["sources"]["pump_fun_trade_sample"] = f"FAILED: {e}"

            except (TypeError, ValueError, AttributeError) as e:
                # Keep the bot running if the sampler returns an unexpected object shape.
                rpc_diagnostics["note"] = (
                    f"unexpected RPC sample result: {type(e).__name__}: {e}"
                )
                report["sources"]["pump_fun_trade_sample"] = (
                    "UNAVAILABLE — unexpected parser result"
                )

        report["pre_migration_rpc_diagnostics"] = rpc_diagnostics

        buys = [
            e
            for e in trades
            if str(e.get("tx_type") or "").lower() == "buy"
        ]
        sells = [
            e
            for e in trades
            if str(e.get("tx_type") or "").lower() == "sell"
        ]

        buyers = {
            e.get("wallet")
            for e in buys
            if e.get("wallet")
        }
        sellers = {
            e.get("wallet")
            for e in sells
            if e.get("wallet")
        }

        def total(rows, key):
            result = 0.0

            for row in rows:
                value = row.get(key)

                if value is None:
                    continue

                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue

                if number >= 0:
                    result += number

            return result

        buy_sol = total(buys, "sol_amount")
        sell_sol = total(sells, "sol_amount")
        # Buys whose SOL amount is missing are UNMEASURED, not zero SOL. In the live log of
        # 2026-10-19 a curve with 79 verified trades was rejected for "buy flow 0.00 SOL".
        buys_with_sol = sum(1 for e in buys if isinstance(e.get("sol_amount"), (int, float))
                            and e.get("sol_amount") > 0)
        buy_flow_measured = (not buys) or buys_with_sol > 0
        buy_flow_note = (None if buy_flow_measured else
                         f"UNMEASURED — {len(buys)} buy(s) carried no SOL amount")
        if buys and 0 < buys_with_sol < len(buys):
            buy_flow_note = f"PARTIAL — {buys_with_sol} of {len(buys)} buys carried a SOL amount (lower bound)"
        ratio = len(buys) / max(1, len(sells))

        caps = []

        for event in events:
            value = event.get("market_cap_sol")

            if value is None:
                continue

            try:
                value = float(value)
            except (TypeError, ValueError):
                continue

            if value > 0:
                caps.append(value)

        launch_cap = launch_payload.get("marketCapSol")

        try:
            launch_cap = (
                float(launch_cap)
                if launch_cap is not None and float(launch_cap) > 0
                else None
            )
        except (TypeError, ValueError):
            launch_cap = None

        current_cap = (
            curve_state.get("market_cap_sol")
            if curve_state
            else (caps[-1] if caps else None)
        )

        if len(caps) >= 2:
            first_cap = caps[0]
            trend_window = "observed recent trade snapshots (up to 5m)"
        else:
            first_cap = launch_cap
            trend_window = (
                "since launch (launch snapshot vs current on-chain curve)"
                if launch_cap and current_cap
                else "unavailable"
            )

        change_pct = (
            ((current_cap / first_cap) - 1) * 100
            if first_cap and current_cap
            else None
        )

        latest = next(
            (
                event
                for event in reversed(events)
                if event.get("market_cap_sol") or event.get("v_sol_curve")
            ),
            {},
        )

        if curve_state:
            latest = {
                **latest,
                "v_sol_curve": (
                    curve_state.get("virtual_sol_reserves_lamports", 0)
                    / 1e9
                ),
                "v_tokens_curve": curve_state.get(
                    "virtual_token_reserves_raw"
                ),
                "curve_complete": curve_state.get("complete"),
            }

        report["lifecycle"] = "PRE_MIGRATION_BONDING_CURVE"
        report["pool"] = {
            "address": None,
            "reason": pool_reason,
            "pools_for_mint": 0,
            "price_dispersion_pct": None,
            "excluded_inconsistent": [],
            "inverted_pools_ignored": 0,
        }

        profile = self._profile(mint) or {}

        report["pool"]["bonding_curve"] = bonding_curve_key
        known = cand or self.db.get_candidate(mint) or {}
        report["identity"] = {
            "symbol": known.get("symbol") or "?",
            "name": known.get("name") or "Unknown launch",
            "dex": "Pump.fun bonding curve",
            "quote": "SOL",
            "image_url": (
                (cand or {}).get("icon")
                or profile.get("icon")
            ),
        }

        report["pre_migration"] = {
            "source": trade_source if trades else "UNAVAILABLE",
            "data_status": "OBSERVED" if trades else "UNAVAILABLE",
            "sample_window_seconds": 300,
            "trade_events": len(trades),
            "buys": len(buys),
            "sells": len(sells),
            "unique_buyers": len(buyers),
            "unique_sellers": len(sellers),
            "buy_sell_count_ratio": round(ratio, 3),
            "buy_sol_5m": round(buy_sol, 6) if buy_flow_measured else None,
            "buy_flow_status": buy_flow_note or "MEASURED",
            "sell_sol_5m": round(sell_sol, 6),
            "market_cap_sol_start": first_cap,
            "market_cap_sol_current": current_cap,
            "market_cap_change_pct": (
                round(change_pct, 2)
                if change_pct is not None
                else None
            ),
            "market_cap_trend_window": trend_window,
            "v_sol_in_bonding_curve": latest.get("v_sol_curve"),
            "v_tokens_in_bonding_curve_raw": latest.get("v_tokens_curve"),
            "curve_complete": latest.get("curve_complete"),
            "curve_progress_pct": None,
            "curve_progress_note": (
                "UNAVAILABLE: no validated graduation-progress denominator "
                "in the observed payload"
            ),
            "holder_risk": "UNKNOWN until holder data is available",
            "lp_risk": "NOT_APPLICABLE before migration",
            "risk_note": (
                "INCOMPLETE: early signal only; holder concentration and "
                "post-migration liquidity are not yet assessed"
            ),
        }

        # Dollar values for display. Thresholds and multiples stay in SOL (the curve's own unit).
        sol_usd = None
        if self.p.sol_price is not None:
            try:
                sol_usd = await self.p.sol_price.get()
            except Exception:  # noqa: BLE001 - display-only enrichment
                sol_usd = None
        pm = report["pre_migration"]
        pm["sol_usd"] = sol_usd
        pm["market_cap_usd_current"] = (current_cap * sol_usd
                                        if sol_usd and isinstance(current_cap, (int, float)) else None)
        pm["buy_usd_5m"] = round(buy_sol * sol_usd, 2) if sol_usd and buy_flow_measured else None

        if curve_state and curve_state.get("quote_is_sol") is False:
            report["pre_migration"]["quote_mint"] = curve_state.get("quote_mint")
            return self._final(
                mint,
                Action.INSUFFICIENT_DATA,
                report,
                [
                    "bonding curve is not SOL-backed (quote asset "
                    f"{curve_state.get('quote_mint')}); SOL-denominated "
                    "market cap and flow thresholds cannot be applied"
                ],
                risk=RiskLevel.INCOMPLETE,
            )

        if curve_state and curve_state.get("complete"):
            report["pre_migration"]["curve_progress_note"] = (
                "Curve account reports complete; migration may be in progress"
            )

            return self._final(
                mint,
                Action.INSUFFICIENT_DATA,
                report,
                [
                    "bonding curve is complete but no usable DEX pool is "
                    "available yet; retrying"
                ],
                risk=RiskLevel.INCOMPLETE,
            )

        # Verify the mint and authorities before early-watch consideration.
        security_gates = []
        security_flags = []
        mint_acc = None

        mint_value = None
        if self.p.rpc is not None:
            try:
                mint_value = await self.p.rpc.get_account_info(mint)
                mint_acc = parse_mint_account(mint_value)
                report["sources"]["rpc"] = "OK"
            except ProviderError as e:
                report["sources"]["rpc"] = f"FAILED: {e}"

        # Name/symbol straight from the chain when discovery did not provide them.
        ident = report["identity"]
        onchain: dict = {}
        if mint_acc is not None and mint_acc.verified_mint and (ident["symbol"] == "?" or
                                                                ident["name"] == "Unknown launch"):
            onchain = await self._onchain_identity(mint, mint_value)
            if onchain.get("symbol") and ident["symbol"] == "?":
                ident["symbol"] = str(onchain["symbol"])[:32]
            if onchain.get("name") and ident["name"] == "Unknown launch":
                ident["name"] = str(onchain["name"])[:80]
            if onchain:
                report["sources"]["onchain_metadata"] = "OK"

        if mint_acc is None or not mint_acc.verified_mint:
            report["security"] = {
                "mint_account": "UNKNOWN — on-chain mint verification unavailable",
                "mint_authority": "UNKNOWN",
                "freeze_authority": "UNKNOWN",
            }

            report["missing"] = [
                "verified SPL mint",
                "holder concentration",
                "creator history",
                "post-migration liquidity",
            ]

            return self._final(
                mint,
                Action.INSUFFICIENT_DATA,
                report,
                [
                    "pre-migration token has no DEX pool; waiting for enough "
                    "curve data and verified mint"
                ],
                risk=RiskLevel.INCOMPLETE,
            )

        f1, g1, auth_disp = check_authorities(mint_acc, self.s)
        f2, g2, ext_summary = check_extensions(mint_acc, self.s)

        security_gates.extend(g1 + g2)
        security_flags.extend(f1 + f2)

        # Holder risk straight from the chain. Run for tokens with real activity, or on request;
        # skipped for quiet launches to keep RPC usage low.
        creator = (curve_state or {}).get("creator") or self._creator_from_discovery(mint)
        holder_status = "UNKNOWN (not assessed: too little activity)"
        if self.p.rpc is not None and (force or len(trades) >= self.s.min_curve_trade_events):
            ha = await self._curve_holders(mint, mint_acc, bonding_curve_key, creator)
            report["holders"] = ha.to_dict()
            security_gates.extend(ha.gates)
            security_flags.extend(ha.flags)
            if ha.status.usable:
                holder_status = (
                    f"{ha.status.value}: top-10 {ha.top10_adj:.1f}% / largest {ha.max_single_adj:.1f}% "
                    "of total supply (bonding curve excluded)"
                    + (f"; creator {ha.creator_pct:.2f}%" if ha.creator_pct is not None else "")
                )
                report["pre_migration"]["holder_risk"] = holder_status
            else:
                holder_status = "UNAVAILABLE"
        report["pre_migration"]["creator"] = creator

        # Free social check (token's own links, recycled-link detection, public Telegram count).
        social_ev = None
        if self.p.socials is not None and (force or len(trades) >= self.s.min_curve_trade_events):
            uri = onchain.get("uri") if onchain else None
            if not uri:
                uri = (await self._onchain_identity(mint, mint_value)).get("uri")
            social_ev = await self._free_social(mint, uri=uri)
            if social_ev is not None:
                report["pre_migration"]["socials"] = social_ev.to_dict()
            desc = self._descriptions.pop(mint, None)
            if desc:
                report["identity"]["description"] = desc

        report["security"] = {
            **auth_disp,
            "token_program": mint_acc.program.value,
            "token_2022": mint_acc.is_token_2022,
            "extensions": ext_summary,
            "lp_status": "NOT_APPLICABLE — pre-migration",
            "holder_status": holder_status,
        }

        report["gates"] = [g.to_dict() for g in security_gates]
        report["flags"] = [f.to_dict() for f in security_flags]

        if any(
            gate.status == GateStatus.FAIL
            for gate in security_gates
        ):
            return self._final(
                mint,
                Action.REJECT,
                report,
                [
                    f"pre-migration security gate failed: "
                    f"{gate.name} — {gate.reason}"
                    for gate in security_gates
                    if gate.status == GateStatus.FAIL
                ],
                risk=RiskLevel.ELEVATED,
            )

        # Do not treat inferred or unverified transactions as proof of demand.
        verified_source = trade_source in {
            "PumpPortal live trade stream",
            "PumpPortal stream + Solana RPC verified Pump.fun trades",
            "Solana RPC verified Pump.fun trades",
        }

        event_score = (
            min(
                1.0,
                len(trades) / max(1, self.s.min_curve_trade_events),
            )
            * 10
        )

        buyer_score = (
            min(
                1.0,
                len(buyers) / max(1, self.s.min_curve_unique_buyers),
            )
            * 25
        )

        ratio_score = (
            min(
                1.0,
                ratio / max(1.0, self.s.min_curve_buy_sell_ratio),
            )
            * 20
        )

        sol_score = (
            min(
                1.0,
                buy_sol / max(0.01, self.s.min_curve_buy_sol_5m),
            )
            * 20
        )

        chase_limit = float(self.s.max_curve_chase_pct)

        if change_pct is None:
            momentum_score = 0.0
        elif change_pct <= 100.0:
            # Unchanged below +100%: 12.5 at flat, full 25 from +25% to +100%.
            momentum_score = max(0.0, min(25.0, 12.5 + change_pct / 2))
        elif change_pct >= chase_limit:
            momentum_score = 0.0
        else:
            # A token already up several-fold since launch is a late entry, not a stronger
            # signal: taper linearly from 25 points at +100% to 0 at the chase limit.
            momentum_score = 25.0 * (chase_limit - change_pct) / max(1.0, chase_limit - 100.0)

        extended = change_pct is not None and change_pct > chase_limit

        score = round(
            event_score
            + buyer_score
            + ratio_score
            + sol_score
            + momentum_score,
            1,
        )

        report["pre_migration"]["opportunity_score"] = score
        report["pre_migration"]["score_components"] = {
            "trade_activity": round(event_score, 1),
            "unique_buyers": round(buyer_score, 1),
            "buy_sell_pressure": round(ratio_score, 1),
            "buy_sol_flow": round(sol_score, 1),
            "market_cap_momentum": round(momentum_score, 1),
        }

        report["score"] = {
            "total": score,
            "confidence": (
                "MEDIUM"
                if len(trades) >= self.s.min_curve_trade_events
                else "LOW"
            ),
            "method": (
                "pre-migration activity heuristic; "
                "not validated as predictive"
            ),
        }

        report["risk"] = RiskLevel.INCOMPLETE.value
        report["missing"] = [
            ("wallet clusters" if report["pre_migration"].get("holder_risk")
             else "holder concentration and wallet clusters"),
            "verified creator history",
            "post-migration pool depth / LP status",
            "USD price and USD market cap",
        ]

        qualified = (
            verified_source
            and len(trades) >= self.s.min_curve_trade_events
            and len(buyers) >= self.s.min_curve_unique_buyers
            and ratio >= self.s.min_curve_buy_sell_ratio
            and buy_sol >= self.s.min_curve_buy_sol_5m
            and change_pct is not None
            and change_pct > 0
            and not extended
            and score >= self.s.min_curve_early_score
            and not (social_ev is not None and social_ev.recycled)
        )

        report["pre_migration"]["qualified_early_watch"] = bool(qualified)
        report["pre_migration"]["extended_move"] = bool(extended)
        report["pre_migration"]["trade_verification"] = (
            "VERIFIED_PUMP_EVENT (RPC) and/or provider stream; unverified candidates are never counted"
        )

        report["pre_migration"]["qualification_reasons"] = [
            f"{len(trades)} trade events / {len(buyers)} unique buyers in 5m",
            f"buy/sell count ratio {ratio:.2f}",
            (f"buy flow {buy_sol:.2f} SOL in 5m" if buy_flow_measured else "buy flow not measured"),
            (
                f"market-cap change {change_pct:+.1f}% in SOL terms "
                f"({trend_window})"
                if change_pct is not None
                else "market-cap trend unavailable"
            ),
        ]

        if (
            not trades
            or len(trades) < self.s.min_curve_trade_events
        ):
            return self._not_ready(
                mint, report,
                [f"pre-migration trade sample incomplete "
                 f"({len(trades)}/{self.s.min_curve_trade_events}); "
                 f"{rpc_diagnostics['note']}"],
                score=score)

        if not verified_source:
            return self._final(
                mint,
                Action.INSUFFICIENT_DATA,
                report,
                [
                    "RPC data did not establish a trustworthy decoded swap "
                    "sample; refusing to qualify an early-watch alert"
                ],
                score=score,
                risk=RiskLevel.INCOMPLETE,
                confidence=Confidence.LOW,
            )

        if qualified:
            return self._final(
                mint,
                Action.WATCHLIST,
                report,
                [
                    "early-watch criteria met; full holder/liquidity risk "
                    "remains incomplete"
                ],
                score=score,
                risk=RiskLevel.INCOMPLETE,
                confidence=Confidence.MEDIUM,
            )

        # Name every threshold that failed, with the observed value, so logs are actionable.
        misses = []
        if len(buyers) < self.s.min_curve_unique_buyers:
            misses.append(f"unique buyers {len(buyers)} < {self.s.min_curve_unique_buyers}")
        if ratio < self.s.min_curve_buy_sell_ratio:
            misses.append(f"buy/sell ratio {ratio:.2f} < {self.s.min_curve_buy_sell_ratio:.2f}")
        # Unmeasured inputs are kept apart from thresholds that were really missed: a coin whose
        # only problem is a gap in the data is re-checked (INSUFFICIENT DATA), not rejected.
        unmeasured = []
        if not buy_flow_measured:
            unmeasured.append(f"buy flow not measured ({buy_flow_note})")
        elif buy_sol < self.s.min_curve_buy_sol_5m:
            misses.append(f"buy flow {buy_sol:.2f} SOL < {self.s.min_curve_buy_sol_5m:.2f} SOL in 5m")
        if change_pct is None:
            unmeasured.append("market-cap trend not measured (no launch or earlier curve snapshot)")
        elif change_pct <= 0:
            misses.append(f"market cap {change_pct:+.1f}% (not rising)")
        if score < self.s.min_curve_early_score and not unmeasured:
            # with inputs unmeasured the activity score is understated, so it is not a miss
            misses.append(f"activity score {score:.1f} < {self.s.min_curve_early_score:.0f}")
        if social_ev is not None and social_ev.recycled:
            misses.append("recycled socials (" + ", ".join(social_ev.recycled)
                          + " also used by other recent tokens) — serial-launcher pattern")

        if unmeasured and not misses and not extended:
            return self._final(
                mint,
                Action.INSUFFICIENT_DATA,
                report,
                ["pre-migration evidence incomplete, re-checking: " + "; ".join(unmeasured)],
                score=score,
                risk=RiskLevel.INCOMPLETE,
                confidence=Confidence.LOW,
            )

        misses += unmeasured
        reject_reasons = (
            [
                "pre-migration activity thresholds not met"
                + (": " + "; ".join(misses) if misses else "")
                + "; continue monitoring for new evidence"
            ]
            if misses or not extended
            else []
        )

        if extended:
            reject_reasons.insert(
                0,
                f"market cap already {change_pct:+.0f}% since launch "
                f"(limit {chase_limit:.0f}%): entry would be late",
            )

        return self._final(
            mint,
            Action.REJECT,
            report,
            reject_reasons,
            score=score,
            risk=RiskLevel.INCOMPLETE,
            confidence=Confidence.LOW,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _market_block(self, p: PairInfo) -> dict:
        def d(v, src="dexscreener"):
            return {
                "value": v,
                "status": "OBSERVED" if v is not None else "UNAVAILABLE",
                "source": src,
            }

        return {
            "price_usd": d(p.price_usd),
            "liquidity_usd": d(p.liquidity_usd),
            "market_cap": d(p.market_cap)
            | (
                {"note": "provider field"}
                if p.market_cap is not None
                else {
                    "note": (
                        "not provided; FDV shown separately and never "
                        "as market cap"
                    )
                }
            ),
            "fdv": d(p.fdv),
            "volume": {
                k: d(v) for k, v in p.vol.items()
            }
            | {
                "m15": {
                    "value": None,
                    "status": "UNAVAILABLE",
                    "source": "dexscreener",
                    "note": "DexScreener provides no 15m window",
                }
            },
            "txns": p.txns,
            "price_change": p.price_change,
            "pair_created_at": p.pair_created_at,
            "fetched_at": p.fetched_at,
            "url": p.url,
            "image_url": p.image_url,
            "boosts_active": p.boosts_active,
        }

    def _creator_from_discovery(self, mint: str) -> Optional[str]:
        row = self.db.fetchone(
            "SELECT payload FROM discovery_events "
            "WHERE mint=? AND source='pumpportal_new' "
            "ORDER BY ts LIMIT 1",
            (mint,),
        )

        if not row:
            return None

        try:
            return json.loads(row["payload"]).get("traderPublicKey")
        except (TypeError, ValueError):
            return None

    def _profile(self, mint: str) -> Optional[dict]:
        row = self.db.fetchone(
            "SELECT payload FROM discovery_events "
            "WHERE mint=? AND source IN "
            "('dexscreener_profile','dexscreener_cto') "
            "ORDER BY ts DESC LIMIT 1",
            (mint,),
        )

        if not row:
            return None

        try:
            return json.loads(row["payload"])
        except (TypeError, ValueError):
            return None

    async def _holders(
        self,
        mint: str,
        pair: PairInfo,
        rc: Optional[dict],
        supply_ui: Optional[float],
        creator: Optional[str],
        extra_pools: Optional[list[str]] = None,
    ) -> H.HolderAnalysis:
        holders, status, src = [], DataStatus.UNAVAILABLE, "none"

        if self.p.rpc is not None and supply_ui:
            try:
                largest = await self.p.rpc.get_token_largest_accounts(mint)
                owners = await self.p.rpc.get_multiple_accounts(
                    [
                        account["address"]
                        for account in largest
                        if account.get("address")
                    ]
                )

                holders = H.build_holders_from_rpc(
                    largest, owners, supply_ui
                )
                status, src = DataStatus.VERIFIED, "solana-rpc"

            except ProviderError as e:
                log.info(
                    "largest accounts unavailable for %s: %s",
                    mint,
                    e,
                )

        if not holders and rc and rc.get("topHolders"):
            holders, status, src = (
                H.build_holders_from_rugcheck(rc),
                DataStatus.OBSERVED,
                "rugcheck",
            )

        if rc and status == DataStatus.VERIFIED:
            insiders = {
                holder.get("address"): holder.get("insider")
                for holder in rc.get("topHolders") or []
                if isinstance(holder, dict)
            }

            for holder in holders:
                holder.insider = insiders.get(holder.token_account)

        rc_aug = dict(rc or {})
        known = dict(rc_aug.get("knownAccounts") or {})

        known.setdefault(
            RAYDIUM_V4_AUTHORITY,
            {
                "name": "Raydium AMM v4 authority",
                "type": "AMM",
            },
        )

        for pool in extra_pools or []:
            known.setdefault(pool, {"name": "pool for this token (DexScreener)", "type": "AMM"})

        for market in rc_aug.get("markets") or []:
            if isinstance(market, dict) and market.get("pubkey"):
                known.setdefault(
                    market["pubkey"],
                    {
                        "name": f"{market.get('marketType', 'pool')} market",
                        "type": "AMM",
                    },
                )

        rc_aug["knownAccounts"] = known

        return H.analyze_holders(
            holders,
            status,
            src,
            self.s,
            pair_address=pair.pair_address,
            creator=creator,
            rc=rc_aug,
            supply_ui=supply_ui,
        )

    async def _wallets(
        self,
        mint,
        pair,
        rc,
        ha,
        supply_ui,
        creator,
        age_h,
        trade_pool: Optional[str] = None,
    ):
        s = self.s
        pool_addr = trade_pool or pair.pair_address

        if self.p.rpc is None:
            return (
                W.TradeStats(
                    DataStatus.UNAVAILABLE,
                    note="RPC disabled",
                ),
                W.ClusterResult(
                    DataStatus.UNAVAILABLE,
                    note="RPC disabled",
                ),
            )

        pool_owners = {
            pair.pair_address,
            pool_addr,
            RAYDIUM_V4_AUTHORITY,
        }

        pool_owners |= {
            holder.owner
            for holder in ha.holders
            if holder.excluded and holder.owner
        }

        try:
            recent, seen = await W.sample_trades(
                self.p.rpc,
                pool_addr,
                mint,
                pool_owners,
                s.swap_sample_signatures,
            )

        except ProviderError as e:
            log.info("trade sample failed for %s: %s", mint, e)
            recent, seen = [], 0

        verified = [t for t in recent if t.verification in W.VERIFIED_LABELS]
        if len(verified) >= s.min_verified_swaps:
            ts = W.compute_trade_stats(
                verified,
                creator,
                supply_ui,
                seen,
                status=DataStatus.VERIFIED,
                note=(
                    f"{len(verified)} swaps verified from PumpSwap swap events "
                    f"({len(recent) - len(verified)} other candidate transaction(s) excluded); "
                    "a recent sample, not complete trading history"
                ),
            )
        else:
            ts = W.compute_trade_stats(
                recent,
                creator,
                supply_ui,
                seen,
            )
            if verified:
                ts.note = (
                    f"{ts.note}; only {len(verified)} verified swap(s), "
                    f"below the {s.min_verified_swaps} required to treat the sample as VERIFIED"
                )

        launch = []

        if age_h is not None and age_h <= 24:
            try:
                launch, _ = await W.sample_trades(
                    self.p.rpc,
                    pool_addr,
                    mint,
                    pool_owners,
                    40,
                    launch=True,
                )

            except ProviderError as e:
                log.info("launch sample failed for %s: %s", mint, e)

        if launch and creator:
            # A VERIFIED sample must not absorb unverified launch-window candidates.
            launch_for_creator = (
                [t for t in launch if t.verification in W.VERIFIED_LABELS]
                if ts.status == DataStatus.VERIFIED
                else launch
            )
            sold = sum(
                trade.token_amount
                for trade in launch_for_creator
                if trade.wallet == creator and trade.side == "sell"
            )

            if sold and supply_ui:
                ts.creator_sold_pct_supply = round(
                    (ts.creator_sold_pct_supply or 0)
                    + sold / supply_ui * 100,
                    3,
                )

        early = []

        for trade in sorted(launch, key=lambda trade: trade.slot):
            if (
                trade.side == "buy"
                and trade.wallet not in early
                and trade.wallet != creator
            ):
                early.append(trade.wallet)

        holders_w = [
            holder.owner
            for holder in ha.holders
            if not holder.excluded
            and holder.owner
            and holder.owner != creator
        ]

        targets = list(
            dict.fromkeys(
                early[:6]
                + holders_w[:8]
                + [
                    trade.wallet
                    for trade in recent
                    if trade.side == "buy"
                ][:6]
            )
        )

        traced = {}

        if targets:
            try:
                traced = await W.trace_wallets(
                    self.p.rpc,
                    targets,
                    s.funding_trace_wallets,
                )

            except ProviderError as e:
                log.info("wallet tracing failed for %s: %s", mint, e)

        funders = {}
        if traced and s.funding_trace_hops >= 2:
            try:
                funders = await W.trace_funders(self.p.rpc, traced, creator, s.funding_trace_funders)
            except ProviderError as e:
                log.info("second-hop funder tracing failed for %s: %s", mint, e)

        holdings = {}

        for holder in ha.holders:
            if holder.owner and not holder.excluded:
                holdings[holder.owner] = (
                    holdings.get(holder.owner, 0) + holder.amount
                )

        clusters = W.build_clusters(
            launch,
            recent,
            traced,
            holdings,
            supply_ui,
            creator,
            s,
            funders=funders,
        )

        return ts, clusters

    async def _narrative(self, mint: str, pair: PairInfo):
        metas = await self.trending_metas()
        competitors = None

        if self.p.dex is not None and pair.base_symbol:
            try:
                competitors = await self.p.dex.search(pair.base_symbol)
            except ProviderError as e:
                log.info("competitor search failed for %s: %s", mint, e)

        profile = self._profile(mint)
        social_ev = None
        if self.p.socials is not None:
            links = SOC.collect(pair.socials, pair.websites, (profile or {}).get("links"))
            uri = None
            if not any(l.kind in ("x_account", "x_community", "telegram") for l in links):
                uri = (await self._onchain_identity(mint, None)).get("uri")
            social_ev = await self._free_social(mint, pair=pair, profile=profile, uri=uri)

        return analyze_narrative(
            pair,
            profile,
            metas,
            competitors,
            self.s.w_narrative,
            social=social_ev,
        )

    def _verdict(self, mint: str, report: dict) -> Optional[dict]:
        """The rug verdict. Computed BEFORE the decision so the unified engine can use it."""
        if isinstance(report.get("verdict"), dict):
            return report["verdict"]
        if report.get("gates") is None and not report.get("holders"):
            return None
        try:
            from .analysis.verdict import assess
            report["verdict"] = assess(report, self.s, self._flag_stats())
            return report["verdict"]
        except Exception:  # noqa: BLE001 - the summary must never break research
            log.warning("verdict failed for %s", mint, exc_info=True)
            return None

    def _unified(self, mint: str, report: dict) -> Optional[dict]:
        """One score, one risk class, one decision (northstar/analysis/unified.py)."""
        if not getattr(self.s, "unified_enabled", True):
            return None
        try:
            from .analysis import unified
            report["unified"] = unified.evaluate(report, self.s)
            return report["unified"]
        except Exception:  # noqa: BLE001 - never break research over the summary layer
            log.warning("unified decision failed for %s", mint, exc_info=True)
            return None

    async def _sellability(self, mint: str, mint_acc, pair, report: dict) -> None:
        """Could you actually get out? Live Jupiter quotes at real dollar sizes (free, keyless)."""
        if self.p.jupiter is None:
            return
        try:
            sizes = [float(x) for x in str(self.s.jupiter_sell_sizes_usd).split(",") if x.strip()]
            res = await self.p.jupiter.sellability(mint, getattr(mint_acc, "decimals", None),
                                                  pair.price_usd if pair else None, sizes or (100.0,))
        except Exception:  # noqa: BLE001 - exit-cost evidence is optional, never fatal
            log.info("sellability check failed for %s", mint, exc_info=True)
            return
        if res:
            report["sellability"] = res

    def _flag_stats(self) -> dict:
        """Learned crash rates per rug-check flag (refreshed every 10 minutes)."""
        now = time.time()
        cache = getattr(self, "_flag_cache", None)
        if cache is None or now - cache[0] > 600:
            from .learning import load_flag_stats
            cache = (now, load_flag_stats(self.db))
            self._flag_cache = cache
        return cache[1]

    async def _verify_lp_onchain(self, pair: PairInfo, mint: str, la, report: dict) -> None:
        """When RugCheck can't confirm the LP, read the PumpSwap pool on-chain. Confirms burns only."""
        gate = next((g for g in la.gates if g.name == "lp_lock"), None)
        if gate is None or gate.status == GateStatus.PASS or self.p.rpc is None:
            return
        diag: dict = {}
        try:
            res = await pumpswap_lp_burn(self.p.rpc, pair.pair_address, mint, diag)
        except Exception as e:  # noqa: BLE001 - optional evidence; never break research
            log.info("on-chain LP check failed for %s: %s", mint, e)
            report["lp_onchain_check"] = f"error: {type(e).__name__}: {str(e)[:120]}"
            return
        if res is None:
            report["lp_onchain_check"] = diag.get("reason") or "not verified"
            return
        report["lp_onchain_check"] = f"verified: {res['burned_pct']:.1f}% burned"
        pct = res["burned_pct"]
        report["sources"]["lp_onchain"] = "OK"
        if pct >= LPC.BURNED_PCT:
            text = f"BURNED — {pct:.1f}% of LP tokens burned (VERIFIED on-chain, PumpSwap pool)"
            la.lp_status = Datum(text, DataStatus.VERIFIED, "solana_rpc")
            la.lp_locked_pct = pct
            gate.status, gate.reason = GateStatus.PASS, text
            la.flags[:] = [f for f in la.flags if f.code != "LP_STATUS_UNKNOWN"]
        else:
            text = f"PARTIAL — only {pct:.0f}% of LP burned (VERIFIED on-chain); the rest is withdrawable"
            la.lp_status = Datum(text, DataStatus.VERIFIED, "solana_rpc")
            la.lp_locked_pct = pct
            # a measured partial burn is a FACT, not an unknown (v2.4 left the gate UNKNOWN)
            judged = lp_gate(text, pct, pair.liquidity_usd,
                             getattr(getattr(self, "s", None), "min_liquidity_usd", 0.0))
            gate.status, gate.reason = judged.status, judged.reason
            la.flags[:] = [f for f in la.flags if f.code != "LP_STATUS_UNKNOWN"]
            la.flags.append(Flag("LP_NOT_LOCKED", Severity.HIGH if pct < 50 else Severity.MEDIUM,
                                 f"only {pct:.0f}% of LP burned on-chain — remaining liquidity is withdrawable",
                                 DataStatus.VERIFIED))

    MAX_LP_POOLS = 6          # pools weighed for the LP floor (largest first)
    MAX_LP_RPC_READS = 3      # on-chain PumpSwap burn reads per research run

    async def _resolve_lp_floor(self, pair: PairInfo, pairs: list, mint: str, rc: Optional[dict],
                                la, report: dict) -> None:
        """Judge LP safety across all of the token's pools (see security.resolve_lp_floor).

        Runs only when the selected pool's LP is not already a PASS and either the selected pool
        is a concentrated-liquidity pool or the token has other pools. Reads PumpSwap pools on
        chain (the burn is a fact there); never invents a burn it could not read.
        """
        gate = next((g for g in la.gates if g.name == "lp_lock"), None)
        if gate is None or gate.status != GateStatus.UNKNOWN:
            # PASS needs nothing; a MEASURED partial lock on the selected pool stays a FAIL —
            # another pool's burn does not make the deployer's unlocked LP any safer
            return
        own = sorted([p for p in pairs or [] if p.base_mint == mint and (p.liquidity_usd or 0) >= 1_000
                      and p.price_usd],
                     key=lambda p: -(p.liquidity_usd or 0))[: self.MAX_LP_POOLS]
        if pair.pair_address not in {p.pair_address for p in own}:
            own = [pair] + own
        selected_kind, _ = pool_kind(pair, rc)
        if len(own) < 2 and selected_kind != "CONCENTRATED":
            return                    # a single ordinary pool: the per-pool gate already decided

        lp_text = la.lp_status.value or ""
        rows, reads, read_ok = [], 0, False
        for p in own:
            row = {"address": p.pair_address, "dex": p.dex_id, "labels": list(p.labels or []),
                   "liquidity_usd": p.liquidity_usd, "verified": False}
            if p.pair_address == pair.pair_address:
                kind = next((k for k in ("BURNED", "LOCKED", "BONDING", "PARTIAL", "CONCENTRATED")
                             if lp_text.startswith(k)), "UNKNOWN")
                pct = la.lp_locked_pct if kind in ("BURNED", "LOCKED", "PARTIAL") else (
                    0.0 if kind == "CONCENTRATED" else None)
                row["verified"] = "VERIFIED" in lp_text
            else:
                kind, pct = pool_kind(p, rc)
                if kind == "UNKNOWN" and (p.dex_id or "").lower() == "pumpswap" and self.p.rpc is not None \
                        and reads < self.MAX_LP_RPC_READS:
                    reads += 1
                    diag: dict = {}
                    try:
                        res = await pumpswap_lp_burn(self.p.rpc, p.pair_address, mint, diag)
                    except Exception as e:  # noqa: BLE001 - optional evidence; never break research
                        log.info("on-chain LP check failed for %s pool %s: %s", mint, p.pair_address, e)
                        res, diag = None, {"reason": f"error: {type(e).__name__}"}
                    row["onchain_check"] = (f"verified: {res['burned_pct']:.1f}% burned" if res
                                            else diag.get("reason") or "not verified")
                    if res is not None:
                        read_ok = True
                        pct = float(res["burned_pct"])
                        kind = "BURNED" if pct >= LPC.BURNED_PCT else "PARTIAL"
                        row["verified"] = True
            row.update(kind=kind, locked_pct=pct)
            rows.append(row)
        if read_ok:
            report["sources"]["lp_onchain"] = "OK"

        res = resolve_lp_floor(rows, self.s.min_liquidity_usd)
        report["liquidity_breakdown"] = res
        status = {"PASS": GateStatus.PASS, "FAIL": GateStatus.FAIL}.get(res["status"], GateStatus.UNKNOWN)
        gate.status, gate.reason = status, res["reason"]
        verified = any(r.get("verified") for r in rows)
        partial = [r for r in rows if r.get("kind") == "PARTIAL"]
        if status == GateStatus.PASS:
            la.lp_status = Datum(res["reason"], DataStatus.VERIFIED if verified else DataStatus.OBSERVED,
                                 "solana_rpc" if verified else "rugcheck")
            la.flags[:] = [f for f in la.flags if f.code != "LP_STATUS_UNKNOWN"]
            if partial:
                # a pool whose LP tokens are only partly burned has a HOLDER who can pull the
                # rest — unlike third-party concentrated positions, that is the classic rug path
                worst = min(partial, key=lambda r: r.get("locked_pct") or 0)
                la.flags.append(Flag("LP_NOT_LOCKED", Severity.HIGH,
                                     f"pool {worst['address'][:6]}… has only {worst.get('locked_pct') or 0:.0f}% "
                                     "of its LP burned/locked — the holder of the rest can pull it",
                                     DataStatus.VERIFIED if worst.get("verified") else DataStatus.OBSERVED))
        elif status == GateStatus.FAIL:
            la.lp_status = Datum("WITHDRAWABLE — " + res["reason"], DataStatus.OBSERVED, "dexscreener")
            la.flags[:] = [f for f in la.flags if f.code != "LP_STATUS_UNKNOWN"]
            la.flags.append(Flag("LP_NOT_LOCKED", Severity.HIGH, res["reason"], DataStatus.OBSERVED))

    LP_DANGER_WORDS = ("lp unlocked", "lp providers", "liquidity unlocked")

    def _reconcile_rugcheck_lp(self, la, g3: list, f3: list, report: dict) -> None:
        """RugCheck's "Large Amount of LP Unlocked" danger vs Northstar's own LP measurement.

        Both describe the same fact. When Northstar has READ the burn on-chain (lp_lock PASS from
        a verified PumpSwap burn), a contradicting RugCheck LP danger is a provider disagreement,
        not a proven trap: it is recorded as a conflict (lower confidence) and removed from the
        rugcheck_danger gate. Every other RugCheck danger still fails the gate, and without an
        on-chain read the RugCheck LP danger stands. Live log 2026-10-19 showed this danger
        alone hard-rejecting coins before any analysis ran.
        """
        lp_gate_ = next((g for g in la.gates if g.name == "lp_lock"), None)
        if lp_gate_ is None or lp_gate_.status != GateStatus.PASS or la.lp_status.status != DataStatus.VERIFIED:
            return
        gate = next((g for g in g3 if g.name == "rugcheck_danger" and g.status == GateStatus.FAIL), None)
        if gate is None:
            return
        names = [n.strip() for n in gate.reason.replace("RugCheck danger:", "").split(",") if n.strip()]
        lp = [n for n in names if any(w in n.lower() for w in self.LP_DANGER_WORDS)]
        if not lp:
            return
        rest = [n for n in names if n not in lp]
        text = (f"RugCheck reports '{', '.join(lp)}' but the LP burn was read on-chain: "
                f"{lp_gate_.reason[:160]}")
        f3.append(Flag("LP_PROVIDER_CONFLICT", Severity.MEDIUM, text, DataStatus.OBSERVED))
        report.setdefault("provider_conflicts", []).append(text)
        if rest:
            gate.reason = "RugCheck danger: " + ", ".join(rest)
        else:
            gate.status, gate.critical = GateStatus.PASS, False
            gate.reason = "only RugCheck danger was LP-unlocked, contradicted by the on-chain burn read"

    @staticmethod
    def _trade_pool(pair: PairInfo, pairs: list, mint: str) -> str:
        """Where to sample swaps: the selected pool, unless it is a pool type the decoder cannot
        read (DLMM/CLMM) and the token has a PumpSwap pool, whose swap events ARE decodable."""
        kind, _ = pool_kind(pair, None)
        if kind != "CONCENTRATED":
            return pair.pair_address
        swaps = [p for p in pairs or [] if p.base_mint == mint and (p.dex_id or "").lower() == "pumpswap"
                 and (p.liquidity_usd or 0) > 0]
        return max(swaps, key=lambda p: p.liquidity_usd or 0).pair_address if swaps else pair.pair_address

    async def _free_social(self, mint: str, *, pair: Optional[PairInfo] = None, profile: Optional[dict] = None,
                           uri: Optional[str] = None) -> Optional["SOC.SocialEvidence"]:
        """Links the token publishes, checked for reuse across recent tokens; never raises."""
        try:
            meta = await self.p.socials.metadata(uri) if uri else None
            if meta and isinstance(meta.get("description"), str):
                self._descriptions[mint] = meta["description"][:500]
            links = SOC.collect(pair.socials if pair else None, pair.websites if pair else None,
                                (profile or {}).get("links"), meta)
            reusable = [l for l in links if l.kind in SOC.REUSE_KINDS]
            since = time.time() - self.s.social_reuse_days * 86400
            ev = SOC.SocialEvidence(links=links, reuse=self.db.social_link_reuse(mint, reusable, since))
            self.db.record_social_links(mint, reusable)
            tg = next((l for l in links if l.kind == "telegram" and l.handle), None)
            if tg is not None and not ev.recycled:
                ev.telegram_handle = tg.handle
                ev.telegram_members = await self.p.socials.telegram_members(tg.handle)
            return ev
        except Exception:  # noqa: BLE001 - social evidence is optional; never block research
            log.warning("free social check failed for %s", mint, exc_info=True)
            return None

    def _caveats(self, ts, ha, nr, ms) -> list[str]:
        caveats = [
            "weights are starting assumptions, not validated predictive coefficients",
            "holder data covers the top 20 accounts only",
        ]

        if ts.status == DataStatus.VERIFIED:
            caveats.append(
                f"demand metrics come from a {ts.sample_size}-swap sample"
                + (
                    f" spanning {ts.span_minutes:.0f} min"
                    if ts.span_minutes
                    else ""
                )
            )

        if nr.social_status != DataStatus.OBSERVED and nr.free_socials:
            caveats.append(
                "social data is limited to the token's own links and public Telegram size; "
                "X activity is not measured"
            )
        elif nr.social_status != DataStatus.OBSERVED:
            caveats.append(
                "no social data: narrative attention is unmeasured"
            )

        if ms.snapshots_used < 4:
            caveats.append(
                "chart structure assessed without candle data "
                "and with little snapshot history"
            )

        return caveats

    def _finish(
        self,
        mint: str,
        pair: PairInfo,
        dh: Optional[str],
        report: dict,
        gates: list[GateResult],
        flags: list[Flag],
        partial: bool = False,
        extras: Optional[dict] = None,
    ) -> ResearchOutcome:
        s = self.s
        risk = assess_risk(flags, gates)

        report["gates"] = [gate.to_dict() for gate in gates]
        report["flags"] = [flag.to_dict() for flag in flags]
        report["risk"] = risk.value

        if partial or not extras:
            failed = [
                f"gate failed: {gate.name} — {gate.reason}"
                for gate in gates
                if gate.status == GateStatus.FAIL
            ]

            return self._final(
                mint,
                Action.REJECT,
                report,
                failed or ["security gate failed"],
                pair=pair,
                dh=dh,
                risk=risk,
            )

        sr = extras["sr"]
        nr = extras["nr"]
        ms = extras["ms"]

        cooldown = (
            self.db.recent_alert(
                mint,
                time.time() - s.alert_cooldown_hours * 3600,
            )
            is not None
        )

        cap = (
            self.db.alerts_since(time.time() - 86400)
            >= s.max_daily_alerts
        )

        # The rug verdict and the unified engine run BEFORE the decision, not after it.
        self._verdict(mint, report)
        u = self._unified(mint, report)

        # Publication policy is computed on its own. v2.4 took it from the legacy decide()'s
        # blockers, but decide() returns early on any FAILED or critical-UNKNOWN gate — so on
        # exactly those coins the cooldown, daily cap and callout market-cap minimum were never
        # computed and a unified STRONG could bypass them.
        report["policy_blockers"] = self.publication_blockers(cooldown, cap, pair.market_cap)

        decision: Decision = decide(
            sr.total,
            risk,
            sr.confidence,
            gates,
            nr.component.points,
            nr.evidence_items,
            ms.extended,
            s,
            cooldown,
            cap,
            market_cap=pair.market_cap,
            volume_acceleration=ms.volume_acceleration,
            # buys with zero sells has no finite ratio; it is the strongest buying, not unknown
            buy_sell_ratio=(ms.buy_sell_ratio_h1 if ms.buy_sell_ratio_h1 is not None
                            else (float("inf") if ((pair.txns or {}).get("h1") or {}).get("buys") else None)),
        )

        if u:
            # ONE source of truth (northstar/final.py): the unified result decides the action,
            # and its score, risk and confidence are what is stored and shown everywhere.
            from .final import final_view

            v = final_view(u, report["policy_blockers"])
            report["decision"] = {"action": v["action"].value, "reasons": v["reasons"],
                                  "blockers": v["blockers"]}
            report["confidence"] = v["confidence"].value
            return self._final(mint, v["action"], report,
                               v["reasons"] + [b for b in v["blockers"] if b not in v["reasons"]][:6],
                               pair=pair, dh=dh, risk=v["risk"], score=v["score"],
                               confidence=v["confidence"])

        report["decision"] = decision.to_dict()
        report["confidence"] = sr.confidence.value

        return self._final(
            mint,
            decision.action,
            report,
            decision.reasons + decision.blockers,
            pair=pair,
            dh=dh,
            risk=risk,
            score=sr.total,
            confidence=sr.confidence,
        )

    def publication_blockers(self, cooldown: bool, cap: bool, market_cap: Optional[float]) -> list[str]:
        """Publication policy: may hold a call back, never create one."""
        out = []
        if cooldown:
            out.append("alert cooldown active for this mint")
        if cap:
            out.append("daily alert cap reached")
        min_mc = getattr(self.s, "min_callout_market_cap", 0) or 0
        if min_mc > 0 and (market_cap is None or market_cap < min_mc):
            out.append(f"market cap {'UNKNOWN' if market_cap is None else f'${market_cap:,.0f}'} "
                       f"below ${min_mc:,.0f} callout minimum")
        return out

    def _not_ready(self, mint: str, report: dict, reasons: list[str],
                   score: Optional[float] = None) -> ResearchOutcome:
        """The coin has nothing to judge yet — no pool, or no trades at all.

        This is not a verdict, so it is not written to the research history and does not appear
        in the decision counts. Recording these as INSUFFICIENT DATA buried the real decisions:
        on the live bot they were 61% of everything the panel showed. The coin stays a candidate
        and comes back when the curve radar sees it trading.
        """
        report["final_action"] = "NOT_READY"
        report["reasons"] = reasons
        return ResearchOutcome(mint, Action.INSUFFICIENT_DATA, report, reasons=reasons,
                               score=score, not_ready=True)

    def _final(
        self,
        mint: str,
        action: Action,
        report: dict,
        reasons: list[str],
        pair: Optional[PairInfo] = None,
        dh: Optional[str] = None,
        risk: RiskLevel = RiskLevel.INCOMPLETE,
        score: Optional[float] = None,
        confidence: Confidence = Confidence.LOW,
    ) -> ResearchOutcome:
        report.setdefault(
            "decision",
            {
                "action": action.value,
                "reasons": reasons,
                "blockers": [],
            },
        )

        from .final import apply_final, has_unified

        if has_unified(report):
            # the unified risk class is THE risk; the older assess_risk() level stays for reference
            report.setdefault("legacy_risk", report.get("risk"))
            report["risk"] = risk.value
        else:
            report["risk"] = report.get("risk", risk.value)
        report["final_action"] = action.value
        report["reasons"] = reasons
        self._verdict(mint, report)

        outcome = ResearchOutcome(mint, action, report, dh, pair, None, score, risk, confidence, reasons)
        apply_final(outcome)
        report["final"]["stage"] = "researched"

        # store exactly what the outcome carries (apply_final is the single source of truth)
        action, score, risk, confidence = outcome.action, outcome.score, outcome.risk, outcome.confidence
        snapshot_id = self.db.save_research(
            mint,
            action.value,
            score,
            risk.value,
            confidence.value,
            pair.pair_address if pair else None,
            pair.price_usd if pair else None,
            pair.liquidity_usd if pair else None,
            pair.market_cap if pair else None,
            pair.fdv if pair else None,
            report,
        )

        if pair:
            trades = report.get("trades") or {}
            holders = report.get("holders") or {}

            self.db.save_market_snapshot(
                mint,
                pair.pair_address,
                price_usd=pair.price_usd,
                liquidity_usd=pair.liquidity_usd,
                market_cap=pair.market_cap,
                fdv=pair.fdv,
                vol_m5=pair.vol.get("m5"),
                vol_h1=pair.vol.get("h1"),
                buys_m5=pair.txns["m5"]["buys"],
                sells_m5=pair.txns["m5"]["sells"],
                buys_h1=pair.txns["h1"]["buys"],
                sells_h1=pair.txns["h1"]["sells"],
                unique_buyers=trades.get("unique_buyers"),
                unique_sellers=trades.get("unique_sellers"),
                top10_adj_pct=holders.get("top10_adj"),
            )

            # Track every decision, including rejected tokens, to avoid
            # survivorship bias in later performance statistics.
            self.db.schedule_outcomes(
                snapshot_id,
                mint,
                action.value,
                score,
                pair.price_usd,
                pair.liquidity_usd,
            )

        outcome.snapshot_id = snapshot_id
        return outcome