# Extracted from pipeline-framework src/utils/sanitize.py on 2026-08-23.
# Behaviour is preserved verbatim; only imports and configuration plumbing were adapted.
from __future__ import annotations

import os
import re

_PATTERNS: list[re.Pattern[str]] = []


def init_sanitizer() -> None:
    """Initialize the sanitizer with sensitive values from environment."""
    global _PATTERNS
    _PATTERNS = []

    # Suffixes, not names. The scan below matches any variable *ending* with one of these, so
    # `AO_PASSWORD` and `JIRA_API_TOKEN` were already covered by `PASSWORD` and `TOKEN` — naming
    # them bought nothing and read as a list of one organisation's systems.
    sensitive_keys = [
        "PASSWORD",
        "TOKEN",
        "SECRET",
        "API_KEY",
        "CREDENTIALS",
    ]

    for key in sensitive_keys:
        value = os.getenv(key, "")
        if not value:
            # Check all env vars ending with this suffix
            for env_key, env_val in os.environ.items():
                if env_key.endswith(key) and env_val and len(env_val) > 5:
                    _PATTERNS.append(re.compile(re.escape(env_val)))
        elif len(value) > 5:
            _PATTERNS.append(re.compile(re.escape(value)))
        elif value:
            _PATTERNS.append(re.compile(rf"\b{re.escape(value)}\b"))


def sanitize(text: str) -> str:
    """Replace sensitive values with ********."""
    result = text
    for pattern in _PATTERNS:
        result = pattern.sub("********", result)

    # Generic patterns
    result = re.sub(r"(Bearer\s+)\S+", r"\1********", result)
    result = re.sub(r"(token[\"']?\s*[:=]\s*[\"']?)\S+", r"\1********", result, flags=re.IGNORECASE)

    return result
