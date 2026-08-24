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


FETCH_TOKEN_ENV = "LOCKSTEP_FETCH_TOKEN"
APP_TOKEN_STEP = "inherits-token"


def _fetch_steps(spec: Spec, ctx: EmitContext) -> list[dict[str, Any]]:
    """Materialize the inherited trees, with a credential when the upstreams are private.

    A public upstream needs nothing: `lockstep fetch` reads it anonymously. A private one cannot be
    read by the consumer's own `GITHUB_TOKEN` at all — that token is scoped to the repository it
    belongs to — so the credential has to be declared, and this is where it is wired in rather than
    left to a comment in a README that every consuming repository re-derives.
    """
    auth = spec.manifest.inherits_auth
    steps: list[dict[str, Any]] = []
    token = ""

    if auth.uses_app:
        steps.append(
            {
                "name": "Mint a token for the private upstreams",
                "id": APP_TOKEN_STEP,
                "uses": ctx.pins.external_action("actions/create-github-app-token"),
                "with": {
                    "app-id": "${{ vars." + auth.app_id + " }}",
                    "private-key": "${{ secrets." + auth.private_key + " }}",
                    # The App is installed on the upstreams, not on this repository.
                    "owner": "${{ github.repository_owner }}",
                },
            }
        )
        token = "${{ steps." + APP_TOKEN_STEP + ".outputs.token }}"
    elif auth.token:
        token = "${{ secrets." + auth.token + " }}"

    fetch: dict[str, Any] = {"name": "Fetch inherited pipelines", "run": "lockstep fetch"}
    if token:
        fetch["env"] = {FETCH_TOKEN_ENV: token}
    steps.append(fetch)
    return steps


def emit_ci(spec: Spec, ctx: EmitContext) -> dict[str, Any]:
    compiler = ctx.pins.compiler_install()
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
            # Inherited definitions are resolved state, not committed source, so every check that
            # compiles has to materialize them first — at the commits the lock file records, which
            # is what keeps `--check` byte-for-byte honest.
            *(_fetch_steps(spec, ctx) if spec.manifest.inherits else []),
            *(extra or []),
        ]

    # The drift gate regenerates the `.lock.yml` files and compares them, so it needs the tool that
    # produces them — pinned, because a lock file is a function of the markdown *and* of the version
    # that compiled it. `gh` itself is already on every GitHub-hosted runner.
    gh_aw_setup: list[dict[str, Any]] = []
    if spec.manifest.capabilities.gh_aw:
        gh_aw_setup.append(
            {
                "name": f"Install gh-aw {spec.manifest.capabilities.gh_aw}",
                "run": f"gh extension install github/gh-aw --pin {spec.manifest.capabilities.gh_aw}",
                "env": {"GH_TOKEN": "${{ github.token }}"},
            }
        )

    jobs: dict[str, Any] = {
        "drift": {
            "name": "Drift gate",
            "runs-on": ctx.runs_on,
            "permissions": {"contents": "read"},
            "steps": [
                *setup(gh_aw_setup),
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
