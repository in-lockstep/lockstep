"""The checks a compiled pipeline repository runs on itself.

The drift gate is the load-bearing one: it recompiles every pull request and byte-compares, which is
what lets reviewers read a spec diff instead of thousands of lines of generated YAML. The rest —
lint, doctor, the policy gate, the scripts' own tests — are the gates that stop a pipeline from
degrading between reviews.
"""

from __future__ import annotations

from typing import Any

from ..spec.model import Spec
from .context import EmitContext

WORKFLOW_NAME = "pipeline-ci.yml"
SPEC_PATHS = [
    "commands/**",
    "agents/**",
    "guardrails/**",
    "skills/**",
    "contexts/**",
    "profiles/**",
    "mcp/**",
    "overlays/**",
    "scripts/**",
    "tests/**",
    "pipeline.yaml",
    ".pipeline/**",
]


def emit_ci(spec: Spec, ctx: EmitContext) -> dict[str, Any]:
    compiler = spec.manifest.capabilities.compiler or "lockstep"
    checkout = ctx.pins.external_action("actions/checkout")
    tests = spec.repo_path("tests")

    def setup(extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        return [
            {"uses": checkout, "with": {"fetch-depth": 0}},
            {"uses": "astral-sh/setup-uv@v6"},
            # Install the pinned compiler as a tool rather than syncing the repository's own
            # environment: a check must not execute project-defined build hooks to run.
            {
                "name": "Install the pinned compiler",
                "run": f'uv tool install "{compiler}"',
            },
            *(
                [
                    {
                        # Inherited definitions are resolved state, not committed source, so every
                        # check that compiles has to materialize them first — at the commits the
                        # lock file records, which is what keeps `--check` byte-for-byte honest.
                        "name": "Fetch inherited pipelines",
                        "run": "lockstep fetch",
                    }
                ]
                if spec.manifest.inherits
                else []
            ),
            *(extra or []),
        ]

    jobs: dict[str, Any] = {
        "drift": {
            "name": "Drift gate",
            "runs-on": ctx.runs_on,
            "permissions": {"contents": "read"},
            "steps": [
                *setup(),
                {
                    "name": "Recompile and compare",
                    # Committed output must equal a fresh compile of spec + overlays + pins. A
                    # hand-edit cannot merge, and a spec change without a recompile cannot either.
                    "run": (
                        "lockstep compile --check --semantic-diff --fail-on-blocking "
                        "--base=origin/${{ github.event.pull_request.base.ref || 'main' }}"
                    ),
                },
            ],
        },
        "lint": {
            "name": "Spec quality",
            "runs-on": ctx.runs_on,
            "permissions": {"contents": "read"},
            "steps": [*setup(), {"name": "Lint the spec", "run": "lockstep lint"}],
        },
        "doctor": {
            "name": "Target readiness",
            "runs-on": ctx.runs_on,
            "permissions": {"contents": "read"},
            "steps": [
                *setup(),
                {"name": "Check target readiness", "run": "lockstep doctor --target=github-agentic"},
            ],
        },
        "scripts": {
            "name": "Script tests",
            "runs-on": ctx.runs_on,
            "permissions": {"contents": "read"},
            "steps": [
                {"uses": checkout},
                {"uses": "astral-sh/setup-uv@v6"},
                {
                    "name": "Run the scripts' own tests",
                    # Script steps run on every execution, so a regression here is silent. These are
                    # the *pipeline's* tests: in a repository that already had its own, they live
                    # beside the pipeline and are run separately from whatever CI it already had.
                    "run": (
                        f"if [ -d {tests} ]; then uv run pytest {tests} -q; "
                        f'else echo "no {tests} directory"; fi'
                    ),
                },
            ],
        },
    }

    return {
        "name": "pipeline-ci",
        "on": {
            "pull_request": {
                "paths": [spec.repo_path(path) for path in SPEC_PATHS]
                + [f"{ctx.out_dir}/**"]
                # Already repository-relative: `watch` names things outside the pipeline's home.
                + list(spec.manifest.target.watch)
            },
            "push": {"branches": ["main"]},
            "workflow_dispatch": {},
        },
        "permissions": {"contents": "read"},
        "concurrency": {
            "group": "pipeline-ci-${{ github.ref }}",
            "cancel-in-progress": True,
        },
        "jobs": jobs,
    }
