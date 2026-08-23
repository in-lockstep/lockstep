"""Searching the tracker.

One command, and most of it is about what *not* to carry forward. An issue record from a tracker is
enormous and mostly irrelevant — worklogs, watchers, render fields, changelog. What reaches the
report is chosen here, deliberately, because everything that reaches it costs context the model
could have spent reasoning instead.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import click

MAX_DESCRIPTION = 1500


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


def reduce_issue(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep what a triage decision turns on, and nothing else."""
    fields = raw.get("fields", {}) or {}
    return {
        "key": raw.get("key", ""),
        "summary": fields.get("summary", ""),
        "description": (fields.get("description") or "")[:MAX_DESCRIPTION],
        "type": (fields.get("issuetype") or {}).get("name", "unknown"),
        "priority": (fields.get("priority") or {}).get("name", "unset"),
        "status": (fields.get("status") or {}).get("name", "unknown"),
        "reporter": ((fields.get("reporter") or {}).get("displayName") or "unknown"),
        "created": fields.get("created", ""),
        "updated": fields.get("updated", ""),
        "labels": list(fields.get("labels") or []),
        "components": [c.get("name", "") for c in (fields.get("components") or [])],
    }


@click.command(name="jql-search")
@click.option("--jql", required=True, help="The query defining what gets triaged.")
@click.option("--output", required=True, type=click.Path(path_type=Path))
@click.option("--limit", default=60, show_default=True, type=int)
@click.option("--base-url", envvar="JIRA_BASE_URL", default="")
@click.option("--token", envvar="JIRA_API_TOKEN", default="")
@click.option("--from-file", type=click.Path(path_type=Path), help="Read a fixture instead.")
def jql_search(jql: str, output: Path, limit: int, base_url: str, token: str, from_file: Path | None) -> None:
    """Run a query and reduce each result to what a triage decision turns on."""
    if from_file:
        payload = json.loads(from_file.read_text(encoding="utf-8"))
        raw_issues = payload.get("issues", payload if isinstance(payload, list) else [])
    else:
        if not base_url or not token:
            _fail("JIRA_BASE_URL and JIRA_API_TOKEN are required unless --from-file is given")
        import httpx

        raw_issues = []
        start = 0
        with httpx.Client(timeout=30, headers={"Authorization": f"Bearer {token}"}) as client:
            while len(raw_issues) < limit:
                response = client.get(
                    f"{base_url.rstrip('/')}/rest/api/2/search",
                    params={"jql": jql, "startAt": start, "maxResults": min(50, limit - len(raw_issues))},
                )
                if response.status_code != 200:
                    _fail(f"tracker returned {response.status_code}: {response.text[:200]}")
                page = response.json()
                batch = page.get("issues", [])
                if not batch:
                    break
                raw_issues.extend(batch)
                start += len(batch)
                if start >= page.get("total", 0):
                    break

    issues = [reduce_issue(raw) for raw in raw_issues[:limit]]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(issues, indent=2) + "\n", encoding="utf-8")

    _emit_output("count", str(len(issues)))
    click.echo(f"found {len(issues)} issue(s) -> {output}")
