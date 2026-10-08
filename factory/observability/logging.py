"""Structured logging with mandatory secret redaction.

Design:

* JSON lines on stdout (one object per line) — easy to ship to any log
  aggregator later without changing application code.
* A redaction filter is attached to the handler AND the formatter re-redacts
  the final payload, so a secret can never reach a log sink even if it is
  embedded in an exception message or a context value.
* Context (``job_id``, ``provider``, ``artifact_id``, ...) is bound through
  contextvars and merged into every record.

Never log: API keys, authorization headers, secrets, or credentials.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from factory.security.redaction import redact_mapping, redact_text

# Immutable empty mapping as the default (mutable ContextVar defaults are shared).
_EMPTY_CONTEXT: MappingProxyType[str, Any] = MappingProxyType({})

_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "factory_log_context",
    # (MappingProxyType is immutable; a shared mutable default would be a bug.)
    default=_EMPTY_CONTEXT,  # type: ignore[arg-type]
)

_RESERVED_RECORD_KEYS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {
    "message",
    "asctime",
}


class SecretRedactionFilter(logging.Filter):
    """Redact secrets from the record message, args, and extra fields."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(str(record.msg))
        if record.args:
            record.args = redact_mapping(record.args)
        for key, value in list(vars(record).items()):
            if key in _RESERVED_RECORD_KEYS or key.startswith("_"):
                continue
            if isinstance(value, str):
                setattr(record, key, redact_text(value))
            elif isinstance(value, (dict, list, tuple)):
                setattr(record, key, redact_mapping(value))
        return True


class JSONFormatter(logging.Formatter):
    """Render log records as single-line JSON with redacted context."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": redact_text(record.getMessage()),
        }
        context = _context.get()
        if context:
            payload["context"] = redact_mapping(context)
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        for key, value in vars(record).items():
            if key in _RESERVED_RECORD_KEYS or key.startswith("_") or key in payload:
                continue
            if key == "context":
                continue
            payload[key] = redact_mapping(value)
        return json.dumps(payload, default=str, ensure_ascii=False)


def bind_context(**fields: Any) -> contextvars.Token[dict[str, Any]]:
    """Bind structured context fields to all subsequent log records."""
    current = _context.get()
    merged = {**current, **{k: v for k, v in fields.items() if v is not None}}
    return _context.set(merged)


def clear_context(token: contextvars.Token[dict[str, Any]] | None = None) -> None:
    """Reset bound context (to a previous token, or entirely)."""
    if token is not None:
        _context.reset(token)
    else:
        _context.set({})


def get_logger(name: str) -> logging.Logger:
    """Return a logger. Handlers are attached once by ``configure_logging``."""
    return logging.getLogger(name)


_configured = False


def configure_logging(level: str = "INFO", *, stream: Any = None) -> None:
    """Configure the root logger with the JSON handler + redaction filter.

    Idempotent: repeated calls only update the level.
    """
    global _configured
    root = logging.getLogger()
    log_level = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(log_level)
    if not _configured:
        handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
        handler.setFormatter(JSONFormatter())
        handler.addFilter(SecretRedactionFilter())
        root.addHandler(handler)
        _configured = True
    for existing_handler in root.handlers:
        existing_handler.setLevel(log_level)
