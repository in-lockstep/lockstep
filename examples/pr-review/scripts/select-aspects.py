#!/usr/bin/env python3
"""Turn a request like `/review security intent` into the work list the pipeline fans out over.

Every aspect is a file in `aspects/`. Adding a review lens means adding a markdown file — no code
changes, no pipeline changes. This script reads that directory, validates what was asked for against
it, and emits one item per aspect carrying the brief the reviewing agent will follow.

Validation happens here rather than in a prompt because an unknown aspect must fail loudly. A model
asked to perform a "banana review" will produce one, and it will look plausible.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n", re.DOTALL)


def load_aspects(directory: Path) -> dict[str, dict[str, Any]]:
    """Read every aspect definition. The file name is the name the command uses."""
    aspects: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        match = FRONTMATTER.match(raw)
        meta: dict[str, str] = {}
        if match:
            for line in match.group(1).splitlines():
                key, _, value = line.partition(":")
                if value.strip():
                    meta[key.strip()] = value.strip()
        name = meta.get("name") or path.stem
        aspects[name] = {
            "key": name,
            "title": meta.get("title", name.title()),
            "summary": meta.get("summary", ""),
            "brief": raw[match.end() :].strip() if match else raw.strip(),
        }
    return aspects


def select(requested: list[str], aspects: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve a request into work items, preserving the order asked for.

    An empty request reviews everything: `/review` with no arguments is a reasonable thing to type,
    and refusing it would be pedantry.
    """
    if not requested:
        return [aspects[name] for name in sorted(aspects)]

    unknown = [name for name in requested if name not in aspects]
    if unknown:
        raise KeyError(
            f"unknown review aspect(s): {', '.join(unknown)}. "
            f"available: {', '.join(sorted(aspects))}"
        )
    # Deduplicated, because `/review security security` asks for one review, not two.
    seen: set[str] = set()
    ordered = [name for name in requested if not (name in seen or seen.add(name))]
    return [aspects[name] for name in ordered]


def parse_request(raw: str) -> list[str]:
    """Accept the JSON array the gate emits, or a plain space- or comma-separated list."""
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            return [str(item).strip().lower() for item in json.loads(text) if str(item).strip()]
        except json.JSONDecodeError:
            pass
    return [word.strip().lower() for word in re.split(r"[,\s]+", text) if word.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requested", default="", help="What the comment asked for.")
    parser.add_argument("--aspects-dir", default="aspects")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    aspects = load_aspects(Path(args.aspects_dir))
    if not aspects:
        print(f"no aspects defined in {args.aspects_dir}", file=sys.stderr)
        sys.exit(1)

    try:
        selected = select(parse_request(args.requested), aspects)
    except KeyError as error:
        print(str(error).strip('"'), file=sys.stderr)
        sys.exit(1)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(selected, indent=2) + "\n", encoding="utf-8")
    print(f"reviewing {len(selected)} aspect(s): {', '.join(a['key'] for a in selected)}")


if __name__ == "__main__":
    main()
