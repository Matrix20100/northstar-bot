"""The second brain: one plain-English rug verdict from everything Northstar measured.

A pro looks at a coin and answers "is this a rug?" in seconds by checking the same things every
time: can the dev still mint or freeze, is the LP burned, who holds the supply and are those
wallets secretly the same person (the bubblemap), how old are the wallets, did the launch get
bundled, has this dev rugged before, is the dev selling. This module does exactly that and
writes the answer down with the evidence for and against.

Levels
  RUG LIKELY       a trap is present (mint/freeze authority, pullable LP, transfer tricks,
                   liquidity being pulled, RugCheck says rugged) - do not touch
  HIGH RISK        several serious red flags (connected wallets holding a lot, prior rugs,
                   dev dumping, heavy bundle, fresh-wallet supply...)
  CAUTION          some red flags; only small size / scalps
  LOOKS CLEAN      nothing serious found and the key checks were actually done
  NOT ENOUGH DATA  too many key checks could not be completed to say it looks clean

It is a summary for humans. It never loosens a safety gate: the gates in decision.py still
decide what may be called.

Calibrated on real outcomes: on 731 coins Northstar followed for 24 h (Oct 2026) the average coin
fell below 0.3x 41% of the time, but coins with one wallet holding 8%+, concentrated buying, wash
trading, a copycat ticker, mostly brand-new wallets, or a dev holding/selling crashed 85-100% of
the time, so those are serious red flags. "Liquidity dropped from its recent high" crashed only
27% of the time (it usually fires after the drop), so it is serious but not a trap.
The learner keeps measuring this (learning.flag_outcomes): each red flag shows how often it led
to a crash, and a medium flag that keeps proving deadly is upgraded automatically.
"""
from __future__ import annotations

from typing import Optional

CRITICAL_FLAGS = {
    "RUGCHECK_RUGGED": "RugCheck marks it as rugged",
    "PERMANENT_DELEGATE": "a permanent delegate can move anyone's tokens",
    "TRANSFER_HOOK": "transfers run custom code (can block selling)",
    "TRANSFER_FEE_HIGH": "high transfer tax",
    "NON_TRANSFERABLE": "tokens can't be transferred",
    "PAUSED": "transfers are paused",
    "PAUSABLE": "the dev can pause transfers",
    "DEFAULT_FROZEN": "new accounts start frozen",
}
HIGH_FLAGS = {
    "LP_NOT_LOCKED": "LP is not fully burned/locked — liquidity can be pulled",
    "CREATOR_PRIOR_RUGS": "the dev has rugged before",
    "CREATOR_DUMPING": "the dev is selling",
    "RECYCLED_SOCIALS": "socials recycled from other recent tokens (serial launcher)",
    "SERIAL_LAUNCHER": "the dev launches tokens in bulk",
    "LP_UNLOCK_SOON": "LP unlocks soon",
    "LIQUIDITY_REMOVAL": "liquidity dropped sharply from its recent high (possible pull)",
    "POSSIBLE_WASH_TRADING": "possible wash trading",
    "VOLUME_LIQUIDITY_IMPLAUSIBLE": "volume is implausible for the liquidity (wash trading?)",
    "BUYING_CONCENTRATED": "a few wallets do most of the buying",
    "POSSIBLE_COPYCAT": "a bigger token uses the same name/ticker (copycat)",
}
MEDIUM_FLAGS = {
    "RUGCHECK_DANGER": None,
    "POSSIBLE_MANIPULATION_REPEAT_BUYS": "the same wallets keep buying (possible manipulation)",
    "INSIDER_HOLDERS": "insiders hold supply",
    "TRANSFER_FEE_MUTABLE": "the transfer tax can be changed",
}
W = {"critical": 100, "high": 25, "medium": 10}


def _num(x) -> Optional[float]:
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _short(w: Optional[str]) -> str:
    return f"{w[:4]}…{w[-4:]}" if w and len(w) > 10 else (w or "?")


