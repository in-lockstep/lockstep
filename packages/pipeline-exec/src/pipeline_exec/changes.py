"""Turning an agent's proposal into a change, and finding out whether it holds up.

Promoted out of `examples/implement-issue` and `examples/bug-fix`, where these lived in extension
packages a shipped pipeline could not depend on. The logic moved rather than being rewritten.

Three of these carry an argument worth restating.

**`apply_patch` is where the rules live, because it is the only thing that writes.** The agent that
produced the diff has no write permission and never touches the repository. A prompt telling it not
to edit CI configuration is a request; a check here is the thing that holds.

**A reproducer that does not fail proves nothing.** So `run_suite` takes what it expects, and a
pipeline asserts the failure before the fix and the pass after it. A test that passes both times has
told you nothing about the bug and would sit in the suite forever looking like coverage.

**The CI that judges a change is the project's own.** Not a test command this pipeline chose, which
would only prove the change satisfies this pipeline.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

# Paths an agent-proposed patch may not touch, whatever it says in the diff. Everything here can
# change what runs in CI or what a later run is allowed to do, which makes them the paths a
# compromised or merely confused agent would most want.
PROTECTED = (".github/", ".pipeline/", "profiles/", "guardrails/", "agents/", "commands/")

SUITES = {
    "pytest": ["python", "-m", "pytest", "-q"],
    "jest": ["npx", "jest", "--silent"],
    "go": ["go", "test", "./..."],
    "cargo": ["cargo", "test", "--quiet"],
}


class ChangeError(ValueError):
    """A proposal that will not be applied, refused before anything is written."""


def protected_paths(diff: str) -> list[str]:
    """Files a patch touches that it must not."""
    touched = re.findall(r"^\+\+\+ b/(.+)$", diff, flags=re.MULTILINE)
    return sorted({path for path in touched if path.startswith(PROTECTED)})


def suite_verdict(
    result: subprocess.CompletedProcess[str], *, suite: str, select: str, expect: str
) -> dict[str, Any]:
    passed = result.returncode == 0
    return {
        "suite": suite,
        "select": select,
        "passed": passed,
        "expected": expect,
        # The question is not whether the tests passed. It is whether they did what this step of the
        # pipeline needed them to do.
        "satisfied": passed if expect == "pass" else not passed,
        "output": (result.stdout + result.stderr)[-4000:],
    }


def check_verdict(runs: list[dict[str, Any]], *, ref: str) -> dict[str, Any]:
    """What the project's own CI concluded.

    `neutral` and `skipped` are not failures: a check that decided it had nothing to do has not
    rejected the change, and treating it as a rejection would block every change that did not touch
    the thing that check watches.
    """
    failed = [r["name"] for r in runs if r.get("conclusion") not in ("success", "neutral", "skipped")]
    return {
        "ref": ref,
        "checks": [{"name": r.get("name", ""), "conclusion": r.get("conclusion")} for r in runs],
        "failed": failed,
        "passed": not failed,
    }


def render_plan(plan: dict[str, Any]) -> str:
    """The plan as the markdown a reviewer reads on the pull request.

    Deterministic on purpose. The planning agent decided *what* the plan says; how it is laid out is
    formatting, and formatting that varies between runs makes the comment churn — which matters
    because that comment is updated in place across many iterations.
    """
    lines = [str(plan.get("summary") or "").strip(), ""]

    if plan.get("approach"):
        lines += ["### Approach", "", str(plan["approach"]).strip(), ""]

    if plan.get("rejected"):
        lines += ["### Considered and rejected", ""]
        lines += [f"- **{item.get('option', '')}** — {item.get('reason', '')}" for item in plan["rejected"]]
        lines += [""]

    if plan.get("changes"):
        lines += ["### Files this changes", "", "| File | Why |", "|---|---|"]
        lines += [f"| `{c.get('path', '')}` | {c.get('reason', '')} |" for c in plan["changes"]]
        lines += [""]

    if plan.get("verification"):
        lines += ["### How this is proven", "", str(plan["verification"]).strip(), ""]

    if plan.get("risks"):
        lines += ["### What this could break", ""]
        lines += [f"- {risk}" for risk in plan["risks"]]
        lines += [""]

    if plan.get("open_questions"):
        lines += ["### Open questions", ""]
        lines += [f"- {question}" for question in plan["open_questions"]]
        lines += [""]

    return "\n".join(lines).strip() + "\n"


def load_plan(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))
