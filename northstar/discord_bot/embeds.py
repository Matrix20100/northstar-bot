"""Discord embed builders (pure functions -> dicts usable with discord.Embed.from_dict).

Rules enforced here: every link uses the exact mint; UNKNOWN stays UNKNOWN; FDV is never shown as
market cap; "LOW" risk wording is impossible when critical data is missing (the decision layer
never publishes INCOMPLETE, and the label itself always says NOT RUG-PROOF); no guarantees.
"""
from __future__ import annotations

import json
import time
from typing import Optional


COLOR_CALLOUT = 0x2ECC71
COLOR_UPDATE = 0x3498DB
COLOR_WARNING = 0xE67E22
COLOR_CRITICAL = 0xE74C3C
COLOR_CLOSED = 0x95A5A6
DISCLAIMER = "Research signal, not a trade recommendation. Not rug-proof. Realized profit unknown."


def money(v: Optional[float]) -> str:
    if v is None:
        return "UNAVAILABLE"
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.2f}M"
    if v >= 1e3:
        return f"${v/1e3:.1f}K"
    return f"${v:,.2f}"


def price(v: Optional[float]) -> str:
    if v is None:
        return "UNAVAILABLE"
    return f"${v:.4g}" if v < 1 else f"${v:,.4f}"


def pct(v: Optional[float], signed: bool = True) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.1f}%" if signed else f"{v:.1f}%"


def age(hours: Optional[float]) -> str:
    if hours is None:
        return "UNKNOWN"
    if hours < 1:
        return f"{hours*60:.0f} min"
    if hours < 48:
        return f"{hours:.1f} h"
    return f"{hours/24:.1f} d"


def _clip(s: str, n: int = 1024) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _bullets(items: list[str], limit: int = 5) -> str:
    items = [i for i in items if i][:limit]
    return "\n".join(f"• {i}" for i in items) if items else "—"


def links(mint: str, pool: Optional[str]) -> str:
    out = [f"[DexScreener](https://dexscreener.com/solana/{pool or mint})",
           f"[Solscan token](https://solscan.io/token/{mint})",
           f"[RugCheck](https://rugcheck.xyz/tokens/{mint})"]
    if pool:
        out.append(f"[Pool on Solscan](https://solscan.io/account/{pool})")
    return " · ".join(out)


