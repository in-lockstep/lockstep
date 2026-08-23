"""Collecting a pull request's review as pipeline input.

Any pipeline that proposes work can be reviewed, and a reviewer's comments are that pipeline's most
valuable input and its least trustworthy: they arrive from a text box, they are addressed to a human,
and they are about to be read by a model that will act on them.

So reducing them is a deliberate step with rules rather than a `gh api` call inlined into a workflow.
This began life as an extension in one pipeline; a second pipeline needing it is what moved it here.
"""

from __future__ import annotations

from typing import Any

MAX_COMMENT = 4000
MAX_COMMENTS = 60


def normalize(
    reviews: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    issue_comments: list[dict[str, Any]],
    *,
    max_comment: int = MAX_COMMENT,
    max_comments: int = MAX_COMMENTS,
) -> dict[str, Any]:
    """Reduce a pull request's review to what an agent can act on.

    Inline comments keep their file and line, because "this is wrong" means nothing without them.
    Everything is truncated and capped: a reviewer who pastes a log should not be able to push the
    work item itself out of the model's context.
    """
    inline = [
        {
            "path": comment.get("path", ""),
            "line": comment.get("line") or comment.get("original_line"),
            "body": (comment.get("body") or "")[:max_comment],
            "author": (comment.get("user") or {}).get("login", ""),
        }
        for comment in comments
        if (comment.get("body") or "").strip()
    ][:max_comments]

    general = [
        {
            "state": review.get("state", ""),
            "body": (review.get("body") or "")[:max_comment],
            "author": (review.get("user") or {}).get("login", ""),
        }
        for review in reviews
        if (review.get("body") or "").strip()
    ][:max_comments]

    discussion = [
        {
            "body": (comment.get("body") or "")[:max_comment],
            "author": (comment.get("user") or {}).get("login", ""),
        }
        for comment in issue_comments
        # A comment that only invokes the pipeline is a request to run, not feedback on the work.
        if (comment.get("body") or "").strip() and not (comment.get("body") or "").lstrip().startswith("/")
    ][:max_comments]

    return {
        "inline": inline,
        "reviews": general,
        "discussion": discussion,
        "requested_changes": any(r.get("state") == "CHANGES_REQUESTED" for r in reviews),
        "count": len(inline) + len(general) + len(discussion),
    }


def group_by_path(feedback: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Inline comments grouped by the file they were left on.

    A pipeline that fans out over many items needs to route feedback to the leg it concerns, and the
    file a comment sits on is the only reliable signal of which one that is.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for comment in feedback.get("inline", []):
        grouped.setdefault(comment.get("path", ""), []).append(comment)
    return grouped
