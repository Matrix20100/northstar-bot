"""The learning brain: a model Northstar trains on its own history to learn which coins actually run.

Hand-written scores are guesses ("narrative is worth 25 points"). The brain replaces guessing with
evidence. Every coin Northstar researches is recorded with ~40 numbers that describe it at that
moment (money flow, buyers, holders, liquidity, chart, radar, trending...). Two hours later the real
minute-by-minute price path tells us how it went. The brain learns which numbers separate the coins
that ran from the ones that didn't.

  RUNNER = the price reached 2x the entry BEFORE it fell to 0.6x (worst case when both happen in the
           same minute: counted as a loss). Northstar is tuned to find the biggest runners, so that is
           what the brain learns to spot. Both levels are settings (NORTHSTAR_BRAIN_WIN_X / _LOSS_X).

Where the examples come from
  * shadows: every researched coin, with its minute-by-minute price path for 2 hours (CoinGecko);
  * decision tracking: Northstar already follows every decision for 24 hours (highest and lowest
    price seen). A coin that reached 2x there counts as a runner. That source has no minute order, so
    it is only used for coins that have no shadow path, and it gives the brain data from day one.

The model is a regularised logistic regression written in plain Python (no extra packages), so the
odds it gives are real probabilities and every prediction can be explained: each number pushes the
odds up or down by an amount the bot can show you.

Honesty rules built in
  * Walk-forward validation: the model is always judged on coins that came AFTER the ones it learned
    from (like real life). The score shown is AUC: 0.50 = coin flip, 1.00 = perfect.
  * It only gets a say in live decisions when it is TRUSTED: enough coins, enough winners, and it
    beats a coin flip both overall and on the most recent coins. If it gets worse (markets change),
    it loses its say automatically until it earns it back.
  * It never touches the rug checks. A coin that fails or can't confirm a safety check is never
    rescued by the brain.
"""
from __future__ import annotations

import json
import logging
import math
import time
from typing import Optional

log = logging.getLogger("northstar.brain")

WIN_X = 2.0            # a "runner" reaches this multiple ...
LOSS_X = 0.6           # ... before touching this one
TRACK_MIN_HOURS = 6.0  # decision-tracking rows: followed at least this long (or already a runner)
L2 = 1.0               # regularisation strength (standardised features)
MAX_ROWS = 5000        # newest coins used per training run
FOLDS = 5              # walk-forward folds

SCHEMA = """
CREATE TABLE IF NOT EXISTS brain_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    n INTEGER NOT NULL,
    positives INTEGER NOT NULL,
    auc REAL,
    recent_auc REAL,
    trusted INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL
);
"""

# feature name -> plain-English label (shown in Discord)
LABELS = {
    "log_mcap": "market cap", "log_liq": "liquidity", "liq_to_mcap": "liquidity vs market cap",
    "log_vol_m5": "5-min volume", "log_vol_h1": "1-hour volume", "log_vol_h6": "6-hour volume",
    "buy_share_m5": "share of buys (5 min)", "buy_share_h1": "share of buys (1 hour)",
    "log_txns_m5": "trade count (5 min)", "chg_m5": "price change 5 min", "chg_h1": "price change 1 hour",
    "chg_h6": "price change 6 hours", "log_age_h": "pool age", "boosted": "paid DexScreener boost",
    "vol_acc": "volume acceleration", "bs_h1": "buy/sell ratio (1 hour)", "extended": "already extended",
    "fading": "fading chart", "constructive": "constructive chart", "vertical_thin": "vertical on thin liquidity",
    "pullback": "pullback from high", "drawdown": "observed drawdown",
    "score": "Northstar score", "c_narrative": "narrative points", "c_demand": "demand points",
    "c_liquidity": "liquidity points", "c_holders": "holder points", "c_momentum": "momentum points",
    "c_wallet_quality": "wallet-quality points", "c_evidence": "evidence points",
    "top10": "top-10 holders %", "max_single": "largest holder %", "creator_pct": "creator holdings %",
    "insider_pct": "insider holdings %", "log_buyers": "unique buyers", "buys_per_buyer": "buys per buyer",
    "top5_buyer_share": "top-5 buyers' share", "buyers_rising": "buyers increasing",
    "round_trip": "round-trip wallets", "cluster_pct": "largest wallet cluster %", "bundle_pct": "launch bundle %",
    "fresh_share": "fresh wallets share", "risk_moderate": "moderate risk", "confidence": "evidence confidence",
    "n_failed": "failed checks", "n_unknown": "unconfirmed checks", "surge": "radar surge",
    "log_spike": "surge strength", "trending": "CoinGecko trending", "trend_rank": "trending rank",
    "rug_score": "rug-check risk score", "fresh_supply": "supply held by fresh wallets",
    "linked_groups": "linked wallet groups", "two_hop": "hidden 2-hop funding", "hub_funded": "exchange-funded holders",
}
FEATURES = list(LABELS)