def callout_embed(report: dict, detected_at: Optional[float] = None, plan: Optional[list[str]] = None) -> dict:
    """Minimal tracked callout: token image, entry/current market cap, return and coin links."""
    mint = report["mint"]
    ident = report.get("identity") or {}
    market = report.get("market") or {}
    pool = report.get("pool") or {}
    score = report.get("score") or {}
    symbol = ident.get("symbol") or "?"
    name = ident.get("name") or "Unknown token"
    entry_mcap = (market.get("market_cap") or {}).get("value")
    image_url = market.get("image_url") or ident.get("image_url")
    token_url = f"https://dexscreener.com/solana/{mint}"
    narrative = report.get("narrative") or {}
    structure = report.get("market_structure") or {}
    components = score.get("components") or []
    narrative_component = next((c for c in components if c.get("name") == "narrative"), {})
    demand_component = next((c for c in components if c.get("name") == "demand"), {})
    moved = market.get("moved_during_research_pct")
    chart_lines = ([report["surge"]["line"]] if (report.get("surge") or {}).get("line") else []) + (
        [report["context"]["line"]] if (report.get("context") or {}).get("line") else []) + (
        [report["trending"]["line"]] if (report.get("trending") or {}).get("line") else []) + (
        [f"Moved {moved:+.0f}% while Northstar ran its checks; levels use the price at posting"]
        if isinstance(moved, (int, float)) and abs(moved) >= 3 else []) + [
                   f"Entry quality: **{structure.get('entry_quality', 'UNKNOWN')}**",
                   f"Observed snapshot return: {pct(structure.get('observed_return_pct'))}",
                   f"Observed max drawdown: {pct(structure.get('observed_drawdown_pct'), signed=False)}",
                   f"1h buy/sell count ratio: {structure.get('buy_sell_ratio_h1') if structure.get('buy_sell_ratio_h1') is not None else 'UNKNOWN'}",
                   f"Snapshot history: {structure.get('snapshots_used', 0)} prior observations (not OHLC candles)"]
    if structure.get("volume_acceleration") is not None:
        chart_lines.append(f"1h volume / 6h hourly average: {structure['volume_acceleration']:.2f}x")
    why_lines = [narrative.get("thesis") or narrative.get("summary") or "Narrative thesis unavailable."]
    why_lines.extend((narrative_component.get("evidence") or [])[1:3])
    why_lines.extend((demand_component.get("evidence") or [])[:2])
    risk_label = (report.get("risk") or "INCOMPLETE").replace("_", " ")
    caveats = report.get("caveats") or score.get("could_be_wrong") or []
    scalp = report.get("scalp") if report.get("call_type") == "RUG_SCALP" else None
    top = [
        {"name": "Called at market cap", "value": money(entry_mcap), "inline": True},
        {"name": "Current market cap", "value": money(entry_mcap), "inline": True},
        {"name": "Since callout", "value": "+0.0% · 1.00x", "inline": True},
        {"name": "Peak since callout", "value": f"{money(entry_mcap)} · 1.00x", "inline": True},
        *([{"name": "📋 Trade plan" if not scalp else "📋 Scalp plan", "value": _clip("\n".join(plan), 1024),
            "inline": False}] if plan else []),
    ]
    u = report.get("unified")
    if isinstance(u, dict) and u.get("version"):
        from ..analysis.unified import panel_text
        top.append({"name": "🎯 Decision", "value": _clip(panel_text(u), 1024), "inline": False})
    verdict = report.get("verdict")
    if isinstance(verdict, dict) and verdict.get("level"):
        from ..analysis.verdict import embed_text
        # the decision panel already carries the headline, so keep this to the detail
        top.append({"name": "🧠 Rug check", "value": _clip(embed_text(verdict, max_red=3, max_green=2), 1024),
                    "inline": False})
    brain = report.get("brain") or {}
    if isinstance(brain.get("p"), (int, float)) and not (isinstance(u, dict) and u.get("version")):
        top.append({"name": "🧠 Learned runner odds", "value": _clip(brain_text(brain), 1024), "inline": False})
    if isinstance(u, dict) and (u.get("entry") or u.get("invalidation")):
        lines = []
        entry = u.get("entry") or {}
        lines.append(("✅ Entry conditions look acceptable" if entry.get("ok")
                      else "⚠️ Entry conditions are not ideal") + ":")
        lines += [f"· {n}" for n in (entry.get("notes") or [])[:3]]
        if u.get("invalidation"):
            lines.append("**Thesis is dead if:**")
            lines += [f"· {x}" for x in u["invalidation"][:3]]
        if u.get("exit_triggers"):
            lines.append("**Re-check / exit on:**")
            lines += [f"· {x}" for x in u["exit_triggers"][:2]]
        top.append({"name": "🧭 Entry, invalidation and exit", "value": _clip("\n".join(lines), 1024),
                    "inline": False})
    if scalp:
        body = [
            {"name": "☠️ Why this is a rug", "value": _clip(_bullets(scalp.get("rug_reasons") or [], 6)),
             "inline": False},
            {"name": "⚡ Why it's still moving", "value": _clip(_bullets(chart_lines, 5)), "inline": False},
        ]
    else:
        body = [
            {"name": "Why Northstar selected it", "value": _clip(_bullets(why_lines, 4)), "inline": False},
            {"name": "Chart & flow evidence", "value": _clip(_bullets(chart_lines, 6)), "inline": False},
            {"name": "Risk / data limits",
             "value": _clip(f"**{risk_label}** — not rug-proof.\n" + _bullets(caveats, 2), 1024), "inline": False},
        ]
    fields = top + body + [{"name": "Links", "value": links(mint, pool.get("address")), "inline": False}]
    from ..scalp import BANNER
    embed = {
        "title": f"☠️ RUG SCALP — ${symbol}" if scalp else f"🚀 NORTHSTAR CALL — ${symbol}",
        "url": token_url,
        "description": (f"**{name}**\n`{mint}`\n\n{BANNER}" if scalp else f"**{name}**\n`{mint}`"),
        "color": COLOR_CRITICAL if scalp else COLOR_CALLOUT,
        "fields": fields,
        "footer": {"text": f"{'RUG — scalp only. ' if scalp else ''}{DISCLAIMER} · mint:{mint}"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(detected_at or time.time())),
        "_buttons": [{"label": "View on Axiom", "url": f"https://axiom.trade/t/{mint}"}],
    }
    if isinstance(image_url, str) and image_url.startswith(("https://", "http://")):
        embed["thumbnail"] = {"url": image_url}
    return fit(embed)


