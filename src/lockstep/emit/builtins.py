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
    }
)

# GitHub refuses a matrix larger than this; `pipeline-exec` enforces the same number at run time.
MATRIX_CAP = 256
