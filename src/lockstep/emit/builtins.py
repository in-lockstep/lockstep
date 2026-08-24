"""The `pipeline-exec` command surface, as the compiler understands it.

The compiler must not import `pipeline-exec` — a generated repo installs the runtime, never the
compiler, and a runtime dependency here would invert that. So the surface is declared, and
`tests/test_contract.py` asserts this declaration matches the real CLI. A `builtin:` step naming a
command that does not exist is then a compile error rather than a 2am `command not found`.
"""

from __future__ import annotations

# Commands a `builtin:` step may name in the spec.
AVAILABLE = frozenset(
    {
        "test-runner",
        "discover",
        "report",
        "collect-failures",
        "check-convergence",
        "validate-schema",
        "wait-for",
        "pr-feedback",
        "gh-issue-fetch",
        # Introspection: how a pipeline repository proves in CI that the builtins its spec names
        # actually exist, extensions included.
        "list-commands",
        # One shape from either tracker, so a pipeline reading `acceptance_criteria` does not have
        # to know which one delivered it.
        "issue-fetch",
        # The counterpart to gh-aw's safe outputs, for the tracker that has none.
        "jira-update",
        # Reviewing a pull request. Promoted out of `examples/pr-review` so a shipped pipeline can
        # depend on them: an extension package in an example is not something the library can name.
        "pr-diff",
        "review-state",
        "post-reviews",
        # Implementing a change and finding out whether it holds up. Promoted out of
        # `examples/implement-issue` and `examples/bug-fix` for the same reason.
        "apply-patch",
        "run-suite",
        "await-checks",
        "render-plan",
    }
)

# Commands the compiler emits itself, as fan-out glue or from inside a composite action. They are
# not spec surface: a `builtin:` step naming one of these would be describing plumbing, not work.
INTERNAL = frozenset(
    {
        "fanout",
        "fanout-verify",
        "shard-run",
        "cache-key",
        "parse-command",
        # Eval plumbing. The compiler writes these into the eval workflow itself; a `builtin:` step
        # naming one would be describing the harness rather than the work.
        "scan-input",
        "eval-cases",
        "eval-judge-prep",
        "eval-grade",
        # Accounting. Emitted at the end of a run, over artifacts the run produced; a `builtin:`
        # step naming it would be asking a pipeline to bill itself mid-flight.
        "meter",
    }
)

# Third-party actions the compiler emits directly, with the tag `lockstep pin` resolves.
EXTERNAL_ACTIONS = {
    "actions/checkout": "v4",
    # Mints a short-lived installation token for reading a private upstream. Emitted only when a
    # pipeline declares `inherits-auth.app-id`, and pinned by `lockstep pin` like anything else.
    "actions/create-github-app-token": "v2",
    # Metering. Emitted only when a pipeline declares `otel.export`, so a pipeline that does not
    # meter carries no pin for either of these.
    "actions/download-artifact": "v5",
    "actions/upload-artifact": "v5",
}

# GitHub refuses a matrix larger than this; `pipeline-exec` enforces the same number at run time.
MATRIX_CAP = 256