EMBED_LIMIT = 5800      # Discord rejects embeds over 6000 characters in total


def fit(e: dict, limit: int = EMBED_LIMIT) -> dict:
    """Trim the longest field values until the embed fits Discord's size limit."""
    guard = 0
    while embed_size(e) > limit and guard < 60:
        guard += 1
        fields = e.get("fields") or []
        if not fields:
            e["description"] = _clip(e.get("description", ""), max(200, len(e.get("description", "")) - 500))
            continue
        longest = max(fields, key=lambda f: len(f.get("value", "")))
        v = longest.get("value", "")
        if len(v) <= 120:
            break
        longest["value"] = _clip(v, max(120, int(len(v) * 0.7)))
    return e


def brain_text(b: dict) -> str:
    """'23% chance to 2x before -40% (avg coin 13%)' plus what pushed the odds, in plain words."""
    win, loss = b.get("win_x") or 2.0, b.get("loss_x") or 0.6
    line = f"**{b['p'] * 100:.0f}%** chance to {win:g}x before {(loss - 1) * 100:.0f}%"
    if isinstance(b.get("base"), (int, float)):
        line += f" (average coin: {b['base'] * 100:.0f}%)"
    parts = [line]
    if b.get("ups"):
        parts.append("▲ " + ", ".join(b["ups"]))
    if b.get("downs"):
        parts.append("▼ " + ", ".join(b["downs"]))
    if b.get("promoted"):
        parts.append("Called because the learning brain rated it highly.")
    if not b.get("trusted"):
        parts.append("_Still learning: these odds don't affect calls yet._")
    elif isinstance(b.get("auc"), (int, float)):
        parts.append(f"_Learned from {b.get('n', 0)} past coins · accuracy on unseen coins {b['auc']:.2f} (0.50 = coin flip)_")
    return "\n".join(parts)


def _sol(v) -> str:
    return f"{v:,.2f} SOL" if isinstance(v, (int, float)) else "UNAVAILABLE"


def usd_or_sol(sol_value, sol_usd) -> str:
    """Dollar value when a SOL/USD rate is known; otherwise the SOL value, clearly labelled."""
    if not isinstance(sol_value, (int, float)):
        return "UNAVAILABLE"
    if isinstance(sol_usd, (int, float)) and sol_usd > 0:
        return money(sol_value * sol_usd)
    return f"{sol_value:,.2f} SOL (USD unavailable)"


