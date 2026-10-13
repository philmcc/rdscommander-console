"""Hide secret-looking values before anything is stored."""

from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|secret|token|credential|private_key|session_key)", re.IGNORECASE
)
REDACTED = "[REDACTED]"


def redact_mapping(values: Any) -> Any:
    """Return a copy of `values` with secret-like keys replaced by `[REDACTED]`, recursively."""
    if isinstance(values, dict):
        return {
            k: (REDACTED if SENSITIVE_KEY_PATTERN.search(str(k)) else redact_mapping(v))
            for k, v in values.items()
        }
    if isinstance(values, list):
        return [redact_mapping(item) for item in values]
    return values
