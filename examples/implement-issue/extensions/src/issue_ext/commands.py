"""Commands the implement-issue pipeline needs.

The interesting one is `pr-feedback`. A reviewer's comments are the pipeline's most valuable input
and its least trustworthy: they arrive from a text box, they are addressed to a human, and they get
fed to a model that is about to write code. Collecting them is therefore a deliberate step with
rules, not a `gh api` call inlined into a workflow.
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

MAX_COMMENT = 4000
MAX_COMMENTS = 60


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


# --- pr-feedback -----------------------------------------------------------


def normalize(reviews: list[dict], comments: list[dict], issue_comments: list[dict]) -> dict[str, Any]:
    """Reduce a pull request's review to what a code-writing agent can act on.

    Inline comments keep their file and line, because "this is wrong" means nothing without them.
    Everything is truncated and capped: a reviewer who pastes a log should not be able to push the
    issue itself out of the model's context.
    """
    inline = [
        {
            "path": comment.get("path", ""),
            "line": comment.get("line") or comment.get("original_line"),
            "body": (comment.get("body") or "")[:MAX_COMMENT],
            "author": (comment.get("user") or {}).get("login", ""),
        }
        for comment in comments
        if (comment.get("body") or "").strip()
    ][:MAX_COMMENTS]

    general = [
        {
            "state": review.get("state", ""),
            "body": (review.get("body") or "")[:MAX_COMMENT],
            "author": (review.get("user") or {}).get("login", ""),
        }
        for review in reviews
        if (review.get("body") or "").strip()
    ][:MAX_COMMENTS]

    discussion = [
        {
            "body": (comment.get("body") or "")[:MAX_COMMENT],
            "author": (comment.get("user") or {}).get("login", ""),
        }
        for comment in issue_comments
        # A comment that only invokes the pipeline is not feedback about the code.
        if (comment.get("body") or "").strip() and not (comment.get("body") or "").lstrip().startswith("/")
    ][:MAX_COMMENTS]

    return {
        "inline": inline,
        "reviews": general,
        "discussion": discussion,
        "requested_changes": any(r.get("state") == "CHANGES_REQUESTED" for r in reviews),
        "count": len(inline) + len(general) + len(discussion),
    }


@click.command(name="pr-feedback")
@click.option("--pr", default="", help="Pull request number. Empty on a first run.")
@click.option("--repo", envvar="GITHUB_REPOSITORY", required=True)
@click.option("--output", required=True, type=click.Path(path_type=Path))
@click.option("--from-dir", type=click.Path(path_type=Path), help="Read fixtures instead of the API.")
def pr_feedback(pr: str, repo: str, output: Path, from_dir: Path | None) -> None:
    """Collect a pull request's review feedback as pipeline input.

    A first run has no pull request and therefore no feedback. That is the normal case, not an
    error: the same step runs on every invocation and simply has nothing to say the first time.
    """
    if not pr.strip() and not from_dir:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(normalize([], [], []), indent=2) + "\n", encoding="utf-8")
        _emit_output("count", "0")
        _emit_output("requested_changes", "false")
        click.echo("no pull request yet; no feedback to collect")
        return

    if from_dir:
        def load(name: str) -> list[dict]:
            path = from_dir / f"{name}.json"
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []

        reviews, comments, issue_comments = load("reviews"), load("comments"), load("issue_comments")
    else:
        reviews = _gh(f"repos/{repo}/pulls/{pr}/reviews", "--paginate")
        comments = _gh(f"repos/{repo}/pulls/{pr}/comments", "--paginate")
        issue_comments = _gh(f"repos/{repo}/issues/{pr}/comments", "--paginate")

    feedback = normalize(reviews, comments, issue_comments)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(feedback, indent=2) + "\n", encoding="utf-8")

    _emit_output("count", str(feedback["count"]))
    _emit_output("requested_changes", "true" if feedback["requested_changes"] else "false")
    click.echo(f"collected {feedback['count']} piece(s) of feedback -> {output}")


# --- await-checks ----------------------------------------------------------


@click.command(name="await-checks")
@click.option("--ref", required=True, help="Commit SHA or branch whose checks to wait for.")
@click.option("--repo", envvar="GITHUB_REPOSITORY", required=True)
@click.option("--timeout", default=1800, show_default=True, type=int, help="Seconds.")
@click.option("--interval", default=20, show_default=True, type=int)
@click.option("--ignore", default="", help="Comma-separated check names to disregard.")
@click.option("--output", type=click.Path(path_type=Path))
def await_checks(
    ref: str, repo: str, timeout: int, interval: int, ignore: str, output: Path | None
) -> None:
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
        click.echo(f"waiting: {sum(1 for r in runs if r.get('status') != 'completed')} still running", err=True)
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
