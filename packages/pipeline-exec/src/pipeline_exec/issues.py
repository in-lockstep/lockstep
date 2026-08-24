"""Reducing one GitHub issue to what an implementing agent actually needs.

The Jira fetcher this parallels has to guess: acceptance criteria live in a custom field whose id
differs per instance, so it scans every `customfield_*` for the word "given". GitHub needs no
guessing — a task list and a heading are both things the platform itself renders, so the two places
criteria actually get written are the two places this reads.

What it deliberately does not do is invent a type taxonomy. Jira issues have an issue type; GitHub
issues have labels, and mapping one onto the other would be this runtime deciding what a repository's
labels mean. The labels travel verbatim and the agent reads them.
"""

from __future__ import annotations

import re
from typing import Any

MAX_BODY = 12000
MAX_COMMENT = 4000
MAX_COMMENTS = 30

# `- [ ] text` / `* [x] text` — GitHub's own task list, which it renders as checkboxes.
TASK_ITEM = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(.+?)\s*$")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
BULLET = re.compile(r"^\s*[-*]\s+(?:\[[ xX]\]\s+)?(.+?)\s*$")
# "Acceptance criteria", "Acceptance Criteria:", "AC", "Definition of done".
CRITERIA_HEADING = re.compile(r"^(acceptance\s+criteria|ac|definition\s+of\s+done)\b", re.IGNORECASE)


def criteria_from(body: str) -> list[str]:
    """The acceptance criteria in an issue body, from a heading if there is one.

    A section beats a task list when both exist: a task list can be anything — a checklist of
    affected files, a rollout plan — while a section under that heading was written to be this.
    """
    return _section_criteria(body) or _task_list(body)


def _section_criteria(body: str) -> list[str]:
    lines = body.splitlines()
    found: list[str] = []
    depth = 0
    collecting = False
    for line in lines:
        heading = HEADING.match(line)
        if heading:
            level, text = len(heading.group(1)), heading.group(2)
            if collecting and level <= depth:
                break
            if CRITERIA_HEADING.match(text):
                collecting, depth = True, level
            continue
        if not collecting:
            continue
        bullet = BULLET.match(line)
        if bullet:
            found.append(bullet.group(1))
        elif line.strip() and not found:
            # Prose directly under the heading, where somebody wrote criteria as a paragraph.
            found.append(line.strip())
    return found


def _task_list(body: str) -> list[str]:
    """Every task-list item, checked or not.

    A checked box in an issue being implemented usually means "agreed", not "already built" — so
    dropping them would drop requirements. The text is what the agent needs either way.
    """
    return [match.group(2) for line in body.splitlines() if (match := TASK_ITEM.match(line))]


def reduce_issue(
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    *,
    repo: str = "",
    max_body: int = MAX_BODY,
    max_comment: int = MAX_COMMENT,
    max_comments: int = MAX_COMMENTS,
) -> dict[str, Any]:
    """One issue, reduced. Same four keys the Jira fetcher emits, plus what GitHub actually has.

    The discussion is included because on GitHub it is frequently where the requirement ended up —
    an issue body says "login is broken" and the third comment says which endpoint. It is capped
    and truncated for the same reason review feedback is: somebody pastes a log, and the work item
    itself falls out of the model's context.
    """
    number = issue.get("number")
    body = issue.get("body") or ""
    return {
        "key": f"#{number}" if number else "",
        "number": number,
        "repo": repo,
        "url": issue.get("html_url", ""),
        "state": issue.get("state", ""),
        "summary": issue.get("title", ""),
        "description": body[:max_body],
        "acceptance_criteria": criteria_from(body),
        "labels": [_name(label) for label in issue.get("labels") or []],
        "assignees": [(user or {}).get("login", "") for user in issue.get("assignees") or []],
        "discussion": [
            {
                "author": (comment.get("user") or {}).get("login", ""),
                "body": (comment.get("body") or "")[:max_comment],
            }
            for comment in comments[:max_comments]
        ],
    }


def _name(label: Any) -> str:
    """Labels arrive as objects from the API and as strings from a webhook payload."""
    return label.get("name", "") if isinstance(label, dict) else str(label)
