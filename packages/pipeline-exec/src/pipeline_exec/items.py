"""Loading, keying, covering and slicing the items a `foreach` step fans out over.

The compiler cannot know how many items there will be — that is a runtime fact — so the decision
between one matrix leg per item and a fixed number of shard legs is made here, not at compile time.
The emitted workflow is identical either way; only this output differs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ExecError, TooManyItems

# GitHub refuses a matrix larger than this, and does so after the run has started.
MATRIX_CAP = 256
SAFE_KEY = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Item:
    key: str
    value: dict[str, Any]


def load_items(path: Path, key_field: str) -> list[Item]:
    """Read a JSON array into keyed items.

    Accepts an array of objects (the normal case), an array of scalars, or an object whose single
    array value holds the items — pipelines produce all three and none is worth failing over.
    """
    if not path.is_file():
        raise ExecError(f"input {path} does not exist")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExecError(f"input {path} is not valid JSON: {exc}") from exc

    if isinstance(data, dict):
        arrays = [value for value in data.values() if isinstance(value, list)]
        if len(arrays) != 1:
            raise ExecError(
                f"input {path} is an object with {len(arrays)} array values; "
                "expected an array, or an object holding exactly one"
            )
        data = arrays[0]
    if not isinstance(data, list):
        raise ExecError(f"input {path} must contain a JSON array")

    items: list[Item] = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(data):
        value = raw if isinstance(raw, dict) else {key_field: raw}
        key = str(value.get(key_field, index))
        key = SAFE_KEY.sub("-", key).strip("-") or str(index)
        # Keys become file names and matrix labels, so collisions must be resolved, not tolerated.
        if key in seen:
            seen[key] += 1
            key = f"{key}-{seen[key]}"
        else:
            seen[key] = 1
        items.append(Item(key=key, value={**value, key_field: value.get(key_field, key)}))
    return items


def covered(items: list[Item], output_dir: Path | None, pattern: str) -> set[str]:
    """Keys whose output already exists — git and the restored workspace are both caches."""
    if output_dir is None:
        return set()
    return {item.key for item in items if (output_dir / pattern.format(key=item.key)).exists()}


def enforce_cap(count: int, cap: int) -> None:
    if count > cap:
        raise TooManyItems(
            f"{count} items exceeds the matrix cap of {cap}; "
            "shard the step, narrow its input, or split the command"
        )


def shard_of(items: list[Item], index: int, total: int) -> list[Item]:
    """Round-robin slicing, so a shard's work is spread rather than clustered by input order."""
    if total <= 0:
        raise ExecError("shard count must be positive")
    return [item for position, item in enumerate(items) if position % total == index]


def as_matrix(items: list[Item]) -> list[dict[str, Any]]:
    return [item.value for item in items]


def as_shards(count: int) -> list[dict[str, Any]]:
    return [{"shard": index, "shards": count, "key": f"shard-{index}"} for index in range(count)]
