"""Turning what detection found into shipped-default bindings.

The composition root asks this for the bindings a `RepoFacts` implies and installs them at
`Tier.DEFAULT` — for a repository with no `lockstep.py` at all, which then runs on adapters that fit
its stack. A repository that ships a module has made its choices and gets none of these silently;
`init` writes the same answer into the module it scaffolds, so the two paths agree on the day the
module is created and the module is the truth from then on (`cli._default_lockstep` says why). The
rule that a Node repository must not silently get pytest bound lives here, as data rather than an
`if` buried in the CLI: this returns pytest only when pytest was actually detected.
"""

from __future__ import annotations

from typing import Any

from ..core.context import RepoFacts
from .command import (
    Build,
    CommandBuild,
    CommandProvision,
    CommandRun,
    CommandTest,
    CommandValidate,
    Provision,
    Run,
)
from .pytest_adapter import PytestTest, Test
from .ruff_adapter import RuffValidate, Validate


def detected_bindings(facts: RepoFacts) -> list[tuple[type[Any], Any]]:
    """The `(interface, implementation)` pairs a repository's detected parts imply. Empty when
    nothing recognisable was found — an honest absence, not a guessed default that would run.

    The precedence, decided in `_detect_facts` and stated here where it is consumed: a tool with
    structured output wins the verb where that structure matters (pytest's per-test cases are what
    a fix loop reproduces from; ruff's per-rule findings are what a review reads). Where none was
    found, the Makefile serves the verb before package.json does, so a Go repository with
    `make test` gets a Test binding that reports an exit code, which is less than pytest gives and
    more than nothing. Build and run have no structured tool at all, so for them the Makefile is
    the first choice rather than the fallback.
    """
    out: list[tuple[type[Any], Any]] = []

    # First, because it is where the other bindings' tools come from: the environment
    # `uv sync --locked` or `npm ci` builds is the first place `tooling` looks (#185). Only from a
    # lockfile that exists; `_detect_facts` says what qualifies and what deliberately does not.
    if facts.provision_commands:
        out.append((Provision, CommandProvision(facts.provision_commands)))

    if facts.pytest:
        out.append((Test, PytestTest(args=["-q", "--no-header"])))
    elif facts.test_command:
        out.append((Test, CommandTest(facts.test_command)))

    if facts.ruff:
        out.append((Validate, RuffValidate()))
    elif facts.lint_command:
        out.append((Validate, CommandValidate(facts.lint_command)))

    # Only what is actually in the file: a `build` target or script that exists. An invented
    # `make build` is a binding that fails at run time.
    if facts.build_command:
        out.append((Build, CommandBuild(facts.build_command)))
    if facts.run_command:
        out.append((Run, CommandRun(facts.run_command)))

    return out
