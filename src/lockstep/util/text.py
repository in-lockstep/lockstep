"""Small text helpers shared by the parser and the emitters."""

from __future__ import annotations

import re

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    """Lowercase kebab slug, safe as a GitHub Actions job id."""
    out = _SLUG_STRIP.sub("-", text.lower()).strip("-")
    return out or "step"


def uniquify(candidate: str, taken: set[str]) -> str:
    """Return candidate, or candidate-2, candidate-3 ... until unused. Mutates `taken`."""
    name = candidate
    n = 2
    while name in taken:
        name = f"{candidate}-{n}"
        n += 1
    taken.add(name)
    return name


def env_key(key: str) -> str:
    """Profile key -> environment variable suffix (api_url -> API_URL)."""
    return _SLUG_STRIP.sub("_", key.lower()).strip("_").upper()
