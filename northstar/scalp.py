"""RUG SCALP calls: strong momentum on a coin that fails the rug checks.

Some manipulated coins still run 2-3x before the dump. The owner wants those called, but never
disguised as a normal call. A coin qualifies only when ALL of these hold:

  * money is flowing in right now (a radar surge in the last 15 minutes, or strong 1h momentum);
  * every failed safety check is a "rug signal" (whales / bundles / insiders / creator) and NOT a
    trap: active mint or freeze authority, transfer fees/hooks, a withdrawable LP, liquidity already
    being pulled, a token already marked rugged, or broken price data. Those can make selling
    impossible, so they are never posted, not even as a scalp;
  * enough market cap and liquidity to get out.

The call then carries a loud RUG banner, every rug reason in plain words, and the scalp plan
(small take-profits, -15% stop, 10-minute time stop, sell everything by TP2).
"""
from __future__ import annotations

from typing import Optional

# failed checks that make a coin a rug but still tradeable for a quick scalp
SCALPABLE = {"holder_concentration", "creator_holdings", "creator_dumping", "launch_bundle", "bundled_supply",
             "rugcheck_danger", "wallet_clusters"}
# failed (or unknown) checks that can trap a buyer: never posted
TRAPS = {"mint_authority", "freeze_authority", "token_extensions", "lp_lock", "liquidity_removal",
         "rugcheck_rugged", "source_consistency", "rugcheck"}
# these checks must be present AND passed (a check that never ran counts as a trap)
MUST_PASS = {"mint_authority", "freeze_authority", "lp_lock", "liquidity_removal"}

PLAIN = {
    "holder_concentration": "a few wallets hold a big share of supply",
    "creator_holdings": "the creator still holds a large bag",
    "creator_dumping": "the creator is selling",
    "launch_bundle": "the launch was bundled (insiders bought in the first blocks)",
    "bundled_supply": "bundled wallets hold a large share of supply",
    "rugcheck_danger": "RugCheck flags it as dangerous",
    "wallet_clusters": "linked wallets (one group) hold a large share",
}


def exit_blocked(report: dict) -> Optional[str]:
    """Why a scalp could trap the buyer even though every failed GATE is scalpable.

    Before v2.5 the scalp check read the gates only, so a coin whose Jupiter exit check found no
    sell route, whose sell cost was ruinous, whose price was already collapsing, or whose rug
    check found a trap flag (transfer hook, permanent delegate, ...) could still be posted as a
    scalp. A scalp is only worth posting if the exit works.
    """
    u = report.get("unified") if isinstance(report.get("unified"), dict) else {}
    for v in u.get("vetoes") or []:
        if not isinstance(v, dict):
            continue
        if v.get("severity") == "CONFIRMED_CRITICAL" and not str(v.get("key", "")).startswith("gate:"):
            return v.get("text") or v.get("key")
        if v.get("key") in ("haz:exit", "haz:exit_cost", "haz:collapse") and v.get("blocking"):
            return v.get("text") or v.get("key")
    for r in (report.get("verdict") or {}).get("red") or []:
        if isinstance(r, dict) and r.get("level") == "critical":
            return r.get("text") or r.get("key")
    sell = report.get("sellability") if isinstance(report.get("sellability"), dict) else {}
    if sell.get("checked") and sell.get("no_route_at_usd") is not None:
        return f"a ${sell['no_route_at_usd']:,.0f} sell cannot be routed"
    if (report.get("market_structure") or {}).get("collapsed"):
        return "price already collapsed"
    return None


def _gates(report: dict) -> list[dict]:
    return [g for g in report.get("gates") or [] if isinstance(g, dict)]


def check(report: dict, pair, s, surge: Optional[dict] = None) -> Optional[dict]:
    """Scalp verdict for a REJECTED post-migration coin, or None when it must not be posted."""
    if not getattr(s, "scalp_enabled", False) or pair is None:
        return None
    if s.allow_active_mint_authority or s.allow_active_freeze_authority:
        return None             # authorities could then PASS while still active: no scalps in that mode
    gates = _gates(report)
    failed = [g for g in gates if g.get("status") == "FAIL"]
    if not failed:
        return None                                        # not a rug-check rejection
    names = {g.get("name") for g in failed}
    unknown_traps = {g.get("name") for g in gates if g.get("status") == "UNKNOWN" and g.get("name") in TRAPS}
    if names - SCALPABLE or unknown_traps:
        return None                                        # a trap (or unchecked trap): never post
    # The trap checks must have actually RUN and passed. A missing check (e.g. RugCheck down, LP check
    # switched off) is treated exactly like a failed one.
    passed = {g.get("name") for g in gates if g.get("status") == "PASS"}
    if not MUST_PASS <= passed:
        return None
    if exit_blocked(report):
        return None                                        # cannot get out: never a scalp
    mc, liq = pair.market_cap, pair.liquidity_usd
    if not mc or mc < s.scalp_min_market_cap or not liq or liq < s.scalp_min_liquidity:
        return None
    ms = report.get("market_structure") or {}
    acc, bs = ms.get("volume_acceleration"), ms.get("buy_sell_ratio_h1")
    strong = isinstance(acc, (int, float)) and acc >= 2.0 and isinstance(bs, (int, float)) and bs >= 1.3
    if s.scalp_require_surge and not surge:
        return None
    if not surge and not strong:
        return None
    reasons = []
    for g in failed:
        plain = PLAIN.get(g.get("name"), g.get("name"))
        reasons.append(f"{plain} — {g.get('reason', '')}".rstrip(" —"))
    return {"rug_reasons": reasons, "momentum": (surge or {}).get("line")
            or f"1h volume {acc:.1f}x its 6h pace, {bs:.2f} buys per sell", "failed_checks": sorted(names)}


BANNER = ("☠️ **THIS IS A RUG — MOMENTUM IS STRONG, SCALP ONLY.**\n"
          "Small size. Take profit fast. Respect the -15% stop. **Never hold this coin.**")
