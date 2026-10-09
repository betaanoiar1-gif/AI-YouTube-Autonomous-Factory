"""Secret redaction utilities.

Guarantees that secrets never reach logs, error messages, or artifacts:

* ``register_secret`` registers known secret values (e.g. the CleanAPIs API
  key) so every occurrence is masked wherever redaction is applied.
* Pattern-based redaction catches common credential shapes even when they were
  not explicitly registered (``cc_...`` API keys, ``Bearer ...`` tokens, and
  ``api_key = ...`` / ``token: ...`` style assignments).
* ``redact_mapping`` deep-copies mappings/lists and masks secret values.
"""

from __future__ import annotations

import re
from typing import Any

# CleanAPIs keys start with `cc_` (verified in CleanAPIs docs). Match the
# prefix plus a conservative tail so real keys are always masked.
_CC_KEY_PATTERN = re.compile(r"cc_[A-Za-z0-9_\-]{12,}")

# Authorization headers / bearer tokens.
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}")

# key = value / key: value assignments for common secret field names.
_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|apikey|auth[_-]?token|access[_-]?token|secret|secret[_-]?key|"
    r"password|passwd|credential[s]?|authorization)\b(\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)

REDACTED = "[REDACTED]"

_registered_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register a secret value so it is masked by all redaction helpers.

    Values shorter than 8 characters are ignored (too short to be a real
    credential, and masking them would mangle ordinary text).
    """
    if value and len(value) >= 8:
        _registered_secrets.add(value)


def unregister_all_secrets() -> None:
    """Clear registered secrets. Intended for tests."""
    _registered_secrets.clear()


def redact_text(text: str) -> str:
    """Return ``text`` with all known secrets and credential patterns masked."""
    if not text:
        return text
    redacted = text
    for secret in _registered_secrets:
        if secret in redacted:
            redacted = redacted.replace(secret, REDACTED)
    redacted = _CC_KEY_PATTERN.sub(REDACTED, redacted)
    redacted = _BEARER_PATTERN.sub(lambda m: m.group(1) + " " + REDACTED, redacted)
    redacted = _ASSIGNMENT_PATTERN.sub(lambda m: m.group(1) + m.group(2) + REDACTED, redacted)
    return redacted


def redact_mapping(value: Any) -> Any:
    """Deep-copy ``value`` (dict/list/scalars) with all secrets masked."""
    if isinstance(value, dict):
        return {str(k): redact_mapping(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_mapping(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_args(args: Any) -> Any:
    """Redact ``%``-formatting args while PRESERVING their structure.

    ``LogRecord.getMessage()`` requires ``record.args`` to be a tuple for
    multi-argument messages (``msg % args``) — converting it to a list breaks
    %-formatting downstream. Tuples stay tuples, dicts stay dicts.
    """
    if isinstance(args, tuple):
        return tuple(redact_args(item) for item in args)
    if isinstance(args, dict):
        return {str(k): redact_args(v) for k, v in args.items()}
    if isinstance(args, list):
        return [redact_args(item) for item in args]
    if isinstance(args, str):
        return redact_text(args)
    return args