def init_schema(db) -> None:
    with db._lock:
        db._conn.executescript(SCHEMA)
    cols = {r["name"] for r in db.fetchall("PRAGMA table_info(shadow_calls)")}
    for col, typ in (("brain_p", "REAL"), ("brain_oof", "REAL"), ("vector", "TEXT"), ("win", "INTEGER")):
        if cols and col not in cols:
            db.execute(f"ALTER TABLE shadow_calls ADD COLUMN {col} {typ}")
    tcols = {r["name"] for r in db.fetchall("PRAGMA table_info(decision_tracking)")}
    if tcols and "brain_vector" not in tcols:
        # research snapshots are pruned after a week; the brain keeps its own copy of the numbers
        db.execute("ALTER TABLE decision_tracking ADD COLUMN brain_vector TEXT")
    if tcols and "verdict_keys" not in tcols:
        db.execute("ALTER TABLE decision_tracking ADD COLUMN verdict_keys TEXT")


# ----------------------------------------------------------------------------- features
def _num(x) -> Optional[float]:
    if isinstance(x, dict):
        x = x.get("value")
    if isinstance(x, bool):
        return 1.0 if x else 0.0
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _log(x, floor: float = 1.0) -> Optional[float]:
    v = _num(x)
    return math.log10(max(v, floor)) if v is not None else None


def _clip(x, lo, hi) -> Optional[float]:
    v = _num(x)
    return None if v is None else max(lo, min(hi, v))


def _share(t: Optional[dict]) -> Optional[float]:
    if not isinstance(t, dict):
        return None
    b, s = _num(t.get("buys")) or 0, _num(t.get("sells")) or 0
    return b / (b + s) if b + s > 0 else None


