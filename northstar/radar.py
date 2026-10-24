"""Surge radar: spot money starting to flow into a coin and research it immediately.

The research queue is slow by design (deep on-chain checks for every launch), so a migrated coin
could wait minutes before anyone looks at it again, and most moves happen inside 15 minutes.
The radar is a cheap, fast loop on top of it:

  * every ``radar_interval_seconds`` it pulls DexScreener stats for recently migrated / listed coins
    (30 coins per request);
  * it flags a SURGE when the last 5 minutes show a volume spike versus the rest of the hour,
    real buying (enough buys, more buys than sells) and a price that is rising but has not already
    gone vertical;
  * a surging coin is researched straight away (all rug checks and the decision rules still apply).

It never posts anything by itself and never bypasses a safety gate. It only changes WHEN a coin is
looked at. Thresholds are starting assumptions; every surge is recorded so they can be checked.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from .net import ProviderError

log = logging.getLogger("northstar.radar")
QUOTES = {"So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
          "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}


@dataclass
class Surge:
    mint: str
    symbol: str
    market_cap: float
    liquidity: float
    vol_m5: float
    baseline_5m: float
    spike: float
    buys_m5: int
    sells_m5: int
    change_m5: Optional[float]
    change_h1: Optional[float]

    def to_dict(self) -> dict:
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in self.__dict__.items()}

    def line(self) -> str:
        bs = f"{self.buys_m5} buys / {self.sells_m5} sells"
        return (f"⚡ Surge: ${self.vol_m5:,.0f} volume in 5 min ({self.spike:.1f}x the hour's pace), {bs}"
                + (f", price {self.change_m5:+.0f}% in 5 min" if self.change_m5 is not None else ""))


NEAR_MISS_PER_HOUR = 25

RADAR_KEYS = ("radar_volume_spike", "radar_min_buys_m5", "radar_buy_sell_ratio", "radar_min_volume_m5",
              "radar_max_change_m5")


def metrics(pair) -> Optional[dict]:
    """Raw radar numbers for one DexScreener pair (no thresholds applied). None when unusable."""
    mc, liq = pair.market_cap, pair.liquidity_usd
    v5, v1 = (pair.vol or {}).get("m5"), (pair.vol or {}).get("h1")
    t5 = (pair.txns or {}).get("m5") or {}
    if not mc or not liq or v5 is None:
        return None
    # Average 5-min volume over the previous 55 minutes. A pool younger than ~10 minutes (or lagging
    # DexScreener data where 1h <= 5m) has no real baseline: every trade would look like an infinite
    # spike, so that is never treated as a surge.
    created = getattr(pair, "pair_created_at", None)
    if created and time.time() - created < 600:
        return None
    if not v1 or v1 <= v5:
        return None
    baseline = (v1 - v5) / 11.0
    return {"mc": mc, "liq": liq, "vol_m5": v5, "baseline": baseline, "spike": min(v5 / baseline, 99.0),
            "buys": t5.get("buys") or 0, "sells": t5.get("sells") or 0,
            "c5": (pair.price_change or {}).get("m5"), "c1": (pair.price_change or {}).get("h1")}


def passes(m: dict, s, overrides: Optional[dict] = None) -> bool:
    """The surge rules. ``overrides`` lets the learner replay them with other thresholds."""
    g = (lambda k: (overrides or {}).get(k, getattr(s, k)))  # noqa: E731
    if not (s.radar_min_market_cap <= m["mc"] <= s.radar_max_market_cap) or m["liq"] < s.radar_min_liquidity:
        return False
    if m["vol_m5"] < g("radar_min_volume_m5") or m["spike"] < g("radar_volume_spike"):
        return False
    if m["buys"] < g("radar_min_buys_m5") or m["buys"] < g("radar_buy_sell_ratio") * max(1, m["sells"]):
        return False
    if m["c5"] is not None and not (0 <= m["c5"] <= g("radar_max_change_m5")):
        return False                                       # falling, or already vertical
    if m["c1"] is not None and m["c1"] > s.radar_max_change_h1:
        return False                                       # the move already happened this hour
    return True


def near_miss(m: dict, s) -> bool:
    """Loose version of the rules: worth recording so the learner can see what it missed."""
    return (s.radar_min_market_cap <= m["mc"] <= s.radar_max_market_cap and m["liq"] >= s.radar_min_liquidity
            and m["vol_m5"] >= 1000 and m["spike"] >= 1.5 and m["buys"] >= 5 and m["buys"] >= m["sells"]
            and (m["c5"] is None or m["c5"] >= 0))


def detect(pair, s) -> Optional[Surge]:
    """Surge test for one DexScreener pair; None when it does not qualify."""
    m = metrics(pair)
    if m is None or not passes(m, s):
        return None
    return Surge(pair.base_mint, pair.base_symbol, m["mc"], m["liq"], m["vol_m5"], m["baseline"], m["spike"],
                 m["buys"], m["sells"], m["c5"], m["c1"])


class SurgeRadar:
    def __init__(self, s, db, dex, cg=None):
        self.s, self.db, self.dex, self.cg = s, db, dex, cg
        self._last_trigger: dict[str, float] = {}
        self.last_surges: dict[str, Surge] = {}
        self._last_trending = 0.0
        self.trending: dict[str, dict] = {}     # mint -> latest CoinGecko trending row (attention)
        self._last_obs: dict[str, float] = {}
        self._near_miss_hour: tuple[int, int] = (0, 0)       # (hour, near-misses recorded in it)

    def _observe(self, mint: str, pair, now: float) -> None:
        """Record surges AND near-misses (max one per coin per 20 min) for the radar learner."""
        if now - self._last_obs.get(mint, 0) < 1200 or not pair.price_usd or not pair.pair_address:
            return
        m = metrics(pair)
        if m is None or not near_miss(m, self.s):
            return
        fired = passes(m, self.s)
        if not fired:
            # every surge is kept; near-misses are capped so the price checks can keep up with them
            hour = int(now // 3600)
            h, n = self._near_miss_hour
            n = n if h == hour else 0
            if n >= NEAR_MISS_PER_HOUR:
                return
            self._near_miss_hour = (hour, n + 1)
        self._last_obs[mint] = now
        try:
            self.db.execute("INSERT INTO radar_obs(mint, ts, pool, price, metrics, fired) VALUES (?,?,?,?,?,?)",
                            (mint, now, pair.pair_address, pair.price_usd, json.dumps(m), 1 if fired else 0))
        except Exception:  # noqa: BLE001 - learning data is optional, never break the radar
            log.debug("radar observation not stored", exc_info=True)

    async def _refresh_trending(self, now: float) -> None:
        """CoinGecko/GeckoTerminal trending Solana pools join the radar's watchlist (attention feed)."""
        if self.cg is None or now - self._last_trending < self.s.coingecko_trending_seconds:
            return
        self._last_trending = now
        try:
            rows = await self.cg.trending_pools("5m")
        except Exception:  # noqa: BLE001 - optional feed
            log.debug("trending refresh failed", exc_info=True)
            return
        fresh = {}
        for rank, row in enumerate(rows, 1):
            row = {**row, "rank": rank, "seen_at": now}
            fresh[row["mint"]] = row
            if row.get("market_cap") or row.get("fdv"):
                self.db.upsert_discovery(row["mint"], "coingecko_trending", 4.0, row.get("name"), None,
                                         {k: row[k] for k in ("pool", "rank", "market_cap", "fdv", "vol_m5")})
        if fresh:
            self.trending = fresh
            log.info("coingecko trending: %d Solana pools on the watchlist", len(fresh))

    def _watchlist(self, now: float) -> list[str]:
        rows = self.db.fetchall(
            """
            SELECT mint FROM candidates
            WHERE discovered_at >= ?
              AND state IN ('DISCOVERED', 'WATCHLIST', 'INSUFFICIENT_DATA', 'REJECTED')
              AND (pool_address IS NOT NULL OR sources LIKE '%migration%' OR sources LIKE '%dexscreener%'
                   OR sources LIKE '%coingecko%')
              AND NOT (state = 'REJECTED' AND next_research_at IS NULL)
            ORDER BY discovered_at DESC
            LIMIT ?
            """,
            (now - self.s.radar_lookback_hours * 3600, self.s.radar_max_tokens))
        return [r["mint"] for r in rows]

    def _dead(self, best: dict, now: float) -> set:
        """v4.5.0: coins the radar's own prices prove are confirmed rugs (died, liquidity pulled, dead
        pool) are blocked on the spot: no research, no surge, never watched again."""
        from . import rugs as RG
        if not best:
            return set()
        marks = ",".join("?" * len(best))
        try:
            known = {r["mint"]: r for r in self.db.fetchall(
                f"SELECT mint, state, peak_mcap, peak_liq, last_research_at FROM candidates WHERE mint IN ({marks})",
                list(best))}
        except Exception:  # noqa: BLE001
            return set()
        out = set()
        for mint, pair in best.items():
            cand = known.get(mint)
            if cand is None or cand["state"] in ("PUBLISHED", "WARNING", "CLOSED", "QUALIFIED", "IGNORED"):
                continue            # unknown here (trending only) or a call: the monitor follows calls
            if cand["state"] == RG.STATE:
                out.add(mint)
                continue
            mc, liq = pair.market_cap or pair.fdv, pair.liquidity_usd
            max_age = float(getattr(self.s, "max_token_age_hours", 0) or 0)
            created = getattr(pair, "pair_created_at", None)
            if (max_age and created and cand["state"] == "DISCOVERED" and not cand.get("last_research_at")
                    and (now - created) / 3600 > max_age):
                # v4.5.1: a trending/boosted coin whose pool is older than the limit would only be researched
                # to be rejected by the prefilter ("pool age 19207h exceeds max 8h"): reject it here for free
                age = (now - created) / 3600
                self.db.set_state(mint, "REJECTED", next_research_at=None, last_action="REJECT",
                                  reasons=[f"prefilter: pool age {age:.0f}h exceeds max {max_age:.0f}h (seen by the radar)"])
                out.add(mint)
                continue
            prior = max([x for x in (cand.get("peak_mcap"), RG.recorded_peak(self.db, mint)) if x], default=None)
            why = RG.confirmed(mc, liq, True, prior, cand.get("peak_liq"), self.s)
            if why:
                RG.block(self.db, mint, f"{why} (seen by the surge radar)", now, last_action="REJECT")
                out.add(mint)
        return out

    async def run_once(self, now: Optional[float] = None) -> list[Surge]:
        now = now or time.time()
        await self._refresh_trending(now)
        mints = list(dict.fromkeys(self._watchlist(now) + list(self.trending)))
        if self.trending:
            try:
                blocked = {r["mint"] for r in self.db.fetchall("SELECT mint FROM candidates WHERE state = 'RUGGED'")}
            except Exception:  # noqa: BLE001
                blocked = set()
            mints = [m for m in mints if m not in blocked]
        if not mints or self.dex is None:
            return []
        try:
            pairs = await self.dex.tokens_batch(mints)
        except ProviderError as exc:
            log.warning("radar refresh failed: %s", exc)
            return []
        best: dict[str, object] = {}
        for p in pairs:
            if p.quote_mint not in QUOTES:
                continue
            cur = best.get(p.base_mint)
            if cur is None or (p.liquidity_usd or 0) > (cur.liquidity_usd or 0):
                best[p.base_mint] = p
        out = []
        dead = self._dead(best, now)
        for mint, pair in best.items():
            if mint in dead:
                continue
            if self.s.radar_learning:
                self._observe(mint, pair, now)
            if now - self._last_trigger.get(mint, 0) < self.s.radar_cooldown_minutes * 60:
                continue
            surge = detect(pair, self.s)
            if surge is None:
                continue
            self._last_trigger[mint] = now
            self.last_surges[mint] = surge
            self.db.upsert_discovery(mint, "radar_surge", 10.0, pair.base_name, pair.base_symbol, surge.to_dict())
            log.info("SURGE %s ($%s): mc $%s, 5m vol $%s (%.1fx the hour's pace), %d buys / %d sells, m5 %s%%",
                     mint, surge.symbol, f"{surge.market_cap:,.0f}", f"{surge.vol_m5:,.0f}", surge.spike,
                     surge.buys_m5, surge.sells_m5, surge.change_m5)
            out.append(surge)
        if len(self._last_obs) > 5000:
            self._last_obs = {k: v for k, v in self._last_obs.items() if v >= now - 3600}
        if len(self._last_trigger) > 5000:
            cutoff = now - 6 * 3600
            self._last_trigger = {k: v for k, v in self._last_trigger.items() if v >= cutoff}
            self.last_surges = {k: v for k, v in self.last_surges.items() if k in self._last_trigger}
        return out
