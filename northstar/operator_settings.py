"""Settings changed from the dashboard (v4.1.0).

Operator: "I must be able to change every setting inside the dashboard."

  * Every setting except keys, tokens, passwords and file/server locations can be changed on the
    dashboard's Settings tab. The value is validated exactly as a .env value would be.
  * Changes apply to the running bot at once and are stored in `operator_settings`, so they survive
    restarts. A dashboard value beats .env, the default, and anything the learners tuned (the learners
    leave a setting you set alone).
  * Most settings take effect immediately; loop timings, on/off switches for whole features and
    channel ids read at start apply after the next restart (the panel says so).
  * "Reset" removes your value: the setting goes back to .env / default.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional, get_args

log = logging.getLogger("northstar.operator_settings")

SCHEMA = """
CREATE TABLE IF NOT EXISTS operator_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

# never changeable from the browser: secrets, and where the bot keeps its data / listens
BLOCKED = {"discord_token", "helius_api_key", "pumpportal_api_key", "coingecko_api_key", "dashboard_password",
           "ai_key", "anthropic_api_key", "gemini_api_key", "data_dir", "db_path", "dashboard_host",
           "dashboard_port", "dashboard_enabled", "tracked_wallets_file"}
# read once at start: changes apply after a restart
RESTART_HINTS = ("_seconds", "_interval", "_enabled", "_minutes", "rpc_url", "_rpm", "concurrency", "max_queue",
                 "channel_id", "enable_")


def init_schema(db) -> None:
    with db._lock:
        db._conn.executescript(SCHEMA)


def editable(s, key: str) -> bool:
    fields = type(s).model_fields
    if key not in fields or key in BLOCKED or "SecretStr" in str(fields[key].annotation):
        return False
    low = key.lower()
    return not ("secret" in low or low.endswith("_api_key") or low.endswith("_token") or "password" in low)


def needs_restart(key: str) -> bool:
    return any(h in key for h in RESTART_HINTS)


def _wants_text(s, key: str) -> bool:
    """True for a plain text setting (str, or Optional[str]): its value is never read as JSON."""
    ann = type(s).model_fields[key].annotation
    if ann is str:
        return True
    args = set(get_args(ann))
    return bool(args) and str in args and args <= {str, type(None)}


def validate(s, key: str, raw: Any) -> Any:
    """The value as the bot would hold it, validated like a .env value. Raises ValueError."""
    if not editable(s, key):
        raise ValueError(f"{key} cannot be changed from the dashboard")
    if isinstance(raw, str):
        txt = raw.strip()
        if _wants_text(s, key):
            # v4.4.1: a text setting keeps what was typed. "1563216711522721856" (a channel id list) must
            # not become a number: the field only accepts text and the value was refused
            raw = txt
        elif txt.lower() in ("", "none", "null"):
            raw = None
        else:
            try:
                raw = json.loads(txt)          # numbers, true/false, lists, objects
            except ValueError:
                raw = txt                      # plain text
    try:
        fresh = type(s)(_env_file=None, **{key: raw})
    except Exception as e:  # noqa: BLE001 - pydantic's ValidationError, said plainly
        msg = str(e).split("\n")
        raise ValueError(f"{key}: " + (msg[2].strip() if len(msg) > 2 else msg[0])[:200]) from None
    return getattr(fresh, key)


def _json(v: Any) -> str:
    if isinstance(v, (set, tuple)):
        v = list(v)
    return json.dumps(v, default=str)


def keys(db) -> set[str]:
    try:
        init_schema(db)
        return {r["key"] for r in db.fetchall("SELECT key FROM operator_settings")}
    except Exception:  # noqa: BLE001
        return set()


def sync_globals(s) -> None:
    """Module-level values some settings drive (v4.3.0: the dead-coin market cap)."""
    from . import brain as B
    try:
        B.DEAD_MCAP = float(getattr(s, "dead_mcap_usd", B.DEAD_MCAP) or 0)
    except (TypeError, ValueError):
        pass


def apply(db, s) -> dict:
    """At start: put back every dashboard value. Returns {key: value}. Invalid rows are skipped."""
    init_schema(db)
    out = {}
    for r in db.fetchall("SELECT key, value FROM operator_settings"):
        try:
            v = validate(s, r["key"], r["value"])
        except ValueError as e:
            log.warning("dashboard setting ignored: %s", e)
            continue
        setattr(s, r["key"], v)
        out[r["key"]] = v
    sync_globals(s)
    return out


def set_value(db, s, key: str, raw: Any, now: Optional[float] = None) -> Any:
    v = validate(s, key, raw)
    init_schema(db)
    db.execute("INSERT INTO operator_settings(key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE "
               "SET value = excluded.value, updated_at = excluded.updated_at", (key, _json(v), now or time.time()))
    setattr(s, key, v)
    sync_globals(s)
    log.info("dashboard: %s set to %r", key, v)
    return v


def reset(db, s, key: str) -> Any:
    """Remove the dashboard value; the setting goes back to .env / default."""
    if key not in type(s).model_fields:
        raise ValueError(f"unknown setting {key}")
    init_schema(db)
    db.execute("DELETE FROM operator_settings WHERE key = ?", (key,))
    v = getattr(type(s)(), key)               # .env, else default
    setattr(s, key, v)
    sync_globals(s)
    log.info("dashboard: %s reset to %r", key, v)
    return v
