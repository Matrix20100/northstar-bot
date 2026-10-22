"""Candidate discovery from multiple independent feeds, deduplicated by mint.

Sources:
  dexscreener_profile   - newly listed token profiles (team-submitted info)          priority 2.0
  dexscreener_cto       - community takeovers                                         priority 2.5
  dexscreener_boost     - paid boosts (promotion; discovery only, no narrative credit) priority 1.0
  pumpportal_new        - pump.fun launches (researched after an initial delay)       priority 1.0
  pumpportal_migration  - pump.fun bonding-curve graduations into DEX pools           priority 3.0
DexScreener exposes no public "all new pairs" or "top movers" feed, so volume-growth discovery
happens through re-research of known candidates rather than a market-wide scan.
"""
from __future__ import annotations

import logging
import time

from .analysis.identity import is_valid_mint_format
from .config import Settings
from .db import Database
from .net import ProviderError

log = logging.getLogger("northstar.discovery")

PRIORITY = {"dexscreener_profile": 2.0, "dexscreener_cto": 2.5, "dexscreener_boost": 1.0,
            "pumpportal_new": 1.0, "pumpportal_migration": 3.0}


class Discovery:
    def __init__(self, s: Settings, db: Database, dex):
        self.s, self.db, self.dex = s, db, dex
        self.invalid_seen = 0
        self.pumpportal_events = {"new_token": 0, "migration": 0, "trade": 0}
        self.pumpportal_new_candidates = 0
        self.pumpportal_duplicate_events = 0

    def ingest(self, mint: str, source: str, payload: dict | None = None, name: str | None = None,
               symbol: str | None = None) -> bool:
        if not is_valid_mint_format(mint):
            self.invalid_seen += 1
            return False
        is_new = self.db.upsert_discovery(mint, source, PRIORITY.get(source, 1.0), name, symbol, payload)
        if is_new and source == "pumpportal_new":
            if getattr(self.s, "research_new_launches_on_timer", False):
                self.db.execute("UPDATE candidates SET next_research_at=? WHERE mint=?",
                                (time.time() + self.s.new_token_initial_delay_seconds, mint))
            else:
                # not scheduled: the curve radar researches it once it shows real buying
                self.db.execute("UPDATE candidates SET next_research_at=NULL WHERE mint=?", (mint,))
        elif not is_new and source == "pumpportal_migration":
            # graduation is new information: research soon even if previously rejected
            self.db.execute("UPDATE candidates SET next_research_at=?, last_data_hash=NULL WHERE mint=? AND state IN "
                            "('REJECTED','WATCHLIST','INSUFFICIENT_DATA','DISCOVERED')", (time.time(), mint))
        return is_new

    async def run_once(self) -> dict:
        counts: dict[str, int] = {}
        if self.dex is None:
            return counts
        feeds = [("dexscreener_profile", self.dex.latest_profiles),
                 ("dexscreener_cto", self.dex.community_takeovers),
                 ("dexscreener_boost", self.dex.latest_boosts),
                 ("dexscreener_boost", self.dex.top_boosts)]
        for source, fn in feeds:
            try:
                items = await fn()
            except ProviderError as e:
                log.warning("discovery feed %s failed: %s", source, e)
                continue
            new = 0
            for it in items:
                payload = {k: it.get(k) for k in ("description", "links", "icon", "url", "amount", "totalAmount",
                                                  "claimDate") if it.get(k) is not None}
                new += self.ingest(it["tokenAddress"], source, payload)
            counts[source] = counts.get(source, 0) + new
        return counts

    def _creator_stats(self, kind: str, mint: str, payload: dict, is_new: bool, first_migration: bool) -> None:
        """Count launches and graduations per creator wallet (v2.5.9). Never raises."""
        try:
            if kind == "new_token" and is_new and payload.get("traderPublicKey"):
                self.db.record_creator_launch(str(payload["traderPublicKey"]))
            elif first_migration:
                creator = self.db.launch_creator(mint)
                if creator:
                    self.db.record_creator_migration(creator)
        except Exception:  # noqa: BLE001 - statistics must never stop discovery
            log.debug("creator stats update failed for %s", mint, exc_info=True)

    async def on_pumpportal(self, kind: str, msg: dict) -> None:
        mint = str(msg.get("mint") or "")
        if not is_valid_mint_format(mint):
            return
        if kind == "trade":
            self.pumpportal_events["trade"] += 1
            self.db.record_curve_snapshot(mint, msg)
            signal = self.db.fetchone("SELECT score, report FROM early_signals WHERE mint=?", (mint,))
            if signal:
                try:
                    import json
                    old_report = json.loads(signal.get("report") or "{}")
                    cap = msg.get("marketCapSol")
                    cap = float(cap) if cap is not None else None
                    self.db.create_or_update_early_signal(mint, cap, float(signal.get("score") or 0), old_report)
                except (TypeError, ValueError):
                    pass
            now = time.time()
            self.db.execute(
                "UPDATE candidates SET next_research_at=MIN(COALESCE(next_research_at,?),?), updated_at=? "
                "WHERE mint=? AND state IN ('DISCOVERED','WATCHLIST','INSUFFICIENT_DATA','REJECTED')",
                (now, now + 2, now, mint))
            return
        self.pumpportal_events[kind if kind in self.pumpportal_events else "new_token"] += 1
        source = "pumpportal_migration" if kind == "migration" else "pumpportal_new"
        payload = {k: msg.get(k) for k in (
            "traderPublicKey", "name", "symbol", "pool", "signature", "marketCapSol", "solAmount",
            "tokenAmount", "bondingCurveKey", "vSolInBondingCurve", "vTokensInBondingCurve", "txType"
        ) if msg.get(k) is not None}
        first_migration = kind == "migration" and not self.db.has_discovery_source(mint, "pumpportal_migration")
        is_new = self.ingest(mint, source, payload=payload, name=msg.get("name"), symbol=msg.get("symbol"))
        self._creator_stats(kind, mint, payload, is_new, first_migration)
        if is_new:
            self.pumpportal_new_candidates += 1
        else:
            self.pumpportal_duplicate_events += 1
        if kind == "new_token":
            self.db.record_curve_snapshot(mint, msg)
