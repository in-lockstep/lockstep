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
from .command import CommandTest, CommandValidate
from .pytest_adapter import PytestTest, Test
from .ruff_adapter import RuffValidate, Validate


def detected_bindings(facts: RepoFacts) -> list[tuple[type[Any], Any]]:
    """The `(interface, implementation)` pairs a repository's detected parts imply. Empty when
    nothing recognisable was found — an honest absence, not a guessed default that would run."""
    out: list[tuple[type[Any], Any]] = []

    if facts.pytest:
        out.append((Test, PytestTest(args=["-q", "--no-header"])))
    elif facts.test_command:
        out.append((Test, CommandTest(facts.test_command)))

    if facts.ruff:
        out.append((Validate, RuffValidate()))
    elif facts.lint_command:
        out.append((Validate, CommandValidate(facts.lint_command)))

    return out
