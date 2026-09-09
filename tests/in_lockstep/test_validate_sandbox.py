"""GATE-SANDBOX-2 for Validate: a validator that EXECUTES is subject to the container rule.

The container rule existed for Test at four sites and for Build since #396. It was not asked of
Validate anywhere, because the two shipped adapters declared `READS_REPO` and nothing else, and a
validator that reads the tree does not need a container. But `CommandValidate` bound to a
repository's own target (`make lint`, `npm run lint`) EXECUTES whatever those recipes run, and
`mypy.ini`, `ruff.toml` and `eslint.config.js` are all writable by a model -- so a model can
author the file the command executes, on the host, with no image.

`CommandValidate` must declare that it executes by default, `RuffValidate` must declare that it
reads, and both call sites -- the strategy's check step (`_checked`) and the `run_validate` tool
(`_validate_runner`) -- must ask `staged_refusal(ctx, Validate)` before materialising anything.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from in_lockstep.adapters.command import CommandValidate
from in_lockstep.adapters.ruff_adapter import RuffValidate
from in_lockstep.core.outcome import Cost, Outcome, Status
from in_lockstep.core.types import (
    Validate,
    ValidationReport,
)
from in_lockstep.core.verbs import Capability

# -------------------------------------------------------------------------------------
# The declaration itself
# -------------------------------------------------------------------------------------


def test_command_validate_declares_executes_code_by_default() -> None:
    """GATE-SANDBOX-2. A repository's own `make lint` runs recipes, and a model can author the
    files those recipes read -- `mypy.ini`, `ruff.toml`, `eslint.config.js` -- so the command
    EXECUTES what the model wrote. Declaring `READS_REPO` alone was the false statement."""
    adapter = CommandValidate(["make", "lint"])
    assert Capability.EXECUTES_CODE in adapter.capabilities


def test_ruff_validate_declares_reads_repo_only() -> None:
    """Ruff is a binary that parses; its config is TOML rules rather than code, and a model-staged
    `ruff.toml` changes which rules run and nothing else. So `run_validate` does not regress for
    the repositories that bind it."""
    assert Capability.EXECUTES_CODE not in RuffValidate.capabilities
    assert Capability.READS_REPO in RuffValidate.capabilities


# -------------------------------------------------------------------------------------
# Falsification: without the declaration, the refusal would not fire
# -------------------------------------------------------------------------------------


def test_falsification_without_executes_code_the_refusal_does_not_fire() -> None:
    """The declaration is what `staged_refusal` reads. A `CommandValidate` whose capabilities
    field lacked `EXECUTES_CODE` would pass the refusal check, which is the hole this ticket
    closes."""
    from in_lockstep.adapters.sandbox import Sandbox
    from in_lockstep.adapters.worktree import staged_refusal

    # A CommandValidate whose sandbox names no image -- the runner the rule refuses.
    adapter = CommandValidate(["make", "lint"], sandbox=Sandbox())

    # With EXECUTES_CODE declared (the fix), staged_refusal must refuse.
    assert Capability.EXECUTES_CODE in adapter.capabilities

    class _Ctx:
        container: Any

    class _Container:
        @staticmethod
        def has(verb: type) -> bool:
            return verb is Validate

        @staticmethod
        def resolve(verb: type) -> Any:
            return adapter

    ctx = _Ctx()
    ctx.container = _Container()
    refusal = staged_refusal(ctx, Validate)
    assert refusal is not None, "a CommandValidate with no image MUST be refused"
    assert "container" in refusal.lower()


# -------------------------------------------------------------------------------------
# The strategy's check step (`_checked`) asks the question
# -------------------------------------------------------------------------------------


class _Validator:
    """A Validate adapter that records what it was asked."""

    def __init__(self, *, executes: bool = True) -> None:
        self.seen: list[Validate] = []
        self.fixes = False
        self.takes_paths = True
        # The adapter's declared capabilities.
        if executes:
            self.capabilities: frozenset[Capability] = frozenset(
                {Capability.EXECUTES_CODE, Capability.READS_REPO}
            )
        else:
            self.capabilities = frozenset({Capability.READS_REPO})

    async def invoke(self, ctx: Any, request: Validate) -> Outcome[ValidationReport]:
        self.seen.append(request)
        return Outcome(status=Status.SUCCEEDED, value=ValidationReport(), cost=Cost())


class _Ctx:
    """A container that answers for the verbs bound, and a `do` that dispatches by type."""

    def __init__(self, validator: Any = None) -> None:
        self.bound: dict[Any, Any] = {}
        if validator is not None:
            self.bound[Validate] = validator
        ctx = self

        class _Container:
            @staticmethod
            def has(verb: Any) -> bool:
                return verb in ctx.bound

            @staticmethod
            def resolve(verb: Any) -> Any:
                return ctx.bound[verb]

        self.container = _Container()
        self.repo = type("R", (), {"root": "."})()

    async def do(self, request: Any) -> Any:
        return await self.bound[type(request)].invoke(self, request)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=root, check=True)
    (root / "seed.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    return root


class _Session:
    def __init__(self, root: Path) -> None:
        from in_lockstep.ai.builtins import Workspace

        self.repo_root = str(root)
        self.workspace = Workspace(root=root)


def test_gate_sandbox_2_checked_refuses_a_validator_that_executes_without_a_container(
    tmp_path: Path,
) -> None:
    """GATE-SANDBOX-2. The strategy's check step (`_checked`) must ask `staged_refusal(ctx,
    Validate)` before materialising. A `CommandValidate` with no container image is refused."""
    from in_lockstep.adapters.ai.strategy import _checked
    from in_lockstep.adapters.sandbox import Sandbox

    root = _repo(tmp_path)
    session = _Session(root)
    session.workspace.record("a.py", "import os\n")
    changeset = session.workspace.changeset()

    # A validator that executes, with no container image.
    validator = _Validator(executes=True)
    validator.sandbox = Sandbox()  # type: ignore[attr-defined]
    ctx = _Ctx(validator)

    report, said, why = asyncio.run(_checked(ctx, session, changeset, ("a.py",)))

    assert validator.seen == [], "nothing was materialised or run"
    assert why and "container" in why.lower(), f"the refusal must name the container: {why!r}"


def test_gate_sandbox_2_checked_allows_a_validator_that_reads(tmp_path: Path) -> None:
    """A binding that reads -- like `RuffValidate` -- is unaffected by the container rule."""
    from in_lockstep.adapters.ai.strategy import _checked

    root = _repo(tmp_path)
    session = _Session(root)
    session.workspace.record("a.py", "import os\n")
    changeset = session.workspace.changeset()

    # A validator that only reads.
    validator = _Validator(executes=False)
    ctx = _Ctx(validator)

    report, said, why = asyncio.run(_checked(ctx, session, changeset, ("a.py",)))

    assert len(validator.seen) == 1, "the validator ran"
    assert not why, f"no refusal expected, got: {why!r}"


# -------------------------------------------------------------------------------------
# The `run_validate` tool (`_validate_runner`) asks the question
# -------------------------------------------------------------------------------------


def test_gate_sandbox_2_validate_runner_refuses_a_validator_that_executes_without_container(
    tmp_path: Path,
) -> None:
    """GATE-SANDBOX-2. The `run_validate` tool must ask `staged_refusal(ctx, Validate)` before
    materialising. A `CommandValidate` with no container image is refused."""
    from in_lockstep.adapters.ai.strategy import _validate_runner
    from in_lockstep.adapters.sandbox import Sandbox
    from in_lockstep.ai.builtins import Workspace

    root = _repo(tmp_path)
    workspace = Workspace(root=root)
    workspace.record("a.py", "import os\n")

    validator = _Validator(executes=True)
    validator.sandbox = Sandbox()  # type: ignore[attr-defined]
    ctx = _Ctx(validator)

    run = _validate_runner(ctx, str(root), workspace)
    result = asyncio.run(run())

    assert validator.seen == [], "nothing was materialised or run"
    assert "refused" in result.lower() or "sandbox" in result.lower(), f"unexpected result: {result}"
    assert "container" in result.lower(), f"the refusal must name the container: {result}"


def test_gate_sandbox_2_validate_runner_allows_a_validator_that_reads(tmp_path: Path) -> None:
    """A binding that reads is unaffected: `run_validate` must not regress for `RuffValidate`."""
    from in_lockstep.adapters.ai.strategy import _validate_runner
    from in_lockstep.ai.builtins import Workspace

    root = _repo(tmp_path)
    workspace = Workspace(root=root)
    workspace.record("a.py", "import os\n")

    validator = _Validator(executes=False)
    ctx = _Ctx(validator)

    run = _validate_runner(ctx, str(root), workspace)
    result = asyncio.run(run())

    assert len(validator.seen) == 1, "the validator should have run"
    assert "clean" in result.lower(), f"expected clean result, got: {result}"


# -------------------------------------------------------------------------------------
# Detection carries the declaration
# -------------------------------------------------------------------------------------


def test_detection_make_lint_binds_a_validator_that_executes(tmp_path: Path) -> None:
    """Detection's `make …` and `npm run …` bindings must carry `EXECUTES_CODE`, because the
    model authors the files those commands execute."""
    from in_lockstep.adapters.detected import detected_bindings
    from in_lockstep.lockstep import _detect_facts

    (tmp_path / "Makefile").write_text("lint:\n\truff check\n")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    facts = _detect_facts(tmp_path)
    assert facts.lint_command  # make lint detected
    bindings = dict(detected_bindings(facts))
    adapter = bindings[Validate]
    assert Capability.EXECUTES_CODE in adapter.capabilities


def test_detection_ruff_binds_a_validator_that_reads(tmp_path: Path) -> None:
    """A `RuffValidate` binding from detection declares `READS_REPO` only."""
    from in_lockstep.adapters.detected import detected_bindings
    from in_lockstep.lockstep import _detect_facts

    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n")
    facts = _detect_facts(tmp_path)
    assert facts.ruff
    bindings = dict(detected_bindings(facts))
    adapter = bindings[Validate]
    assert Capability.EXECUTES_CODE not in adapter.capabilities
    assert Capability.READS_REPO in adapter.capabilities


def test_gate_sandbox_2_a_container_it_cannot_inspect_is_not_silently_cleared() -> None:
    """A security control's unknown case is loud, not permissive.

    `staged_refusal` reached `container.resolve` through a `getattr(..., None)` with an early
    `return None`, added so a test double implementing only `has` would not raise. That turns a
    crash into a silent pass: a container this code cannot inspect is one it cannot clear, and
    returning "no refusal" says it cleared it. The real `Container` always resolves, and a double
    that cannot has not modelled the thing under test -- so the honest answer to an uninspectable
    container is to fail where somebody sees it (#410).
    """
    import pytest

    from in_lockstep.adapters.worktree import staged_refusal

    class _OnlyHas:
        @staticmethod
        def has(verb: type) -> bool:
            return True

    class _Ctx:
        container: Any

    ctx = _Ctx()
    ctx.container = _OnlyHas()

    with pytest.raises(AttributeError):
        staged_refusal(ctx, Validate)
