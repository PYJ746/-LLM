"""JSON logging with recursive redaction of personally identifiable fields.

Every tool call emits exactly one log record shaped as:

    {"timestamp": ..., "tool_name": ..., "params": {...},
     "status": "ok"|"error", "duration_ms": ...}
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any

REDACTED = "***REDACTED***"

# Matched against the *normalized* key (lowercased, non-alphanumerics removed),
# so all of `ownerPhone`, `owner_phone`, `OWNER_PHONE`, `owner-phone` and
# `chipNo` / `chip_no` collapse onto the same entries.
SENSITIVE_KEYS = frozenset({"ownerphone", "owneraddr", "chipno"})

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_key(key: str) -> str:
    """Fold a field name to its comparison form."""
    return _NON_ALNUM.sub("", key.lower())


def is_sensitive_key(key: str) -> bool:
    return normalize_key(key) in SENSITIVE_KEYS


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively replace sensitive values anywhere in a JSON-ish structure.

    Recursion is depth-limited so a hostile or cyclic structure can never turn a
    log statement into an infinite loop.
    """
    if _depth > 32:
        return "<max-depth>"
    if isinstance(value, dict):
        return {
            key: (REDACTED if is_sensitive_key(str(key)) else redact(item, _depth=_depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth=_depth + 1) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Structured fields attached via `logger.info(..., extra={"event": {...}})`.
        event = getattr(record, "event", None)
        if isinstance(event, dict):
            payload.update(redact(event))

        if record.exc_info and not event:
            # Only surface exception *type* — never a traceback or its message,
            # which may embed upstream URLs or payload fragments.
            payload["exception_type"] = getattr(record.exc_info[0], "__name__", "Exception")

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger (idempotent)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # The MCP SDK is chatty at INFO on every request.
    logging.getLogger("mcp").setLevel(logging.WARNING)

    # `httpx` logs every request line at INFO, and the URL carries the query
    # string — including `ownerPhone` and any future personal filter. Our own
    # redaction never sees those records, so the only safe answer is to keep
    # them out of the log entirely. `httpcore` does the same one layer down.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_tool_call(
    logger: logging.Logger,
    *,
    tool_name: str,
    params: dict[str, Any],
    status: str,
    duration_ms: float,
) -> None:
    """Emit the canonical per-call record (params are redacted by the formatter)."""
    logger.info(
        "tool_call",
        extra={
            "event": {
                "tool_name": tool_name,
                "params": params,
                "status": status,
                "duration_ms": round(duration_ms, 3),
            }
        },
    )
