#!/usr/bin/env python3
"""Assemble the fixes that survived review into what the proposal step will carry.

Deterministic on purpose. The reviewer agent decided what is acceptable; deciding what that decision
*means* — which files move, under what names, with what provenance — is bookkeeping, and bookkeeping
that varies between runs is a bug.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def approved(review: dict) -> list[dict]:
    """Fixes the reviewer approved. Anything not explicitly approved is not approved."""
    return [entry for entry in review.get("fixes", []) if entry.get("verdict") == "approve"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review", required=True)
    parser.add_argument("--patches", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    review_path = Path(args.review)
    if not review_path.is_file():
        print("no review found; nothing to assemble")
        return

    review = json.loads(review_path.read_text(encoding="utf-8"))
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    carried = []
    for entry in approved(review):
        key = entry["key"]
        patch = Path(args.patches) / f"{key}.patch"
        if not patch.is_file():
            print(f"{key}: approved but no patch on disk; skipping")
            continue
        shutil.copy(patch, output / f"{key}.patch")
        (output / f"{key}.md").write_text(
            f"# {key}\n\n{entry.get('summary', '')}\n\n## Why this is correct\n\n"
            f"{entry.get('rationale', '')}\n\n## What it could break\n\n"
            f"{entry.get('risk', 'Not stated.')}\n",
            encoding="utf-8",
        )
        carried.append(key)

    (output / "MANIFEST.json").write_text(
        json.dumps({"approved": carried, "reviewed": len(review.get("fixes", []))}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"assembled {len(carried)} approved fix(es) of {len(review.get('fixes', []))} reviewed")


if __name__ == "__main__":
    main()
