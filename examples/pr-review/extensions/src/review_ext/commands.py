"""Fetching what a reviewer needs to see.

One command. Most of its work is deciding what *not* to send: a pull request diff can be enormous,
and a reviewer agent given 40,000 lines will produce a review of the first 2,000 and a confident
silence about the rest. Truncating deliberately, and saying so in the output, is better than
truncating implicitly by running out of context.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import click

# Files whose diffs are large, mechanical, and never what a review is about.
GENERATED = (
    "package-lock.json",
    "yarn.lock",
    "uv.lock",
    "poetry.lock",
    "Cargo.lock",
    "go.sum",
    ".lock.yml",
)
MAX_PATCH = 24_000
MAX_TOTAL = 180_000


def _fail(message: str) -> None:
    click.echo(click.style(message, fg="red"), err=True)
    sys.exit(1)


def _emit_output(name: str, value: str) -> None:
    destination = os.environ.get("GITHUB_OUTPUT")
    if destination:
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    else:
        click.echo(f"{name}={value}")


def is_generated(path: str) -> bool:
    return path.endswith(GENERATED)


def assemble(files: list[dict[str, Any]], pull: dict[str, Any]) -> dict[str, Any]:
    """Reduce the API's file list to a diff a reviewer can hold in mind at once."""
    kept: list[dict[str, Any]] = []
    skipped: list[str] = []
    budget = MAX_TOTAL

    for entry in files:
        path = entry.get("filename", "")
        patch = entry.get("patch") or ""
        if is_generated(path):
            skipped.append(f"{path} (generated)")
            continue
        if not patch:
            # Binary files and pure renames carry no patch; naming them is enough.
            skipped.append(f"{path} ({entry.get('status', 'changed')}, no textual diff)")
            continue
        if len(patch) > MAX_PATCH:
            patch = patch[:MAX_PATCH] + "\n… patch truncated …"
        if budget - len(patch) < 0:
            skipped.append(f"{path} (diff budget exhausted)")
            continue
        budget -= len(patch)
        kept.append(
            {
                "path": path,
                "status": entry.get("status", "modified"),
                "additions": entry.get("additions", 0),
                "deletions": entry.get("deletions", 0),
                "patch": patch,
            }
        )

    return {
        "number": pull.get("number"),
        "title": pull.get("title", ""),
        "body": (pull.get("body") or "")[:8000],
        "base": ((pull.get("base") or {}).get("ref", "")),
        "head_sha": ((pull.get("head") or {}).get("sha", "")),
        "files": kept,
        # Named, not silently dropped: a reviewer must know what it did not see.
        "not_reviewed": skipped,
        "truncated": bool(skipped),
    }


@click.command(name="pr-diff")
@click.option("--pr", required=True, help="Pull request number.")
@click.option("--repo", envvar="GITHUB_REPOSITORY", default="")
@click.option("--output", required=True, type=click.Path(path_type=Path))
@click.option("--from-dir", type=click.Path(path_type=Path), help="Read fixtures instead of the API.")
def pr_diff(pr: str, repo: str, output: Path, from_dir: Path | None) -> None:
    """Fetch a pull request's diff and metadata, reduced to what a review can act on."""
    if from_dir:
        files = json.loads((from_dir / "files.json").read_text(encoding="utf-8"))
        pull = json.loads((from_dir / "pull.json").read_text(encoding="utf-8"))
    else:
        if not repo:
            _fail("--repo is required (or set GITHUB_REPOSITORY)")

        def api(path: str, paginate: bool = False) -> Any:
            args = ["gh", "api", path] + (["--paginate"] if paginate else [])
            result = subprocess.run(args, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                _fail(f"gh api {path} failed: {result.stderr.strip()[:200]}")
            return json.loads(result.stdout or "{}")

        pull = api(f"repos/{repo}/pulls/{pr}")
        files = api(f"repos/{repo}/pulls/{pr}/files", paginate=True)

    diff = assemble(files, pull)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(diff, indent=2) + "\n", encoding="utf-8")

    _emit_output("files", str(len(diff["files"])))
    _emit_output("head_sha", diff["head_sha"])
    _emit_output("truncated", "true" if diff["truncated"] else "false")
    click.echo(f"{len(diff['files'])} file(s) for review -> {output}")
    if diff["not_reviewed"]:
        click.echo(f"not reviewed: {', '.join(diff['not_reviewed'][:5])}", err=True)
