"""Commands the implement-issue pipeline needs.

`pr-feedback` used to live here. A second pipeline needed it, so it moved into `pipeline-exec` —
which is the normal lifecycle for an extension that turns out to be general. Nothing in this
pipeline's spec changed: a `builtin:` step names a command, never the package providing it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import click


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


def _gh(*args: str) -> Any:
    """Call `gh api` and parse JSON, so tests can substitute a fixture instead."""
    result = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        _fail(f"gh api {' '.join(args)} failed: {result.stderr.strip()[:200]}")
    return json.loads(result.stdout or "[]")


# --- issue-fetch -----------------------------------------------------------


@click.command()
@click.option("--issue", required=True, help="The issue key to implement.")
@click.option("--output", required=True, type=click.Path(path_type=Path))
@click.option("--base-url", envvar="JIRA_BASE_URL", default="", help="Issue tracker base URL.")
@click.option("--token", envvar="JIRA_API_TOKEN", default="")
@click.option("--from-file", type=click.Path(path_type=Path), help="Read the issue from a file instead.")
def issue_fetch(issue: str, output: Path, base_url: str, token: str, from_file: Path | None) -> None:
    """Fetch one issue and reduce it to what an implementing agent actually needs."""
    if from_file:
        raw = json.loads(from_file.read_text(encoding="utf-8"))
    else:
        if not base_url or not token:
            _fail("JIRA_BASE_URL and JIRA_API_TOKEN are required unless --from-file is given")
        import httpx

        response = httpx.get(
            f"{base_url.rstrip('/')}/rest/api/2/issue/{issue}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if response.status_code != 200:
            _fail(f"issue tracker returned {response.status_code} for {issue}")
        raw = response.json()

    fields = raw.get("fields", {})
    document = {
        "key": raw.get("key", issue),
        "summary": fields.get("summary", ""),
        "description": (fields.get("description") or "")[:12000],
        "acceptance_criteria": _criteria(fields),
        "type": (fields.get("issuetype") or {}).get("name", ""),
        "components": [c.get("name", "") for c in fields.get("components", [])],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    click.echo(f"fetched {document['key']} -> {output}")


def _criteria(fields: dict[str, Any]) -> list[str]:
    """Acceptance criteria live in a custom field whose id differs per instance."""
    for key, value in fields.items():
        if key.startswith("customfield_") and isinstance(value, str) and "given" in value.lower():
            return [line.strip("-* ") for line in value.splitlines() if line.strip()]
    return []


# --- await-checks ----------------------------------------------------------


@click.command(name="await-checks")
@click.option("--ref", required=True, help="Commit SHA or branch whose checks to wait for.")
@click.option("--repo", envvar="GITHUB_REPOSITORY", required=True)
@click.option("--timeout", default=1800, show_default=True, type=int, help="Seconds.")
@click.option("--interval", default=20, show_default=True, type=int)
@click.option("--ignore", default="", help="Comma-separated check names to disregard.")
@click.option("--output", type=click.Path(path_type=Path))
def await_checks(ref: str, repo: str, timeout: int, interval: int, ignore: str, output: Path | None) -> None:
    """Wait for the repository's own CI to finish, and report what it concluded.

    The pipeline writes a change and then asks the project's real CI whether it holds up — not a
    test command this pipeline chose, which would only prove the change satisfies the pipeline.
    """
    skip = {name.strip() for name in ignore.split(",") if name.strip()}
    deadline = time.monotonic() + timeout
    runs: list[dict[str, Any]] = []

    while time.monotonic() < deadline:
        payload = _gh(f"repos/{repo}/commits/{ref}/check-runs")
        runs = [r for r in payload.get("check_runs", []) if r.get("name") not in skip]
        if runs and all(run.get("status") == "completed" for run in runs):
            break
        click.echo(
            f"waiting: {sum(1 for r in runs if r.get('status') != 'completed')} still running", err=True
        )
        time.sleep(interval)
    else:
        _fail(f"checks on {ref} did not finish within {timeout}s")

    failed = [r["name"] for r in runs if r.get("conclusion") not in ("success", "neutral", "skipped")]
    verdict = {
        "ref": ref,
        "checks": [{"name": r["name"], "conclusion": r.get("conclusion")} for r in runs],
        "failed": failed,
        "passed": not failed,
    }
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(verdict, indent=2) + "\n", encoding="utf-8")

    _emit_output("passed", "true" if verdict["passed"] else "false")
    _emit_output("failed", ",".join(failed))
    click.echo(f"{len(runs)} check(s): {'all passed' if not failed else 'failed: ' + ', '.join(failed)}")
    if failed:
        _fail("the repository's own CI rejected this change")
