"""Turning what detection found into shipped-default bindings.

The composition root asks this for the bindings a `RepoFacts` implies, then installs them at
`Tier.DEFAULT` — so a repository that binds nothing still gets adapters that fit its stack, and an
explicit `lockstep.bind(...)` in the module wins over every one of them. The rule that a Node
repository must not silently get pytest bound lives here, as data rather than an `if` buried in the
CLI: this returns pytest only when pytest was actually detected.
"""

from __future__ import annotations

from typing import Any

from ..core.context import RepoFacts
from .command import Build, CommandBuild, CommandRun, CommandTest, CommandValidate, Run
from .pytest_adapter import PytestTest, Test
from .ruff_adapter import RuffValidate, Validate


def detected_bindings(facts: RepoFacts) -> list[tuple[type[Any], Any]]:
    """The `(interface, implementation)` pairs a repository's detected parts imply. Empty when
    nothing recognisable was found — an honest absence, not a guessed default that would run.

    The precedence, stated rather than left to the order of the `if`s: a tool with structured
    output wins the verb where that structure matters (pytest's per-test cases are what a fix loop
    reproduces from; ruff's per-rule findings are what a review reads), and the Makefile or
    package.json serves the verbs where an exit code is the whole answer. That is why `make test`
    is never bound here while `make build` is: the first would trade cases for an exit code, the
    second has nothing to trade.
    """
    out: list[tuple[type[Any], Any]] = []

    if facts.pytest:
        out.append((Test, PytestTest(args=["-q", "--no-header"])))
    elif facts.test_command:
        out.append((Test, CommandTest(facts.test_command)))

    if facts.ruff:
        out.append((Validate, RuffValidate()))
    elif facts.lint_command:
        out.append((Validate, CommandValidate(facts.lint_command)))

    # Only what is actually in the file, decided in `_detect_facts`: a `build` target or script
    # that exists. An invented `make build` is a binding that fails at run time.
    if facts.build_command:
        out.append((Build, CommandBuild(facts.build_command)))
    if facts.run_command:
        out.append((Run, CommandRun(facts.run_command)))

    return out
