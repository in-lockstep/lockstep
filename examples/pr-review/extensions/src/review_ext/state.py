"""Deciding which aspects still need reviewing, and what was said last time.

Two things make a review bot tolerable rather than irritating, and both live here.

It must not re-review a pull request that has not changed. A second review saying the same thing is
worse than no review: it buries the human conversation and teaches people to mute the bot.

And when the pull request *has* changed, it must revise what it already said rather than post again
beside it. A reviewer who addressed a finding wants to see it resolved, not repeated.

Both need the same fact — which commit each aspect was last reviewed against — and the only durable
place to keep it is the review itself. So the bot's reviews carry a marker, and this reads it back.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import click

MARKER = "<!-- lockstep:review aspect={aspect} sha={sha} -->"
MARKER_PATTERN = re.compile(
    r"<!--\s*lockstep:review\s+aspect=(?P<aspect>[\w-]+)\s+sha=(?P<sha>[0-9a-f]+)\s*-->"
)


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


def marker_for(aspect: str, sha: str) -> str:
    return MARKER.format(aspect=aspect, sha=sha)


def previous_reviews(reviews: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The bot's own most recent review per aspect, found by its marker.

    Latest wins: a review revised several times leaves several entries, and only the last one
    describes the current state.
    """
    found: dict[str, dict[str, Any]] = {}
    for review in reviews:
        match = MARKER_PATTERN.search(review.get("body") or "")
        if not match:
            continue
        found[match.group("aspect")] = {
            "id": review.get("id"),
            "sha": match.group("sha"),
            "body": review.get("body") or "",
            "submitted_at": review.get("submitted_at", ""),
        }
    return found


def plan(
    aspects: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    head: str,
    commits: list[dict[str, Any]],
    *,
    force: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Split the requested aspects into work to do and work already done.

    Returns (pending, skipped). An aspect already reviewed at this exact commit is skipped: nothing
    it could say would be new.
    """
    previous = previous_reviews(reviews)
    pending: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for aspect in aspects:
        key = aspect["key"]
        seen = previous.get(key)

        if seen and seen["sha"] == head and not force:
            skipped.append(
                {"key": key, "reason": f"already reviewed at {head[:8]}; the pull request has not moved"}
            )
            continue

        item = dict(aspect)
        if seen:
            item["previous_review_id"] = seen["id"]
            item["previous_review"] = seen["body"]
            item["previously_reviewed_sha"] = seen["sha"]
            # What moved since. This is what a revision is actually about — the agent is being asked
            # what these commits changed about its earlier conclusion.
            item["new_commits"] = commits_since(commits, seen["sha"])
            item["revision"] = True
        else:
            item["revision"] = False
        pending.append(item)

    return pending, skipped


def commits_since(commits: list[dict[str, Any]], sha: str) -> list[dict[str, str]]:
    """Commits after `sha`, oldest first. Everything, if that commit is no longer in the history."""
    messages = [
        {
            "sha": (commit.get("sha") or "")[:8],
            "message": ((commit.get("commit") or {}).get("message") or "").splitlines()[0][:200],
        }
        for commit in commits
    ]
    for index, commit in enumerate(commits):
        if (commit.get("sha") or "").startswith(sha) or sha.startswith(commit.get("sha") or "x"):
            return messages[index + 1 :]
    # A force-push can erase the commit a review was made against. Reviewing everything again is the
    # safe answer; pretending nothing changed is not.
    return messages


def _gh(path: str, paginate: bool = False) -> Any:
    args = ["gh", "api", path] + (["--paginate"] if paginate else [])
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        _fail(f"gh api {path} failed: {result.stderr.strip()[:200]}")
    return json.loads(result.stdout or "[]")


@click.command(name="review-state")
@click.option("--pr", required=True, help="Pull request number.")
@click.option("--repo", envvar="GITHUB_REPOSITORY", default="")
@click.option("--aspects", "aspects_file", required=True, type=click.Path(path_type=Path))
@click.option("--head", default="", help="The commit being reviewed. Read from the API if omitted.")
@click.option("--output", required=True, type=click.Path(path_type=Path))
@click.option("--force", is_flag=True, help="Review again even if nothing has changed.")
@click.option("--from-dir", type=click.Path(path_type=Path), help="Read fixtures instead of the API.")
def review_state(
    pr: str,
    repo: str,
    aspects_file: Path,
    head: str,
    output: Path,
    force: bool,
    from_dir: Path | None,
) -> None:
    """Narrow the requested aspects to those the pull request has actually moved past."""
    aspects = json.loads(aspects_file.read_text(encoding="utf-8"))

    if from_dir:
        def load(name: str) -> Any:
            path = from_dir / f"{name}.json"
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []

        reviews, commits = load("reviews"), load("commits")
        head = head or "head000"
    else:
        if not repo:
            _fail("--repo is required (or set GITHUB_REPOSITORY)")
        reviews = _gh(f"repos/{repo}/pulls/{pr}/reviews", paginate=True)
        commits = _gh(f"repos/{repo}/pulls/{pr}/commits", paginate=True)
        head = head or (commits[-1]["sha"] if commits else "")

    pending, skipped = plan(aspects, reviews, head, commits, force=force)
    for item in pending:
        item["head_sha"] = head

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(pending, indent=2) + "\n", encoding="utf-8")

    _emit_output("pending", str(len(pending)))
    _emit_output("skipped", str(len(skipped)))
    click.echo(f"{len(pending)} aspect(s) to review, {len(skipped)} unchanged")
    for entry in skipped:
        click.echo(f"  skipping {entry['key']}: {entry['reason']}", err=True)