def early_watch_embed(report: dict) -> dict:
    """Compact pre-migration early-watch post (risk stays INCOMPLETE and is stated in the footer)."""
    mint = report.get("mint", "")
    ident = report.get("identity") or {}
    curve = report.get("pre_migration") or {}
    symbol = ident.get("symbol") or "?"
    name = ident.get("name") or "Unknown launch"
    score = float(curve.get("opportunity_score") or 0)
    delta = curve.get("market_cap_change_pct")
    buys, sells = curve.get("buys", 0), curve.get("sells", 0)
    lines = [
        "**Stage:** Bonding curve (pre-migration)",
        f"**Market Cap:** {usd_or_sol(curve.get('market_cap_sol_current'), curve.get('sol_usd'))}",
        f"**Since Launch:** {pct(delta) if delta is not None else 'UNAVAILABLE'}",
        f"**5m Trades:** {curve.get('trade_events', 0)} ({buys} buys / {sells} sells)",
        f"**Unique Buyers:** {curve.get('unique_buyers', 0)}",
        f"**5m Buy Flow:** {usd_or_sol(curve.get('buy_sol_5m', 0), curve.get('sol_usd'))}",
        f"**Activity Score:** {score:.0f}/100",
        f"**Address:** `{mint}`",
        f"**Links:** [Chart](https://dexscreener.com/solana/{mint}) · "
        f"[Pump.fun](https://pump.fun/coin/{mint}) · [Solscan](https://solscan.io/token/{mint})",
    ]
    embed = {"title": f"🛰️ EARLY WATCH — {name} (${symbol})",
             "url": f"https://dexscreener.com/solana/{mint}",
             "description": _clip("\n".join(lines), 4000), "color": COLOR_WARNING,
             "footer": {"text": f"Northstar Early Watch · Experimental, unproven · Risk INCOMPLETE · Not financial advice · mint:{mint}"},
             "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(report.get("ts") or time.time())),
             # Rendered by the publisher as link buttons under the embed (not sent to Discord as-is).
             "_buttons": [{"label": "View on Axiom", "url": f"https://axiom.trade/t/{mint}"}]}
    image_url = ident.get("image_url")
    if isinstance(image_url, str) and image_url.startswith(("https://", "http://")):
        embed["thumbnail"] = {"url": image_url}
    return embed


