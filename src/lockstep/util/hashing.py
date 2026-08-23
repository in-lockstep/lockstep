"""Content hashing. Every generated file records the hashes of everything it was derived from."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SHORT = 8


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def short(digest: str) -> str:
    return digest[:SHORT]


def sha_text(text: str) -> str:
    return sha_bytes(text.encode("utf-8"))


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def sha_obj(obj: Any) -> str:
    """Stable hash of a JSON-serializable object (sorted keys, no whitespace drift)."""
    return sha_text(json.dumps(obj, sort_keys=True, separators=(",", ":")))
