"""Commands this pipeline needs and the framework will never ship.

Three of them, each teaching a different reason to write a builtin rather than a script or an agent:

- `jira-fetch` talks to a system with pagination and auth. A script could do it, but every pipeline
  in the organisation would then carry its own copy of the same paging bug.
- `apply-patch` is a trust boundary. An agent proposes a diff; this decides whether it may land.
  That decision must be code, reviewable and testable, never a model's judgement.
- `run-suite` turns a foreign test suite's exit code and output into a verdict the pipeline can
  branch on, without the pipeline knowing whether the project uses pytest, jest or go test.
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

# What a patch may never touch, no matter what an agent decided. CI configuration and the pipeline's
# own definitions are how this repository defends itself; a fix that edits them is not a fix.
PROTECTED = (
    ".github/",
    ".pipeline/",
    "commands/",
    "agents/",
    "guardrails/",
    "pipeline.yaml",
)


def _fail(message: str) -> None:
    click.echo(click.style(message, fg="red"), err=True)
    sys.exit(1)


def _emit_output(name: str, value: str) -> None:
    """Publish a step output the same way pipeline-exec does, so callers cannot tell the difference."""
    destination = os.environ.get("GITHUB_OUTPUT")
    if destination:
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    else:
        click.echo(f"{name}={value}")


# --- jira-fetch ------------------------------------------------------------


@click.command()
@click.option("--jql", required=True, help="Which issues to fetch.")
@click.option("--output", required=True, type=click.Path(path_type=Path))
@click.option("--limit", default=25, show_default=True, type=int)
@click.option("--base-url", envvar="JIRA_BASE_URL", required=True)
@click.option("--token", envvar="JIRA_API_TOKEN", required=True)
def jira_fetch(jql: str, output: Path, limit: int, base_url: str, token: str) -> None:
    """Fetch bugs as a work list the pipeline can fan out over."""
    import httpx

    issues: list[dict[str, Any]] = []
    start = 0
    with httpx.Client(timeout=30, headers={"Authorization": f"Bearer {token}"}) as client:
        while len(issues) < limit:
            response = client.get(
                f"{base_url.rstrip('/')}/rest/api/2/search",
                params={"jql": jql, "startAt": start, "maxResults": min(50, limit - len(issues))},
            )
            if response.status_code != 200:
                _fail(f"Jira returned {response.status_code}: {response.text[:200]}")
            payload = response.json()
            batch = payload.get("issues", [])
            if not batch:
                break
            issues.extend(batch)
            start += len(batch)
            if start >= payload.get("total", 0):
                break

    # `key` is the field `fanout` keys on, so it decides both the matrix legs and the output
    # filenames. Everything else here exists so the analysing agent needs no tools.
    work = [
        {
            "key": issue["key"],
            "summary": issue["fields"].get("summary", ""),
            "description": (issue["fields"].get("description") or "")[:6000],
            "priority": (issue["fields"].get("priority") or {}).get("name", "unknown"),
            "components": [c.get("name", "") for c in issue["fields"].get("components", [])],
        }
        for issue in issues[:limit]
    ]

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(work, indent=2) + "\n", encoding="utf-8")
    click.echo(f"fetched {len(work)} issue(s) -> {output}")


# --- apply-patch -----------------------------------------------------------


def protected_paths(diff: str) -> list[str]:
    """Files a patch touches that it must not."""
    touched = re.findall(r"^\+\+\+ b/(.+)$", diff, flags=re.MULTILINE)
    return sorted({path for path in touched if path.startswith(PROTECTED)})


@click.command()
@click.option("--patch", required=True, type=click.Path(path_type=Path))
@click.option("--repo", default=".", type=click.Path(path_type=Path), help="Where to apply it.")
@click.option("--check", is_flag=True, help="Report whether it would apply, without applying it.")
def apply_patch(patch: Path, repo: Path, check: bool) -> None:
    """Apply an agent-proposed diff, refusing anything that reaches outside the source tree.

    The agent that wrote this diff has no write permission and never touches the repository. This is
    the only thing that does, which is why the rules live here in code rather than in a prompt.
    """
    if not patch.is_file():
        _fail(f"{patch} does not exist")
    diff = patch.read_text(encoding="utf-8")
    if not diff.strip():
        click.echo("empty patch; nothing to apply")
        _emit_output("applied", "false")
        return

    breaches = protected_paths(diff)
    if breaches:
        _fail(f"patch touches protected paths and was rejected: {', '.join(breaches)}")

    command = ["git", "apply", "--3way", "--whitespace=nowarn"]
    if check:
        command = ["git", "apply", "--check"]
    result = subprocess.run(
        [*command, str(patch.resolve())], cwd=repo, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        click.echo(result.stderr.strip(), err=True)
        _emit_output("applied", "false")
        _fail("patch did not apply cleanly")

    click.echo("patch would apply" if check else "patch applied")
    _emit_output("applied", "false" if check else "true")


# --- run-suite -------------------------------------------------------------

SUITES = {
    "pytest": ["python", "-m", "pytest", "-q"],
    "jest": ["npx", "jest", "--silent"],
    "go": ["go", "test", "./..."],
    "cargo": ["cargo", "test", "--quiet"],
}


@click.command()
@click.option("--repo", default=".", type=click.Path(path_type=Path))
@click.option("--suite", type=click.Choice(sorted(SUITES)), default="pytest", show_default=True)
@click.option("--select", default="", help="Narrow the run, e.g. one reproducer test.")
@click.option("--output", type=click.Path(path_type=Path), help="Write the verdict as JSON.")
@click.option("--expect", type=click.Choice(["pass", "fail"]), default="pass", show_default=True)
def run_suite(repo: Path, suite: str, select: str, output: Path | None, expect: str) -> None:
    """Run the target project's own tests and turn the result into a verdict.

    `--expect fail` is what makes a reproducer meaningful: a test that does not fail before the fix
    proves nothing about the bug, so the pipeline asserts the failure first and the pass afterwards.
    """
    command = [*SUITES[suite], *(select.split() if select else [])]
    result = subprocess.run(command, cwd=repo, capture_output=True, text=True, check=False)
    passed = result.returncode == 0
    verdict = {
        "suite": suite,
        "select": select,
        "passed": passed,
        "expected": expect,
        "satisfied": passed if expect == "pass" else not passed,
        "output": (result.stdout + result.stderr)[-4000:],
    }

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(verdict, indent=2) + "\n", encoding="utf-8")
    _emit_output("passed", "true" if passed else "false")
    _emit_output("satisfied", "true" if verdict["satisfied"] else "false")
    click.echo(f"{suite}: {'passed' if passed else 'failed'} (expected to {expect})")

    if not verdict["satisfied"]:
        _fail(f"suite was expected to {expect} and did not")
