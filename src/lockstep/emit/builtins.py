"""The `pipeline-exec` command surface, as the compiler understands it.

The compiler must not import `pipeline-exec` — a generated repo installs the runtime, never the
compiler, and a runtime dependency here would invert that. So the surface is declared, and
`tests/test_contract.py` asserts this declaration matches the real CLI. A `builtin:` step naming a
command that does not exist is then a compile error rather than a 2am `command not found`.
"""

from __future__ import annotations

# Commands a `builtin:` step may invoke.
AVAILABLE = frozenset(
    {
        # fan-out mechanics
        "fanout",
        "fanout-verify",
        "shard-run",
        # trust boundaries and readiness
        "validate-schema",
        "wait-for",
        # extracted from pipeline-framework
        "test-runner",
        "discover",
        "report",
        "collect-failures",
        "check-convergence",
    }
)

# GitHub refuses a matrix larger than this; `pipeline-exec` enforces the same number at run time.
MATRIX_CAP = 256
