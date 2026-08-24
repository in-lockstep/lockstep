"""Reviewing a pull request: what to show, what is still due, and how to say it.

Promoted out of `examples/pr-review`, where it was an extension package a shipped pipeline could not
depend on. The logic moved rather than being rewritten — it carries behaviour earned against a real
review bot, and three parts of it are the difference between a bot people keep and one they mute.

**What not to send.** A pull request diff can be enormous, and a reviewer handed 40,000 lines
produces a review of the first 2,000 and a confident silence about the rest. Truncating deliberately
and *naming what was dropped* is better than truncating implicitly by running out of context.

**Not reviewing the same commit twice.** A second review saying the same thing buries the human
conversation. Which commit each aspect was last reviewed against has only one durable home — the
review itself — so the reviews carry a marker and this reads it back.

**Revising rather than repeating.** When the branch does move, a reviewer who addressed a finding
wants to see it resolved, not restated underneath.
"""

from __future__ import annotations

import json
import re
from typing import Any


class ReviewError(ValueError):
    """A request this pipeline cannot serve, refused before an agent is asked to serve it."""


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


MARKER = "<!-- lockstep:review aspect={aspect} sha={sha} -->"
MARKER_PATTERN = re.compile(
    r"<!--\s*lockstep:review\s+aspect=(?P<aspect>[\w-]+)\s+sha=(?P<sha>[0-9a-f]+)\s*-->"
)


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


def requested_aspects(raw: str, available: list[str]) -> list[str]:
    """Resolve what the comment asked for against what this pipeline can actually review.

    An empty request reviews everything: `/review` with no arguments is a reasonable thing to type,
    and refusing it would be pedantry.
    """
    text = (raw or "").strip()
    parsed: list[str] | None = None
    if text.startswith("["):
        try:
            parsed = [str(item).strip().lower() for item in json.loads(text) if str(item).strip()]
        except json.JSONDecodeError:
            # Not JSON after all — fall through to the word split below, which is what a human
            # typing `/review security intent` produces.
            parsed = None

    # `parsed is None` and `parsed == []` are different answers, and conflating them is what broke
    # `/review` with no arguments. The gate hands over `[]` for "nothing was requested", which
    # parses successfully to an empty list — and the old code, seeing no words and a non-empty
    # string, re-split the literal `[]` into a request for an aspect called "[]".
    if parsed is not None:
        words = parsed
    elif text:
        words = [word.strip().lower() for word in re.split(r"[,\s]+", text) if word.strip()]
    else:
        words = []

    if not words:
        return sorted(available)

    unknown = [word for word in words if word not in available]
    if unknown:
        # Refused here rather than in a prompt. A model asked to perform a banana review will
        # produce one, and it will look plausible.
        raise ReviewError(
            f"unknown review aspect(s): {', '.join(unknown)}. available: {', '.join(sorted(available))}"
        )
    # Deduplicated, because `/review security security` asks for one review, not two. Order is
    # preserved: the reviews come back in the order somebody asked for them.
    seen: set[str] = set()
    unique: list[str] = []
    for word in words:
        if word not in seen:
            seen.add(word)
            unique.append(word)
    return unique


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
        # Where the code is. The agent job has the pull request checked out at the workspace root,
        # so in a real run this is always ".". It is written down rather than assumed because an
        # eval case hands the same agent a fixture tree somewhere else, and an agent that read the
        # working directory would be reviewing whatever repository the suite happened to run in.
        item["repo"] = "."
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


# --- publishing -----------------------------------------------------------------------------------
#
# This was a shell script driving `jq` and `gh`. It is code here for one reason: a shipped pipeline
# carries no scripts, because a script in the library is untested code arriving in every repository
# that adopts it. Rendering and posting are separated so the rendering half can be tested without a
# network, which is the half where the mistakes are.


def render_review(aspect: str, result: dict[str, Any], *, sha: str) -> str:
    """One aspect's findings, as the body of a pull request review.

    The marker goes first and carries the commit. It is how the next run finds this review to revise
    rather than posting a second one beside it, and it is invisible in the rendered comment.
    """
    lines = [
        marker_for(aspect, sha),
        f"## {result.get('title') or aspect.replace('-', ' ').title()} review",
        "",
    ]
    lines.append(str(result.get("summary") or "No findings."))

    findings = [f for f in (result.get("findings") or []) if isinstance(f, dict)]
    if findings:
        lines.append("")
        for finding in findings:
            where = str(finding.get("path") or "general")
            if finding.get("line") is not None:
                where = f"{where}:{finding['line']}"
            lines.append(f"- **{where}** — {finding.get('comment') or finding.get('note') or ''}")
    return "\n".join(lines) + "\n"


def inline_comments(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Findings that name a file *and* a line, which are the only ones GitHub can anchor.

    A finding with no line is not dropped — it is already in the body. Sending it as an inline
    comment without an anchor is how a review fails to post at all.
    """
    comments = []
    for finding in result.get("findings") or []:
        if not isinstance(finding, dict) or not finding.get("path") or finding.get("line") is None:
            continue
        comments.append(
            {
                "path": str(finding["path"]),
                "line": int(finding["line"]),
                "side": "RIGHT",
                "body": str(finding.get("comment") or finding.get("note") or ""),
            }
        )
    return comments


def review_payload(
    aspect: str, result: dict[str, Any], *, sha: str, previous_id: str = ""
) -> tuple[str, dict[str, Any]]:
    """What to send, and whether it is a new review or a revision of one.

    A submitted review's body can be updated; its inline comments cannot. So a revision goes into
    the body alone, and the thread keeps one review per aspect however often the branch moves.
    """
    body = render_review(aspect, result, sha=sha)
    if previous_id:
        return "revise", {"body": body}
    payload: dict[str, Any] = {"body": body, "event": "COMMENT", "comments": inline_comments(result)}
    if sha:
        payload["commit_id"] = sha
    return "post", payload