def plan_event_embed(*, symbol: str, name: str, mint: str, title: str, text: str, is_exit: bool,
                     early: bool = False, image_url: Optional[str] = None) -> dict:
    """A short NEW message (so people get pinged) when a trade-plan rule triggers."""
    kind = "Early Watch" if early else "Call"
    embed = {
        "title": f"{title} — {name} (${symbol})",
        "url": f"https://dexscreener.com/solana/{mint}",
        "description": _clip(f"{text}\n`{mint}`", 4000),
        "color": COLOR_CRITICAL if is_exit else COLOR_UPDATE,
        "footer": {"text": f"Northstar {kind} · rule-based plan, checked every few seconds; your fill may differ · "
                           f"Not financial advice · mint:{mint}"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "_buttons": [{"label": "View on Axiom", "url": f"https://axiom.trade/t/{mint}"}],
    }
    if isinstance(image_url, str) and image_url.startswith(("https://", "http://")):
        embed["thumbnail"] = {"url": image_url}
    return embed


def performance_field(alert: dict) -> dict:
    entry_mcap = alert.get("entry_mcap")
    current_mcap = alert.get("current_mcap")
    if entry_mcap and current_mcap is not None and entry_mcap > 0:
        multiple = current_mcap / entry_mcap
        gain = (multiple - 1) * 100
        metric = "market cap"
    else:
        entry_price = alert.get("entry_price")
        current_price = alert.get("current_price")
        multiple = current_price / entry_price if current_price is not None and entry_price else None
        gain = (multiple - 1) * 100 if multiple is not None else None
        metric = "price (market cap unavailable)"
    lines = [
        f"Called at: {money(entry_mcap)}",
        f"Current: {money(current_mcap)}",
        f"Change: {pct(gain)} · {multiple:.2f}x" if multiple is not None else "Change: UNAVAILABLE",
    ]
    return {"name": f"Performance ({metric})", "value": _clip("\n".join(lines)), "inline": False}


def plan_status(alert: dict) -> str:
    try:
        w = alert.get("warnings") or "[]"
        sent = {x[5:] for x in (json.loads(w) if isinstance(w, str) else w) if str(x).startswith("PLAN:")}
    except (TypeError, ValueError):
        return ""
    for kind, label in (("DANGER", "🚨 EXIT posted"), ("STOP", "🛑 stopped out"), ("TIME", "⏱️ time stop — exited"),
                        ("TRAIL", "🚪 trailing stop — exited"), ("TP2", "🎯 TP2 hit — trailing the rest"),
                        ("TP1", "🎯 TP1 hit — stop at break-even"), ("ENTRY_CLOSED", "⛔ entry closed — don't chase")):
        if kind in sent:
            return f"**Plan status:** {label}"
    return ""


def updated_callout(original: dict, alert: dict) -> dict:
    e = dict(original)
    fields = []
    entry_mcap = alert.get("entry_mcap")
    current_mcap = alert.get("current_mcap")
    if entry_mcap and current_mcap is not None and entry_mcap > 0:
        multiple = current_mcap / entry_mcap
        gain = (multiple - 1) * 100
    else:
        entry_price = alert.get("entry_price")
        current_price = alert.get("current_price")
        multiple = current_price / entry_price if current_price is not None and entry_price else None
        gain = (multiple - 1) * 100 if multiple is not None else None
    summary = {
        "Called at market cap": money(entry_mcap),
        "Current market cap": money(current_mcap),
        "Since callout": f"{pct(gain)} · {multiple:.2f}x" if multiple is not None else "UNAVAILABLE",
        PEAK_FIELD: peak_text(alert),
    }
    status = plan_status(alert)
    has_peak = any(f.get("name") == PEAK_FIELD for f in e.get("fields", []))
    for f in e.get("fields", []):
        if f.get("name") in summary:
            f = {**f, "value": summary[f["name"]]}
        elif f.get("name") == "📋 Trade plan" and status:
            base = f["value"].split("\n**Plan status:**")[0]
            f = {**f, "value": _clip(base + "\n" + status, 1024)}
        fields.append(f)
        if f.get("name") == "Since callout" and not has_peak:     # calls posted before this field existed
            fields.append({"name": PEAK_FIELD, "value": summary[PEAK_FIELD], "inline": True})
    e["fields"] = fields[:25]
    stamp = alert.get("last_update_at")
    if stamp:
        e["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))   # "updated at" in Discord
    return fit(e)


PEAK_FIELD = "Peak since callout"


def peak_text(alert: dict) -> str:
    """Highest market cap seen since the call (30s samples + 1-minute candle highs when available)."""
    entry = alert.get("entry_mcap")
    peak = alert.get("peak_mcap")
    mult = alert.get("max_multiple")
    if not peak and entry and mult:
        peak = entry * mult
    if not entry or not peak:
        return "UNAVAILABLE"
    x = peak / entry
    when = alert.get("peak_at")
    after = ""
    if when and alert.get("sent_at"):
        mins = max(0, (when - alert["sent_at"]) / 60)
        after = f" · {mins:.0f} min after call" if mins >= 1 else " · at the call"
    return f"{money(peak)} · {x:.2f}x{after}"


def warning_embed(alert: dict, kind: str, detail: str, critical: bool = True) -> dict:
    mint = alert["mint"]
    return {
        "title": f"⚠️ NORTHSTAR WARNING — ${alert.get('symbol') or '?'}",
        "description": _clip(f"**{kind}**\n{detail}\n\n`{mint}`", 4000),
        "color": COLOR_CRITICAL if critical else COLOR_WARNING,
        "fields": [performance_field(alert), {"name": "Links", "value": links(mint, alert.get("pool_address"))}],
        "footer": {"text": f"{DISCLAIMER} | mint:{mint}"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def milestone_embed(alert: dict, multiple: float) -> dict:
    mint = alert["mint"]
    symbol = alert.get("symbol") or "?"
    entry_mcap = alert.get("entry_mcap")
    current_mcap = alert.get("current_mcap")
    if entry_mcap and current_mcap is not None and entry_mcap > 0:
        observed_multiple = current_mcap / entry_mcap
        gain = (observed_multiple - 1) * 100
        basis = "market cap"
    else:
        entry_price = alert.get("entry_price")
        current_price = alert.get("current_price")
        observed_multiple = current_price / entry_price if current_price is not None and entry_price else None
        gain = (observed_multiple - 1) * 100 if observed_multiple is not None else None
        basis = "price (market cap unavailable)"
    token_url = f"https://dexscreener.com/solana/{mint}"
    embed = {
        "title": f"🎯 ${symbol} hit {multiple:g}x!",
        "url": token_url,
        "description": f"Milestone reached since the Northstar callout. Tracking basis: **{basis}**.\n`{mint}`",
        "color": COLOR_UPDATE,
        "fields": [
            {"name": "Called at market cap", "value": money(entry_mcap), "inline": True},
            {"name": "Current market cap", "value": money(current_mcap), "inline": True},
            {"name": "Gain since callout", "value": f"{pct(gain)} · {observed_multiple:.2f}x" if observed_multiple is not None else "UNAVAILABLE", "inline": True},
            {"name": "Links", "value": links(mint, alert.get("pool_address")), "inline": False},
        ],
        "footer": {"text": f"Observed milestone, not guaranteed executable profit · mint:{mint}"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return embed


def embed_size(e: dict) -> int:
    n = len(e.get("title", "")) + len(e.get("description", "")) + len((e.get("footer") or {}).get("text", ""))
    for f in e.get("fields", []):
        n += len(f.get("name", "")) + len(f.get("value", ""))
    return n


# ----------------------------------------------------------------------------- early narrative calls
COLOR_EARLY = 0xF1C40F
SOURCE_LABEL = {"launch_wave": "pump.fun launch wave", "news": "news", "trend": "trending searches",
                "wikipedia": "Wikipedia", "evergreen": "evergreen meme", "none": "no live event"}


def _ai_block(ai: dict) -> str:
    ev = ai.get("event")
    lines = [f"**{ai.get('narrative') or 'Narrative'}**"]
    if ev:
        lines.append(f"📰 Rides: {ev} _({SOURCE_LABEL.get(ai.get('event_source'), ai.get('event_source'))})_")
    lines.append(f"🤖 AI score **{ai.get('score', 0)}/100** · fit {ai.get('fit', 0)} · originality "
                 f"{ai.get('originality', 0)} · attention {ai.get('attention', 0)} · timing {ai.get('timing', '?')}")
    if ai.get("why"):
        lines.append(ai["why"])
    if ai.get("red_flags"):
        lines.append("⚠️ " + "; ".join(ai["red_flags"][:3]))
    return "\n".join(lines)


def early_call_embed(report: dict, call: dict, plan: Optional[list[str]] = None) -> dict:
    """⚡ EARLY NARRATIVE CALL. ``call``: entry_mcap, current_mcap, peak_mcap, peak_at, sent_at,
    phase (CURVE/POOL), status_line, last_update_at. Rebuilt on every edit."""
    mint = report.get("mint", "")
    ident = report.get("identity") or {}
    pm = report.get("pre_migration") or {}
    ai = report.get("ai") or {}
    ctx = report.get("context") or {}
    symbol, name = ident.get("symbol") or "?", ident.get("name") or "Unknown"
    entry, cur, peak = call.get("entry_mcap"), call.get("current_mcap"), call.get("peak_mcap")
    mult = cur / entry if cur and entry else None
    top = [
        {"name": "Called at market cap", "value": money(entry), "inline": True},
        {"name": "Current market cap", "value": money(cur) if cur else money(entry), "inline": True},
        {"name": "Since callout", "value": f"{pct((mult - 1) * 100)} · {mult:.2f}x" if mult else "+0.0% · 1.00x",
         "inline": True},
        {"name": PEAK_FIELD, "value": peak_text({"entry_mcap": entry, "peak_mcap": peak or entry,
                                                 "peak_at": call.get("peak_at"), "sent_at": call.get("sent_at")}),
         "inline": True},
        {"name": "Stage", "value": "🟣 Bonding curve" if call.get("phase", "CURVE") == "CURVE" else "🟢 Migrated (PumpSwap)",
         "inline": True},
    ]
    if ai:
        top.append({"name": "⚡ Narrative (AI)", "value": _clip(_ai_block(ai), 1024), "inline": False})
    live = []
    for w in (ctx.get("waves") or [])[:2]:
        live.append(f"🌊 \"{w['term']}\": {w['launches_30m']} launches by {w.get('creators', '?')} creators in 30 min")
    for k, label in (("news", "📰"), ("trends", "🔎"), ("wiki", "📚")):
        for item in (ctx.get("related") or {}).get(k, [])[:1]:
            live.append(f"{label} {item}")
    if ctx.get("line"):
        live.append(ctx["line"])
    if live:
        top.append({"name": "🌍 Live attention", "value": _clip("\n".join(live), 1024), "inline": False})
    flow = (f"{pm.get('buys', 0)} buys / {pm.get('sells', 0)} sells · {pm.get('unique_buyers', 0)} buyers · "
            f"{usd_or_sol(pm.get('buy_sol_5m'), pm.get('sol_usd'))} bought (5 min)")
    top.append({"name": "📈 Flow", "value": flow, "inline": False})
    if plan:
        top.append({"name": "📋 Early plan", "value": _clip("\n".join(plan) + (
            f"\n**Plan status:** {call['status_line']}" if call.get("status_line") else ""), 1024), "inline": False})
    u = report.get("unified")
    if isinstance(u, dict) and u.get("version"):
        from ..analysis.unified import panel_text
        top.append({"name": "🎯 Decision", "value": _clip(panel_text(u, max_lines=2), 1024), "inline": False})
    verdict = report.get("verdict")
    if isinstance(verdict, dict) and verdict.get("level"):
        from ..analysis.verdict import embed_text
        top.append({"name": "🧠 Rug check", "value": _clip(embed_text(verdict, max_red=3, max_green=2), 1024),
                    "inline": False})
    top.append({"name": "Links", "value": links(mint, None) + f" · [Pump.fun](https://pump.fun/coin/{mint})",
                "inline": False})
    stamp = call.get("last_update_at") or call.get("sent_at") or time.time()
    embed = {"title": f"⚡ EARLY NARRATIVE CALL — ${symbol}", "url": f"https://dexscreener.com/solana/{mint}",
             "description": f"**{name}**\n`{mint}`\nEarly = higher risk: small size, follow the plan.",
             "color": COLOR_EARLY, "fields": top,
             "footer": {"text": f"Early narrative call · AI-judged, not financial advice · {DISCLAIMER} · mint:{mint}"},
             "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp)),
             "_buttons": [{"label": "View on Axiom", "url": f"https://axiom.trade/t/{mint}"}]}
    if isinstance(ident.get("image_url"), str) and ident["image_url"].startswith(("https://", "http://")):
        embed["thumbnail"] = {"url": ident["image_url"]}
    return fit(embed)


def early_free_embed(report: dict, call: dict) -> dict:
    """Free-channel proof post once a VIP early call passes the free threshold."""
    mint = report.get("mint", "")
    ident = report.get("identity") or {}
    ai = report.get("ai") or {}
    entry, peak = call.get("entry_mcap"), call.get("peak_mcap")
    x = peak / entry if peak and entry else None
    mins = (call.get("peak_at") or time.time()) - (call.get("sent_at") or time.time())
    lines = [f"Called in **VIP** at **{money(entry)}** — reached **{money(peak)} ({x:.2f}x)** "
             f"{max(0, mins) / 60:.0f} min later." if x else "Called in VIP.",
             f"Narrative: {ai.get('narrative')}" + (f" — {ai.get('event')}" if ai.get("event") else "")
             if ai else "", "", "Want the next one at the entry? Join VIP."]
    return fit({"title": f"✅ VIP EARLY CALL UP {((x or 1) - 1) * 100:+.0f}% — ${ident.get('symbol') or '?'}",
                "url": f"https://dexscreener.com/solana/{mint}", "description": "\n".join(l for l in lines if l is not None),
                "color": COLOR_CALLOUT, "footer": {"text": f"{DISCLAIMER} · mint:{mint}"},
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