def bubble_summary(cl: dict) -> Optional[str]:
    """'Bubblemap: 2 linked groups — biggest holds 14% (shared funder) · ...' or None."""
    if not cl or not (cl.get("bubbles") or cl.get("clusters")):
        return None
    strong = [c for c in cl.get("clusters") or [] if c.get("strength") in ("STRONG", "CONFIRMED")]
    parts = []
    if strong:
        top = max(strong, key=lambda c: c.get("held_pct_of_supply_top20_only") or 0)
        parts.append(f"{len(strong)} linked wallet group{'s' if len(strong) != 1 else ''} — biggest holds "
                     f"{(top.get('held_pct_of_supply_top20_only') or 0):.1f}% across {top.get('size')} wallets"
                     + (" (linked to the dev)" if top.get("strength") == "CONFIRMED" else ""))
    else:
        parts.append("no linked wallet groups among top holders")
    if cl.get("two_hop_links"):
        parts.append(f"{cl['two_hop_links']} wallet{'s' if cl['two_hop_links'] != 1 else ''} linked through a middle wallet")
    if cl.get("hub_funders"):
        parts.append(f"{len(cl['hub_funders'])} exchange/hub funder{'s' if len(cl['hub_funders']) != 1 else ''} ignored")
    if cl.get("fresh_supply_pct") is not None:
        parts.append(f"fresh wallets (<24h) hold {cl['fresh_supply_pct']:.1f}%")
    return "Bubblemap: " + " · ".join(parts)


