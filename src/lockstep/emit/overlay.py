"""Compile-time overlays: strategic-merge patches over generated artifacts.

Overlays are *inputs to* regeneration, which is what lets a customization survive it. Anchors are
addressed by the ids the emitter produces, and an anchor that matches nothing is a hard error —
never a silent no-op, because a mis-applied security patch is worse than a failed build.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..errors import OverlayAnchorNotFound, OverlayError
from ..util.hashing import sha_file, short

OVERLAY_DIR = "overlays/github"
SELECTOR = re.compile(r"^(?P<key>[\w-]+)(?:\[id=(?P<id>[^\]]+)\])?$")


@dataclass
class Overlay:
    target: str
    patches: list[dict[str, Any]] = field(default_factory=list)
    frontmatter: list[dict[str, Any]] = field(default_factory=list)
    prompt: list[dict[str, Any]] = field(default_factory=list)
    rel: str = ""
    sha: str = ""

    def stamp(self) -> str:
        return f"{self.rel}@{self.sha}"


def load_overlays(root: Path) -> list[Overlay]:
    directory = root / OVERLAY_DIR
    if not directory.is_dir():
        return []
    overlays: list[Overlay] = []
    for path in sorted(directory.glob("*.yml")) + sorted(directory.glob("*.yaml")):
        rel = str(path.relative_to(root))
        sha = short(sha_file(path))
        for index, document in enumerate(yaml.safe_load_all(path.read_text(encoding="utf-8"))):
            if not document:
                continue
            if "target" not in document:
                raise OverlayError(
                    "overlay document has no `target:`",
                    location=f"{rel} document {index + 1}",
                )
            overlays.append(
                Overlay(
                    target=str(document["target"]),
                    patches=list(document.get("patches", []) or []),
                    frontmatter=list(document.get("frontmatter", []) or []),
                    prompt=list(document.get("prompt", []) or []),
                    rel=rel,
                    sha=sha,
                )
            )
    return overlays


def _candidates(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [str(k) for k in node]
    if isinstance(node, list):
        return [str(item.get("id")) for item in node if isinstance(item, dict) and "id" in item]
    return []


def _not_found(component: str, node: Any, location: str) -> OverlayAnchorNotFound:
    options = _candidates(node)
    close = difflib.get_close_matches(component, options, n=1)
    hint = f"nearest: {close[0]}" if close else (f"available: {', '.join(options[:8])}" if options else None)
    return OverlayAnchorNotFound(
        f"no `{component}` in generated output",
        location=location,
        hint=hint,
    )


def resolve(root: Any, path: str, *, location: str) -> tuple[Any, Any]:
    """Walk a dotted path with `[id=…]` selectors. Returns (container, key-or-index)."""
    parts = path.split(".")
    node: Any = root
    container: Any = None
    key: Any = None

    for part in parts:
        match = SELECTOR.match(part)
        if not match:
            raise OverlayError(f"cannot parse path component {part!r}", location=location)
        name, selector = match.group("key"), match.group("id")

        if not isinstance(node, dict) or name not in node:
            raise _not_found(name, node, location)
        container, key, node = node, name, node[name]

        if selector is not None:
            if isinstance(node, dict):
                if selector not in node:
                    raise _not_found(selector, node, location)
                container, key, node = node, selector, node[selector]
            elif isinstance(node, list):
                index = next(
                    (
                        i
                        for i, item in enumerate(node)
                        if isinstance(item, dict) and item.get("id") == selector
                    ),
                    None,
                )
                if index is None:
                    raise _not_found(selector, node, location)
                container, key, node = node, index, node[index]
            else:
                raise _not_found(selector, node, location)

    return container, key


def deep_merge(target: Any, patch: Any) -> Any:
    """Maps merge key-wise; lists append (deduplicated); scalars replace."""
    if isinstance(target, dict) and isinstance(patch, dict):
        for key, value in patch.items():
            if value == "$patch: delete":
                target.pop(key, None)
            elif key in target:
                target[key] = deep_merge(target[key], value)
            else:
                target[key] = value
        return target
    if isinstance(target, list) and isinstance(patch, list):
        for item in patch:
            if item not in target:
                target.append(item)
        return target
    return patch


def apply_mapping_ops(data: dict[str, Any], ops: list[dict[str, Any]], *, location: str) -> int:
    """Apply merge / delete / insert-step operations to a generated mapping."""
    applied = 0
    for index, op in enumerate(ops):
        where = f"{location} hunk {index + 1}"
        kind = str(op.get("op", "merge"))
        path = str(op.get("at", ""))
        if not path:
            raise OverlayError("operation has no `at:` anchor", location=where)
        container, key = resolve(data, path, location=where)

        if kind == "merge":
            container[key] = deep_merge(container[key], op.get("value"))
        elif kind == "delete":
            if isinstance(container, list):
                container.pop(key)
            else:
                container.pop(key, None)
        elif kind == "insert-step":
            target_list = container[key]
            if not isinstance(target_list, list):
                raise OverlayError(f"`{path}` is not a list", location=where)
            position = _insert_index(target_list, op, where)
            target_list.insert(position, op.get("value"))
        else:
            raise OverlayError(f"unknown operation {kind!r}", location=where)
        applied += 1
    return applied


def _insert_index(items: list[Any], op: dict[str, Any], location: str) -> int:
    after, before = op.get("after"), op.get("before")
    anchor = after or before
    if anchor is None:
        return len(items)
    index = next(
        (i for i, item in enumerate(items) if isinstance(item, dict) and item.get("id") == anchor),
        None,
    )
    if index is None:
        raise _not_found(str(anchor), items, location)
    return index + 1 if after else index


def apply_prompt_ops(body: str, ops: list[dict[str, Any]], root: Path, *, location: str) -> tuple[str, int]:
    """Append or replace prose sections in a generated agent body."""
    applied = 0
    for index, op in enumerate(ops):
        where = f"{location} prompt hunk {index + 1}"
        kind = str(op.get("op", "append-section"))
        if kind != "append-section":
            raise OverlayError(f"unknown prompt operation {kind!r}", location=where)
        heading = str(op.get("heading", "")).strip()
        text = str(op.get("text", ""))
        if op.get("file"):
            fragment = root / str(op["file"])
            if not fragment.is_file():
                raise OverlayError(f"prompt fragment {op['file']!r} not found", location=where)
            text = fragment.read_text(encoding="utf-8")
        section = f"\n\n## {heading}\n\n{text.strip()}\n" if heading else f"\n\n{text.strip()}\n"
        body = body.rstrip("\n") + section
        applied += 1
    return body, applied
