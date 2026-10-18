"""Structured logging with secret redaction."""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Iterable

_SECRETS: list[str] = []
_KEY_PATTERNS = [re.compile(r"(api-key=)[^&\s\"']+", re.I), re.compile(r"(Bot\s+)[A-Za-z0-9._-]{20,}")]


def redact(text: str) -> str:
    for s in _SECRETS:
        if s and len(s) >= 6:
            text = text.replace(s, "***REDACTED***")
    for p in _KEY_PATTERNS:
        text = p.sub(r"\1***REDACTED***", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover
            return True
        record.msg = redact(msg)
        record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "ctx", None)
        if isinstance(extra, dict):
            out.update(extra)
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(out, default=str)


def setup_logging(level: str = "INFO", json_logs: bool = True, secrets: Iterable[str] = ()) -> None:
    _SECRETS.clear()
    _SECRETS.extend(s for s in secrets if s)
    root = logging.getLogger()
    root.handlers.clear()
    h = logging.StreamHandler(sys.stdout)
    h.addFilter(RedactingFilter())
    h.setFormatter(JsonFormatter() if json_logs else logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(h)
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "discord", "websockets"):
        logging.getLogger(noisy).setLevel("WARNING")