def vector(report: dict, ts: Optional[float] = None) -> dict:
    """The ~50 numbers the brain learns from, all taken from a research report (None = not known)."""
    m = report.get("market") or {}
    vol = m.get("volume") or {}
    tx = m.get("txns") or {}
    pc = m.get("price_change") or {}
    ms = report.get("market_structure") or {}
    sc = report.get("score") or {}
    comps = {c.get("name"): c for c in sc.get("components") or [] if isinstance(c, dict)}
    h = report.get("holders") or {}
    tr = report.get("trades") or {}
    cl = report.get("clusters") or {}
    gates = [g for g in report.get("gates") or [] if isinstance(g, dict)]
    surge = report.get("surge") or {}
    trend = report.get("trending") or {}
    mcap, liq = _num(m.get("market_cap")), _num(m.get("liquidity_usd"))
    created = _num(m.get("pair_created_at"))
    ts = ts or _num(m.get("fetched_at")) or time.time()
    tx5 = tx.get("m5") if isinstance(tx, dict) else None
    v = {
        "log_mcap": _log(mcap), "log_liq": _log(liq),
        "liq_to_mcap": _clip(liq / mcap, 0, 2) if liq and mcap else None,
        "log_vol_m5": _log(vol.get("m5")), "log_vol_h1": _log(vol.get("h1")), "log_vol_h6": _log(vol.get("h6")),
        "buy_share_m5": _share(tx5), "buy_share_h1": _share(tx.get("h1") if isinstance(tx, dict) else None),
        "log_txns_m5": (math.log10(1 + (_num(tx5.get("buys")) or 0) + (_num(tx5.get("sells")) or 0))
                        if isinstance(tx5, dict) else None),
        "chg_m5": _clip(pc.get("m5"), -90, 300), "chg_h1": _clip(pc.get("h1"), -95, 1000),
        "chg_h6": _clip(pc.get("h6"), -99, 3000),
        "log_age_h": math.log10(1 + max(0.0, (ts - created) / 3600)) if created else None,
        "boosted": 1.0 if (_num(m.get("boosts_active")) or 0) > 0 else 0.0,
        "vol_acc": _clip(ms.get("volume_acceleration"), 0, 20),
        "bs_h1": _clip(ms.get("buy_sell_ratio_h1"), 0, 10),
        "extended": _num(ms.get("extended")), "fading": _num(ms.get("fading")),
        "constructive": _num(ms.get("constructive")), "vertical_thin": _num(ms.get("vertical_thin")),
        "pullback": _clip(ms.get("pullback_pct_from_high"), 0, 100),
        "drawdown": _clip(ms.get("observed_drawdown_pct"), -100, 100),
        "score": _num(sc.get("total")),
        "top10": _num(h.get("top10_adj")), "max_single": _num(h.get("max_single_adj")),
        "creator_pct": _num(h.get("creator_pct")), "insider_pct": _num(h.get("insider_pct")),
        "log_buyers": math.log10(1 + _num(tr.get("unique_buyers"))) if _num(tr.get("unique_buyers")) is not None
        and tr.get("sample_size") else None,
        "buys_per_buyer": _clip(tr.get("buys_per_buyer"), 0, 20),
        "top5_buyer_share": _num(tr.get("top5_buyer_share")),
        "buyers_rising": {"rising": 1.0, "flat": 0.0, "declining": -1.0}.get(str(tr.get("buyer_trend")).lower()),
        "round_trip": (_num(tr.get("round_trip_wallets")) or 0) / max(1, _num(tr.get("unique_buyers")) or 1)
        if tr.get("sample_size") else None,
        "cluster_pct": _num(cl.get("max_cluster_supply_pct")), "bundle_pct": _num(cl.get("launch_bundle_pct")),
        "fresh_share": _num(cl.get("fresh_wallet_share")),
        "risk_moderate": 1.0 if report.get("risk") == "MODERATE" else 0.0,
        "confidence": {"LOW": 0.0, "MEDIUM": 1.0, "HIGH": 2.0}.get(report.get("confidence")),
        "n_failed": float(sum(1 for g in gates if g.get("status") == "FAIL")),
        "n_unknown": float(sum(1 for g in gates if g.get("status") == "UNKNOWN")),
        "surge": 1.0 if surge else 0.0,
        "log_spike": math.log10(max(1.0, _num(surge.get("spike")) or 1.0)) if surge else 0.0,
        "trending": 1.0 if trend else 0.0,
        "trend_rank": 1.0 / max(1.0, _num(trend.get("rank")) or 1.0) if trend else 0.0,
        "rug_score": _num((report.get("verdict") or {}).get("score")),
        "fresh_supply": _clip(cl.get("fresh_supply_pct"), 0, 100),
        "linked_groups": float(sum(1 for c in cl.get("clusters") or [] if isinstance(c, dict)
                                   and c.get("strength") in ("STRONG", "CONFIRMED"))) if cl.get("bubbles") else None,
        "two_hop": _num(cl.get("two_hop_links")) if cl.get("bubbles") else None,
        "hub_funded": float(len(cl.get("hub_funders") or [])) if cl.get("bubbles") else None,
    }
    for name in ("narrative", "demand", "liquidity", "holders", "momentum", "wallet_quality", "evidence"):
        c = comps.get(name) or {}
        pts, mx = _num(c.get("points")), _num(c.get("max_points")) or _num(c.get("weight"))
        v["c_" + name] = pts / mx if pts is not None and mx else None
    return {k: v.get(k) for k in FEATURES}


def outcome(path: list, win_x: float = WIN_X, loss_x: float = LOSS_X) -> Optional[int]:
    """1 if the path hit win_x before loss_x (same minute = loss), else 0. None if no path."""
    if not path:
        return None
    for _m, high, low, _c in path:
        if low <= loss_x:
            return 0
        if high >= win_x:
            return 1
    return 0