def assess(report: dict, s=None, stats: Optional[dict] = None) -> dict:
    """{level, score (0-100 risk), headline, red: [...], green: [...], unknown: [...], bubblemap}."""
    sec = report.get("security") or {}
    hold = report.get("holders") or {}
    cl = report.get("clusters") or {}
    tr = report.get("trades") or {}
    cr = report.get("creator") or {}
    flags = {f.get("code"): f for f in report.get("flags") or [] if isinstance(f, dict)}
    gates = {g.get("name"): g for g in report.get("gates") or [] if isinstance(g, dict)}
    pre = report.get("lifecycle") == "PRE_MIGRATION_BONDING_CURVE"
    red: list[dict] = []
    green: list[str] = []
    unknown: list[str] = []

    def r(level: str, text: str, key: str) -> None:
        if text and all(text != x["text"] for x in red):
            red.append({"level": level, "text": text, "key": key})

    # ---- traps: authorities and token mechanics
    for key, label in (("mint_authority", "the dev can still MINT new tokens"),
                       ("freeze_authority", "the dev can FREEZE wallets (you may not be able to sell)")):
        val = str(sec.get(key) or "")
        g = gates.get(key) or {}
        if val.startswith("ACTIVE") or g.get("status") == "FAIL":
            r("critical", label, key)
        elif val.startswith("REVOKED"):
            green.append(f"{key.replace('_', ' ')} revoked")
        else:
            unknown.append(f"{key.replace('_', ' ')} not confirmed")
    for code, text in CRITICAL_FLAGS.items():
        if code in flags:
            r("critical", text, code.lower())

    # ---- liquidity
    lp = str(sec.get("lp_status") or "")
    if pre:
        green.append("still on the bonding curve — no LP to pull yet")
    elif lp.startswith(("BURNED", "LOCKED")):
        green.append("LP burned/locked" + (" (verified on-chain)" if "on-chain" in lp else ""))
    elif lp.startswith("PARTIAL"):
        r("high", "LP only partly burned — the rest can be pulled", "lp_partial")
    elif lp.startswith("CONCENTRATED"):
        r("medium", "concentrated liquidity: the position owner can withdraw it", "lp_concentrated")
    else:
        unknown.append("LP burn/lock not confirmed")

    # ---- the bubblemap: connected wallets, fresh wallets, bundles
    max_cluster = _num(cl.get("max_cluster_supply_pct"))
    strong = [c for c in cl.get("clusters") or [] if c.get("strength") in ("STRONG", "CONFIRMED")]
    if any(c.get("strength") == "CONFIRMED" and (c.get("held_pct_of_supply_top20_only") or 0) >= 3 for c in strong):
        r("high", "wallets linked to the dev hold supply", "dev_linked_wallets")
    if max_cluster is not None:
        if max_cluster >= 20:
            r("high", f"one group of connected wallets holds {max_cluster:.1f}% of supply", "cluster_big")
        elif max_cluster >= 8:
            r("medium", f"connected wallets hold {max_cluster:.1f}% of supply", "cluster")
        elif cl.get("bubbles") or cl.get("wallets_aged"):
            green.append("top holders are not linked by funding" if not strong else
                         f"connected wallets hold only {max_cluster:.1f}%")
    elif cl.get("status") == "UNAVAILABLE" or not cl:
        unknown.append("wallet connections (bubblemap) not traced")
    if cl.get("two_hop_links"):
        r("medium", f"{cl['two_hop_links']} holder{'s' if cl['two_hop_links'] != 1 else ''} funded through a middle "
                    "wallet (hidden 2-hop link)", "two_hop")
    fresh_supply = _num(cl.get("fresh_supply_pct"))
    if fresh_supply is not None:
        if fresh_supply >= 15:
            r("high", f"brand-new wallets (<24h) hold {fresh_supply:.1f}% of supply", "fresh_supply_big")
        elif fresh_supply >= 5:
            r("medium", f"brand-new wallets (<24h) hold {fresh_supply:.1f}% of supply", "fresh_supply")
        else:
            green.append("top holders are mostly older wallets")
    fresh_share = _num(cl.get("fresh_wallet_share"))
    if fresh_share is not None and fresh_share >= 50 and (fresh_supply is None or fresh_supply < 5):
        r("high", f"{fresh_share:.0f}% of the wallets checked are brand new", "fresh_wallets")
    bundle = _num(cl.get("launch_bundle_pct"))
    if bundle is not None:
        if bundle >= 20:
            r("high", f"launch was bundled: {bundle:.1f}% bought in the first block(s)", "bundle_big")
        elif bundle >= 8:
            r("high", f"{bundle:.1f}% bought in the launch block(s)", "bundle")
        else:
            green.append("no heavy launch bundle")

    # ---- holders
    top10 = _num(hold.get("top10_adj"))
    single = _num(hold.get("max_single_adj"))
    if top10 is not None:
        if top10 >= 45:
            r("high", f"top 10 holders own {top10:.1f}%", "top10_big")
        elif top10 >= 28:
            r("medium", f"top 10 holders own {top10:.1f}%", "top10")
        elif top10 <= 20:
            green.append(f"supply spread out (top 10 own {top10:.1f}%)")
    else:
        unknown.append("holder concentration not measured")
    if single is not None and single >= 8:
        r("high", f"one wallet holds {single:.1f}%", "whale_holder")

    # ---- the dev
    cpct = _num(hold.get("creator_pct"))
    if cpct is not None:
        if cpct >= 10:
            r("high", f"dev still holds {cpct:.1f}%", "dev_holds_big")
        elif cpct >= 3:
            r("medium", f"dev holds {cpct:.1f}%", "dev_holds")
        elif cpct < 0.5:
            green.append("dev holds (almost) nothing")
    sold = _num(tr.get("creator_sold_pct_supply"))
    if sold and sold >= 1:
        r("high", f"dev sold {sold:.1f}% of supply after launch", "dev_sold")
    rugs = int(_num(cr.get("prior_rugged")) or 0)
    launches = _num(cr.get("prior_launches"))
    if rugs:
        r("high", f"dev's earlier tokens: {rugs} rugged", "dev_prior_rugs")
    elif launches is not None and cr.get("prior_checked"):
        green.append(f"dev history checked ({int(cr['prior_checked'])} earlier tokens, no rugs found)")
    elif str(cr.get("summary") or "UNKNOWN").startswith("UNKNOWN"):
        unknown.append("dev history unknown")
    for code, text in HIGH_FLAGS.items():
        if code in flags:
            r("high", text, code.lower())
    for code, text in MEDIUM_FLAGS.items():
        if code in flags:
            r("medium", text or (flags[code].get("message") or code)[:120], code.lower())

    # ---- demand quality
    buyers = _num(tr.get("unique_buyers"))
    if buyers is not None and buyers >= 60:
        green.append(f"{int(buyers)} different buyers in the sample")

    # ---- verdict
    # what this flag has led to before (learned from Northstar's own 24h tracking)
    for x in red:
        st = (stats or {}).get(x["key"])
        if st and st.get("n", 0) >= 10:
            x["crash_rate"], x["seen"] = round(st["crash"] / st["n"], 2), st["n"]
            if x["level"] == "medium" and x["crash_rate"] >= 0.8:
                x["level"], x["upgraded"] = "high", True
    # good signs soften the number a little but can never hide a red flag: any red flag keeps the
    # coin at CAUTION or worse, whatever else looks good
    raw = min(100, sum(W[x["level"]] for x in red))
    crit = [x["text"] for x in red if x["level"] == "critical"]
    highs = [x["text"] for x in red if x["level"] == "high"]
    score = 100 if crit else max(raw - 2 * min(len(green), 5), 20 if red else 0)
    key_unknown = [u for u in unknown if u.startswith(("mint", "freeze", "LP", "wallet", "holder"))]
    if crit:
        level, head = "RUG LIKELY", "Trap found: " + crit[0] + "."
    elif raw >= 50 or len(highs) >= 2:
        level, head = "HIGH RISK", "Several serious red flags: " + "; ".join(highs[:2] or [x["text"] for x in red][:2]) + "."
    elif red:
        level, head = "CAUTION", "Red flag" + ("s" if len(red) > 1 else "") + ": " + "; ".join(
            x["text"] for x in red[:2]) + ". Small size only."
    elif len(key_unknown) >= 2:
        level, head = "NOT ENOUGH DATA", "Key checks couldn't be completed: " + "; ".join(key_unknown[:3]) + "."
    else:
        level, head = "LOOKS CLEAN", "No serious red flags in the checks that ran" + (
            f" ({len(green)} good signs)." if green else ".")
    order = {"critical": 0, "high": 1, "medium": 2}
    return {"level": level, "score": score, "headline": head,
            "red": sorted(red, key=lambda x: order[x["level"]]),
            "green": green, "unknown": unknown, "bubblemap": bubble_summary(cl)}


EMOJI = {"RUG LIKELY": "☠️", "HIGH RISK": "🔴", "CAUTION": "🟠", "LOOKS CLEAN": "🟢", "NOT ENOUGH DATA": "⚪"}


def embed_text(v: dict, max_red: int = 4, max_green: int = 3) -> str:
    level = v.get("level") or "NOT ENOUGH DATA"
    lines = [f"{EMOJI.get(level, '')} **{level}** · risk {v.get('score', 0)}/100"]
    if v.get("headline"):
        lines.append(v["headline"])
    lines += [("⛔ " if r.get("level") == "critical" else "🔺 ") + r["text"]
              + (f" _(crashed {r['crash_rate'] * 100:.0f}% of {r['seen']} times)_" if r.get("crash_rate") is not None else "")
              for r in (v.get("red") or [])[:max_red]]
    lines += ["✅ " + g for g in (v.get("green") or [])[:max_green]]
    if v.get("bubblemap"):
        lines.append("🫧 " + v["bubblemap"].replace("Bubblemap: ", ""))
    return "\n".join(lines)
