"""Narrative intelligence.

Evidence sources actually used (nothing else is claimed):
  * Token identity: name, symbol, DexScreener profile description/links/image          (OBSERVED)
  * Timeliness: DexScreener trending metas (/metas/trending/v1) — category-level market
    cap/volume momentum and token counts                                              (OBSERVED)
  * Saturation: DexScreener search for tokens sharing the ticker/name                  (OBSERVED)
  * Paid promotion (DexScreener boosts) is recorded but never counted as organic demand.

  * Free social checks (analysis/socials.py): the token's own X/Telegram/website links, links
    RECYCLED from other recent tokens (serial-launcher sign), and public Telegram member counts
    (weak; can be botted). Worth at most 4 points.

Without free social evidence the attention sub-score is 0 and confidence is lowered.
X activity (posts, followers, engagement) is NOT measured (no free X read access) — stated in output.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

from ..models import Confidence, DataStatus, EvidenceStrength, Flag, PairInfo, ScoreComponent, Severity

CATEGORIES = {
    "AI / agents": {"ai", "agent", "agents", "gpt", "llm", "bot", "neural", "agi", "robot", "claude", "grok"},
    "Animal meme": {"dog", "doge", "cat", "frog", "pepe", "inu", "shib", "monkey", "ape", "bird", "fish",
                    "penguin", "hamster", "bear", "bull", "wif", "bonk", "goat", "pig", "duck", "rat", "capybara"},
    "Political / public figure": {"trump", "maga", "biden", "president", "elon", "musk", "vote", "election"},
    "Gaming / internet culture": {"game", "gamer", "pixel", "meme", "based", "chad", "wojak", "npc", "gm", "lol"},
    "Event / news": {"olympics", "worldcup", "cup", "war", "launch", "halloween", "christmas", "summit"},
    "Ecosystem": {"sol", "solana", "jup", "pump", "raydium", "defi"},
}
STOP = {"the", "and", "coin", "token", "on", "of", "a", "to", "for", "in", "is", "official", "community", "first",
        "new", "solana", "sol", "meme", "memecoin", "with", "this", "that", "just", "our"}


@dataclass
class NarrativeResult:
    category: str
    summary: str
    component: ScoreComponent
    confidence: Confidence
    evidence_items: int
    social_status: DataStatus
    meta_status: DataStatus
    attention_windows: dict = field(default_factory=dict)
    competing_tokens: Optional[int] = None
    matched_meta: Optional[str] = None
    flags: list[Flag] = field(default_factory=list)
    thesis: str = ""
    catalyst_evidence: list[str] = field(default_factory=list)
    narrative_risks: list[str] = field(default_factory=list)
    attention_direction: str = "UNKNOWN"
    identity_match_quality: str = "UNASSESSED"
    free_socials: Optional[dict] = None

    def to_dict(self) -> dict:
        return {"category": self.category, "summary": self.summary, "component": self.component.to_dict(),
                "confidence": self.confidence.value, "evidence_items": self.evidence_items,
                "social_status": self.social_status.value, "meta_status": self.meta_status.value,
                "attention_windows": self.attention_windows, "competing_tokens": self.competing_tokens,
                "matched_meta": self.matched_meta, "thesis": self.thesis,
                "catalyst_evidence": self.catalyst_evidence, "narrative_risks": self.narrative_risks,
                "attention_direction": self.attention_direction,
                "identity_match_quality": self.identity_match_quality, "free_socials": self.free_socials}


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{2,}", (text or "").lower()) if w not in STOP}


def categorize(name: str, symbol: str, desc: str) -> str:
    ws = words(f"{name} {symbol} {desc}")
    best, hits = "Uncategorized / novel", 0
    for cat, kws in CATEGORIES.items():
        h = len(ws & kws)
        if h > hits:
            best, hits = cat, h
    return best


def analyze_narrative(pair: PairInfo, profile: Optional[dict], metas: Optional[list[dict]],
                      competitors: Optional[list[PairInfo]],
                      weight: float, now: Optional[float] = None, social=None) -> NarrativeResult:
    now = now or time.time()
    name, sym = pair.base_name or "", pair.base_symbol or ""
    desc = (profile or {}).get("description") or ""
    links = (profile or {}).get("links") or []
    ev: list[str] = []
    pen: list[str] = []
    miss: list[str] = []
    flags: list[Flag] = []
    evidence_items = 0

    # ---- A. clarity (0-5) — INFERRED from identity fields
    clarity = 0.0
    if name and len(name) <= 30:
        clarity += 1
    if re.fullmatch(r"[A-Za-z0-9]{2,10}", sym or ""):
        clarity += 1
    if len(desc) >= 20:
        clarity += 1.5
        ev.append(f"profile description present ({len(desc)} chars)")
    else:
        miss.append("no DexScreener profile description")
    if pair.image_url or (profile or {}).get("icon"):
        clarity += 0.5
    social_links = [l for l in links if isinstance(l, dict)] + pair.socials
    if social_links or pair.websites:
        clarity += 1
    else:
        miss.append("no website or social links listed")
    if sym and name and (sym.lower() in name.lower().replace(" ", "") or
                         "".join(w[0] for w in name.split() if w).lower() == sym.lower()):
        clarity = min(5, clarity + 0.5)
    clarity = min(clarity, 5)

    # ---- B. timeliness (0-7) — DexScreener trending metas
    timeliness = 0.0
    matched = None
    if metas is None:
        meta_status = DataStatus.UNAVAILABLE
        miss.append("trending metas unavailable")
    else:
        meta_status = DataStatus.OBSERVED
        tw = words(f"{name} {sym} {desc}")
        identity_words = words(f"{name} {sym}")
        # A generic word alone in a long description is weak. A direct name/symbol match
        # to a meta slug is retained as a category-level hypothesis, not proof of a catalyst.
        generic_meta_words = {"ai", "agent", "meme", "coin", "token", "solana", "sol", "pump",
                              "dog", "cat", "frog", "pepe", "inu", "bot", "gpt"}
        ranked_metas = []
        for m in metas:
            meta_words = words(f"{m.get('name', '')} {str(m.get('slug', '')).replace('-', ' ')}")
            overlap = tw & meta_words
            distinctive = overlap - generic_meta_words
            identity_overlap = identity_words & meta_words
            direct_slug = words(str(m.get('slug', '')).replace('-', ' '))
            category_identity_match = any(len(w) >= 4 for w in identity_overlap & direct_slug)
            if distinctive:
                ranked_metas.append((2, len(distinctive), len(overlap), m))
            elif category_identity_match:
                ranked_metas.append((1, len(identity_overlap), len(overlap), m))
        if ranked_metas:
            ranked_metas.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
            matched = ranked_metas[0][3]
        if matched:
            mc = matched.get("marketCapChange") or {}
            timeliness += 3
            evidence_items += 1
            ev.append(f"possible connection to trending DexScreener meta '{matched.get('name')}' "
                      f"({matched.get('tokenCount', '?')} tokens; name/description overlap, not proof of catalyst)")
            h1, h6 = _num(mc.get("h1")), _num(mc.get("h6"))
            if h6 is not None and h6 > 0:
                timeliness += 2
                ev.append(f"meta market cap {h6:+.0f}% over 6h")
            elif h6 is not None:
                pen.append(f"meta market cap {h6:+.0f}% over 6h (fading)")
            if h1 is not None and h1 > 0:
                timeliness += 2
            elif h1 is not None:
                pen.append(f"meta {h1:+.0f}% over 1h")
            tc = _num(matched.get("tokenCount"))
            if tc and tc > 200:
                timeliness -= 1.5
                pen.append(f"meta crowded ({int(tc)} tokens)")
        else:
            miss.append("no trending DexScreener meta matches this token's name/description")
    timeliness = max(0.0, min(timeliness, 7))

    # ---- C. attention (0-4): free social checks only
    attention = 0.0
    windows: dict = {}
    social_status = DataStatus.UNAVAILABLE
    if social is None:
        miss.append("social attention not measured; X posts/followers are never analysed")
    # free social checks: own links, recycled links, public Telegram size (max 4 points)
    from .socials import score_attention
    free_pts, free_ev, free_pen, free_items = score_attention(social)
    attention += free_pts
    ev += free_ev
    pen += free_pen
    evidence_items += free_items
    if social is not None and social.recycled:
        flags.append(Flag("RECYCLED_SOCIALS", Severity.HIGH,
                          "token reuses X/Telegram/website links from other recent tokens: "
                          + ", ".join(social.recycled), DataStatus.OBSERVED, EvidenceStrength.STRONG))
    attention = max(0.0, min(attention, 4))

    # ---- D. saturation / distinct identity (0-5)
    saturation = 0.0
    n_comp = None
    if competitors is None:
        miss.append("competing-token search unavailable")
    else:
        same = [c for c in competitors if c.base_mint != pair.base_mint and
                ((c.base_symbol or "").lower() == sym.lower() or (c.base_name or "").lower() == name.lower())]
        n_comp = len({c.base_mint for c in same})
        bigger = [c for c in same if (c.liquidity_usd or 0) > (pair.liquidity_usd or 0)]
        if bigger:
            flags.append(Flag("POSSIBLE_COPYCAT", Severity.HIGH,
                              f"{len({c.base_mint for c in bigger})} other token(s) with the same ticker/name have "
                              "more liquidity — this may be a copycat", DataStatus.OBSERVED, EvidenceStrength.WEAK))
            pen.append("not the leading token for its ticker/name")
        else:
            saturation = 5 if n_comp == 0 else 3 if n_comp <= 3 else 1 if n_comp <= 8 else 0
            if n_comp <= 3:
                evidence_items += 1
                ev.append("distinct identity: " + ("no other token shares its ticker/name"
                                                   if n_comp == 0 else f"leads {n_comp} same-ticker tokens"))
            else:
                pen.append(f"{n_comp} tokens share this ticker/name (overcrowded)")

    if pair.boosts_active:
        ev.append(f"paid DexScreener boost active ({pair.boosts_active}) — promotion, not counted as organic")

    raw = clarity + timeliness + attention + saturation  # out of 25
    points = round(raw / 25 * weight, 2)
    category = categorize(name, sym, desc)
    if free_items or (meta_status == DataStatus.OBSERVED and evidence_items >= 1):
        conf = Confidence.MEDIUM
    else:
        conf = Confidence.LOW
    summary = f"{category}: '{name}' (${sym})"
    if matched:
        summary += f" — possible connection to trending meta '{matched.get('name')}'"
    if desc:
        summary += f". {desc[:140].strip()}"

    # This is a transparent hypothesis, not a language-model claim about the project.
    catalyst_evidence = []
    narrative_risks = []
    if matched:
        catalyst_evidence.append(f"Possible trend connection: {matched.get('name')} (name/description token overlap; not proof of a catalyst)")
    if not desc:
        narrative_risks.append("No profile description; project purpose and narrative claims are hard to verify")
    if not social_links and not pair.websites:
        narrative_risks.append("No listed website or social links to independently inspect")
    if pair.boosts_active:
        narrative_risks.append("Paid boost detected; promotion is not evidence of organic demand")
    if n_comp is not None and n_comp > 8:
        narrative_risks.append(f"Crowded ticker/name space: {n_comp} competing tokens found")
    if social is not None and social.recycled:
        narrative_risks.append("Socials recycled from other recent tokens (serial-launcher pattern)")
    if not free_items:
        narrative_risks.append("Independent social attention unavailable; do not infer community demand")
    attention_direction = "UNKNOWN"
    identity_quality = "SUPPORTED" if desc and (social_links or pair.websites) else "PARTIAL" if (desc or social_links or pair.websites) else "WEAK"
    thesis = f"Hypothesis: {category.lower()} narrative."
    if matched:
        thesis += f" There is a possible connection to the '{matched.get('name')}' trend, but overlap alone does not prove the token is relevant."
    else:
        thesis += " No distinctive match to the tracked trending metas was established."
    if catalyst_evidence:
        thesis += " Evidence to investigate: " + "; ".join(catalyst_evidence) + "."
    if narrative_risks:
        thesis += " Limitations: " + "; ".join(narrative_risks) + "."

    comp = ScoreComponent("narrative", points, weight, ev, pen, miss)
    comp.evidence.insert(0, f"sub-scores: clarity {clarity:.1f}/5, timeliness {timeliness:.1f}/7, "
                            f"social attention {attention:.1f}/4, distinctness {saturation:.1f}/5")
    return NarrativeResult(category, summary, comp, conf, evidence_items, social_status, meta_status,
                           windows, n_comp, (matched or {}).get("name"), flags, thesis,
                           catalyst_evidence, narrative_risks, attention_direction, identity_quality,
                           social.to_dict() if social is not None else None)


def _num(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