# ----------------------------------------------------------------------------- model
def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (small dense systems)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            continue
        m[col], m[piv] = m[piv], m[col]
        p = m[col][col]
        for r in range(col + 1, n):
            f = m[r][col] / p
            if f:
                mr, mc = m[r], m[col]
                for k in range(col, n + 1):
                    mr[k] -= f * mc[k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        if abs(m[r][r]) < 1e-12:
            continue
        x[r] = (m[r][n] - sum(m[r][k] * x[k] for k in range(r + 1, n))) / m[r][r]
    return x


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1 / (1 + math.exp(-min(z, 50)))
    e = math.exp(max(z, -50))
    return e / (1 + e)


class Model:
    """Standardise -> L2 logistic regression fitted with Newton steps (IRLS)."""

    def __init__(self, features: list[str], mean: dict, std: dict, coef: dict, bias: float,
                 n: int = 0, positives: int = 0, auc: Optional[float] = None, recent_auc: Optional[float] = None,
                 trusted: bool = False, ts: float = 0.0):
        self.features, self.mean, self.std, self.coef, self.bias = features, mean, std, coef, bias
        self.n, self.positives, self.auc, self.recent_auc, self.trusted, self.ts = n, positives, auc, recent_auc, trusted, ts

    # -- fitting
    @staticmethod
    def fit(vectors: list[dict], labels: list[int], l2: float = L2, iters: int = 12) -> "Model":
        feats = [f for f in FEATURES if sum(1 for v in vectors if v.get(f) is not None) >= max(5, len(vectors) // 10)]
        mean, std = {}, {}
        for f in feats:
            xs = [v[f] for v in vectors if v.get(f) is not None]
            mu = sum(xs) / len(xs)
            sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / len(xs)) or 1.0
            mean[f], std[f] = mu, sd
        X = [[1.0] + [_z(v.get(f), mean[f], std[f]) for f in feats] for v in vectors]
        k = len(feats) + 1
        w = [0.0] * k
        pos = sum(labels)
        if 0 < pos < len(labels):
            w[0] = math.log(pos / (len(labels) - pos))
        for _ in range(iters):
            grad = [0.0] * k
            hess = [[0.0] * k for _ in range(k)]
            for xi, yi in zip(X, labels):
                p = _sigmoid(sum(a * b for a, b in zip(w, xi)))
                r = p - yi
                s = max(p * (1 - p), 1e-6)
                for a in range(k):
                    xa = xi[a]
                    if xa == 0.0:
                        continue
                    grad[a] += r * xa
                    sxa = s * xa
                    ha = hess[a]
                    for b in range(a, k):
                        ha[b] += sxa * xi[b]
            for a in range(k):
                for b in range(a):
                    hess[a][b] = hess[b][a]
                if a:
                    grad[a] += l2 * w[a]
                    hess[a][a] += l2
                else:
                    hess[a][a] += 1e-6
            step = _solve(hess, grad)
            w = [wi - si for wi, si in zip(w, step)]
            if max(abs(si) for si in step) < 1e-6:
                break
        return Model(feats, mean, std, {f: w[i + 1] for i, f in enumerate(feats)}, w[0],
                     n=len(labels), positives=pos)

    # -- use
    def predict(self, v: dict) -> float:
        z = self.bias + sum(c * _z(v.get(f), self.mean[f], self.std[f]) for f, c in self.coef.items())
        return _sigmoid(z)

    def reasons(self, v: dict, top: int = 3) -> tuple[list[str], list[str]]:
        """Which numbers pushed THIS coin's odds up / down the most."""
        contrib = [(c * _z(v.get(f), self.mean[f], self.std[f]), f) for f, c in self.coef.items()
                   if v.get(f) is not None]
        ups = [LABELS.get(f, f) for c, f in sorted(contrib, reverse=True) if c > 0.15][:top]
        downs = [LABELS.get(f, f) for c, f in sorted(contrib) if c < -0.15][:top]
        return ups, downs

    def importance(self, top: int = 6) -> list[tuple[str, float]]:
        return [(LABELS.get(f, f), c) for f, c in sorted(self.coef.items(), key=lambda kv: -abs(kv[1]))[:top]]

    def to_json(self) -> str:
        return json.dumps({"features": self.features, "mean": self.mean, "std": self.std, "coef": self.coef,
                           "bias": self.bias})

    @staticmethod
    def from_row(row: dict) -> "Model":
        d = json.loads(row["model"])
        return Model(d["features"], d["mean"], d["std"], d["coef"], d["bias"], n=row["n"],
                     positives=row["positives"], auc=row["auc"], recent_auc=row["recent_auc"],
                     trusted=bool(row["trusted"]), ts=row["ts"])


def _z(x, mu: float, sd: float) -> float:
    if x is None:
        return 0.0                      # unknown = average (no push either way)
    return max(-5.0, min(5.0, (x - mu) / sd))


def auc(labels: list[int], probs: list[float]) -> Optional[float]:
    """Probability that a random winner is ranked above a random loser (ties count half)."""
    pos = sum(labels)
    neg = len(labels) - pos
    if pos == 0 or neg == 0:
        return None
    order = sorted(range(len(probs)), key=lambda i: probs[i])
    ranks = [0.0] * len(probs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and probs[order[j + 1]] == probs[order[i]]:
            j += 1
        for t in range(i, j + 1):
            ranks[order[t]] = (i + j) / 2 + 1
        i = j + 1
    rank_sum = sum(r for r, y in zip(ranks, labels) if y)
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


# ----------------------------------------------------------------------------- training
def training_rows(db, days: float, now: float, win_x: float = WIN_X, loss_x: float = LOSS_X) -> list[dict]:
    """Evaluated shadows (minute paths) plus 24h decision tracking for coins without a shadow path."""
    rows, shadow_times = [], {}
    for r in db.fetchall("SELECT id, mint, ts, vector, path FROM shadow_calls WHERE evaluated=1 AND vector IS NOT NULL "
                         "AND vector != '{}' AND ts > ? ORDER BY ts", (now - days * 86400,)):
        try:
            v, path = json.loads(r["vector"]), json.loads(r["path"])
        except (TypeError, ValueError):
            continue
        y = outcome(path, win_x, loss_x)
        if y is None:
            continue
        rows.append({"id": r["id"], "ts": r["ts"], "v": v, "y": y, "src": "path"})
        shadow_times.setdefault(r["mint"], []).append(r["ts"])
    rows += _tracking_rows(db, days, now, win_x, shadow_times)
    rows.sort(key=lambda r: r["ts"])
    return rows[-MAX_ROWS:]


def store_tracking_vectors(db, limit: int = 2000) -> int:
    """Copy the brain's numbers onto tracked decisions while their research snapshot still exists
    ('{}' = nothing usable, e.g. a pre-migration coin). Returns how many got numbers."""
    try:
        todo = db.fetchall("SELECT d.snapshot_id, d.decided_at, r.report FROM decision_tracking d "
                           "LEFT JOIN research_snapshots r ON r.id = d.snapshot_id "
                           "WHERE d.brain_vector IS NULL ORDER BY d.decided_at DESC LIMIT ?", (limit,))
    except Exception:  # noqa: BLE001 - older databases without the table/column
        return 0
    from .analysis.verdict import assess
    filled = 0
    for r in todo:
        vec, keys = "{}", None
        try:
            rep = json.loads(r["report"]) if r["report"] else None
        except (TypeError, ValueError):
            rep = None
        if rep:
            try:
                keys = json.dumps(sorted({x["key"] for x in assess(rep)["red"]}))
            except Exception:  # noqa: BLE001 - a malformed old report just gets no flag record
                keys = None
        if rep and rep.get("lifecycle") != "PRE_MIGRATION_BONDING_CURVE":
            vec = json.dumps(vector(rep, r["decided_at"]))
            filled += 1
        db.execute("UPDATE decision_tracking SET brain_vector=?, verdict_keys=? WHERE snapshot_id=?",
                   (vec, keys, r["snapshot_id"]))
    return filled


def _tracking_rows(db, days: float, now: float, win_x: float, shadow_times: dict) -> list[dict]:
    try:
        found = db.fetchall(
            "SELECT d.snapshot_id, d.mint, d.decided_at, d.entry_price, d.max_price, d.done, d.brain_vector, "
            "r.report FROM decision_tracking d LEFT JOIN research_snapshots r ON r.id = d.snapshot_id "
            "WHERE d.samples > 0 AND d.entry_price > 0 AND d.max_price IS NOT NULL AND d.decided_at > ? "
            "ORDER BY d.decided_at", (now - days * 86400,))
    except Exception:  # noqa: BLE001 - older databases without the table
        return []
    out = []
    for r in found:
        peak = r["max_price"] / r["entry_price"]
        followed_h = (now - r["decided_at"]) / 3600
        if peak < win_x and not r["done"] and followed_h < TRACK_MIN_HOURS:
            continue                          # too early to call it a non-runner
        if any(abs(t - r["decided_at"]) < 1800 for t in shadow_times.get(r["mint"], ())):
            continue                          # the shadow path (with minute order) is the better example
        v = None
        if r["brain_vector"]:
            try:
                v = json.loads(r["brain_vector"]) or None       # '{}' = nothing usable
            except (TypeError, ValueError):
                v = None
        elif r["report"]:
            try:
                rep = json.loads(r["report"])
            except (TypeError, ValueError):
                rep = None
            if rep and rep.get("lifecycle") != "PRE_MIGRATION_BONDING_CURVE":
                v = vector(rep, r["decided_at"])
        if not v:
            continue
        out.append({"id": -int(r["snapshot_id"]), "ts": r["decided_at"], "v": v,
                    "y": 1 if peak >= win_x else 0, "src": "tracking"})
    return out


def train(db, s, now: Optional[float] = None) -> Optional[Model]:
    """Walk-forward train + validate, store the model and out-of-sample odds per coin."""
    now = now or time.time()
    win_x, loss_x = getattr(s, "brain_win_x", WIN_X), getattr(s, "brain_loss_x", LOSS_X)
    rows = training_rows(db, max(s.learning_days, s.brain_days), now, win_x, loss_x)
    pos = sum(r["y"] for r in rows)
    if len(rows) < 60 or pos < 8:
        log.info("brain: %d evaluated coins (%d winners) - need at least 60 and 8 winners to learn", len(rows), pos)
        return None
    # walk-forward: each fold is predicted by a model trained only on EARLIER coins
    size = len(rows) // FOLDS
    oof: dict[int, float] = {}
    recent_y, recent_p = [], []
    for k in range(1, FOLDS):
        train_rows = rows[:k * size]
        test_rows = rows[k * size:(k + 1) * size] if k < FOLDS - 1 else rows[k * size:]
        if sum(r["y"] for r in train_rows) < 5 or len(train_rows) < 40:
            continue
        m = Model.fit([r["v"] for r in train_rows], [r["y"] for r in train_rows])
        for r in test_rows:
            oof[r["id"]] = m.predict(r["v"])
        if k == FOLDS - 1:
            recent_y = [r["y"] for r in test_rows]
            recent_p = [oof[r["id"]] for r in test_rows]
    scored = [r for r in rows if r["id"] in oof]
    model_auc = auc([r["y"] for r in scored], [oof[r["id"]] for r in scored]) if scored else None
    recent = auc(recent_y, recent_p) if recent_y else None
    final = Model.fit([r["v"] for r in rows], [r["y"] for r in rows])
    final.auc, final.recent_auc, final.ts = model_auc, recent, now
    final.trusted = bool(
        len(rows) >= s.brain_min_samples and pos >= 15 and model_auc is not None and recent is not None
        and model_auc >= s.brain_min_auc and recent >= s.brain_min_auc - 0.03)
    db.execute("INSERT INTO brain_models(ts, n, positives, auc, recent_auc, trusted, model) VALUES (?,?,?,?,?,?,?)",
               (now, final.n, final.positives, model_auc, recent, 1 if final.trusted else 0, final.to_json()))
    db.execute("DELETE FROM brain_models WHERE id NOT IN (SELECT id FROM brain_models ORDER BY ts DESC LIMIT 20)")
    for rid, p in oof.items():
        if rid > 0:                       # tracking rows (negative ids) have no shadow to update
            db.execute("UPDATE shadow_calls SET brain_oof=? WHERE id=?", (p, rid))
    log.info("brain: trained on %d coins (%d winners) - walk-forward AUC %s, recent %s, %s",
             len(rows), pos, _fmt(model_auc), _fmt(recent), "TRUSTED" if final.trusted else "not trusted yet")
    return final


def latest(db) -> Optional[Model]:
    row = db.fetchone("SELECT * FROM brain_models ORDER BY ts DESC LIMIT 1")
    if not row:
        return None
    try:
        return Model.from_row(row)
    except (TypeError, ValueError, KeyError):
        return None


def _fmt(x) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def describe(m: Optional[Model]) -> str:
    if m is None:
        return "not trained yet (needs 60 evaluated coins incl. 8 winners)"
    state = "TRUSTED — it has a say in calls" if m.trusted else "learning — no say in calls yet"
    return (f"{state} · trained on {m.n} coins ({m.positives} were runners) · "
            f"walk-forward AUC {_fmt(m.auc)} (recent {_fmt(m.recent_auc)}; 0.50 = coin flip)")
