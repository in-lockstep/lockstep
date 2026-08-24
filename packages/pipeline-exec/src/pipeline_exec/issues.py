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


# --- Jira -----------------------------------------------------------------------------------------
#
# The same four keys, from a tracker that stores them differently. What GitHub renders as a heading
# or a task list, Jira puts in a custom field whose id differs per instance — so this reads, in
# order: the field somebody configured, then a field that looks like acceptance criteria, then the
# description itself. Each fallback is weaker than the one before it, and `criteria_source` in the
# output says which one answered, because "no criteria" and "we guessed" are different situations
# and an agent handed them silently would treat them the same.

CRITERIA_HINTS = ("acceptance", "given", "criteria")


def jira_criteria(fields: dict[str, Any], *, field_id: str = "") -> tuple[list[str], str]:
    """Acceptance criteria, and how they were found."""
    if field_id:
        value = fields.get(field_id)
        if isinstance(value, str) and value.strip():
            return _lines(value), f"field {field_id}"
        # Configured and empty is a fact worth reporting, not a reason to go guessing: somebody
        # said where these live, and the answer is that this issue has none.
        return [], f"field {field_id} (empty)"

    for key, value in sorted(fields.items()):
        if not key.startswith("customfield_") or not isinstance(value, str) or not value.strip():
            continue
        if any(hint in value.lower() for hint in CRITERIA_HINTS):
            return _lines(value), f"guessed from {key}"

    described = criteria_from(str(fields.get("description") or ""))
    return described, "description" if described else "none"


def _lines(value: str) -> list[str]:
    return [line.strip("-*• ").strip() for line in value.splitlines() if line.strip()]


def reduce_jira_issue(
    raw: dict[str, Any], *, criteria_field: str = "", max_body: int = MAX_BODY
) -> dict[str, Any]:
    """One Jira issue, reduced to the shape `reduce_issue` produces for GitHub.

    One shape, so a pipeline reads `summary`, `description` and `acceptance_criteria` without
    knowing which tracker it is pointed at — and so an agent's eval cases are about the work rather
    than about the API that delivered it. What differs between trackers stays alongside rather than
    being flattened away: an issue type is a real thing in Jira and is not a GitHub label.
    """
    fields = raw.get("fields") or {}
    criteria, source = jira_criteria(fields, field_id=criteria_field)
    status = (fields.get("status") or {}).get("name", "")
    return {
        "key": raw.get("key", ""),
        "number": None,
        "repo": "",
        "url": raw.get("self", ""),
        "state": status,
        "summary": fields.get("summary", ""),
        "description": str(fields.get("description") or "")[:max_body],
        "acceptance_criteria": criteria,
        "criteria_source": source,
        "labels": list(fields.get("labels") or []),
        "assignees": [name for name in [(fields.get("assignee") or {}).get("displayName", "")] if name],
        "discussion": [],
        # Tracker-native, kept rather than mapped onto something GitHub-shaped.
        "type": (fields.get("issuetype") or {}).get("name", ""),
        "components": [c.get("name", "") for c in fields.get("components") or []],
        "priority": (fields.get("priority") or {}).get("name", ""),
    }
