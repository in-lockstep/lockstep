"""Content hygiene for agent output.

Agent output crosses a trust boundary: it was produced from untrusted input (issue bodies, test
logs) and is about to be consumed by a downstream step, a file name, or a rendered report. Structure
alone is not enough — an enormous field or embedded markup is a payload even when the JSON is valid.
"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_MAX_FIELD = 20_000
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MARKUP = re.compile(r"<[^>]{1,200}>")


def sanitize(value: Any, *, max_field: int = DEFAULT_MAX_FIELD, strip_markup: bool = True) -> Any:
    """Recursively cap string lengths and remove control characters and markup."""
    if isinstance(value, str):
        text = CONTROL.sub("", value)
        if strip_markup:
            text = MARKUP.sub("", text)
        if len(text) > max_field:
            text = text[:max_field] + "…[truncated]"
        return text
    if isinstance(value, list):
        return [sanitize(entry, max_field=max_field, strip_markup=strip_markup) for entry in value]
    if isinstance(value, dict):
        return {
            str(key): sanitize(entry, max_field=max_field, strip_markup=strip_markup)
            for key, entry in value.items()
        }
    return value
