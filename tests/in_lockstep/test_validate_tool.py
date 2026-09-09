"""GATE-VALIDATE-1: a session can check what it staged, not only test it.

The asymmetry this closes cost a run. `run_tests` materialises the staged change and runs the
suite over it; nothing did the same for the Validate verb, and `run_script` runs in a throwaway
worktree of HEAD by design -- so a model that linted its own work was told about the code as it
was before its edit. Run 34294139197 opened a pull request whose suite passed and whose lint
failed on four errors a validator would have named in a second.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from in_lockstep.adapters.ai.strategy import _validate_runner, _validation
from in_lockstep.ai.builtins import Workspace, read_write_execute
from in_lockstep.core.outcome import Outcome, Status
from in_lockstep.core.types import ChangeSet, Validate, ValidationFinding, ValidationReport
from in_lockstep.core.verbs import Capability


def _call(runner: Any, **args: Any) -> str:
    return str(asyncio.run(runner("builtin", "run_validate", args)))


class _Ctx:
    """The two things the runner reaches for: a container that answers `has`, and `do`."""

    def __init__(self, outcome: Outcome[Any] | None, *, bound: bool = True) -> None:
        self.outcome = outcome
        self.bound = bound
        self.seen: list[Validate] = []
        ctx = self

        class _Container:
            @staticmethod
            def has(verb: type) -> bool:
                return ctx.bound

        self.container = _Container()
        self.repo = type("R", (), {"root": "."})()

    async def do(self, request: Validate) -> Any:
        self.seen.append(request)
        return self.outcome


def _clean() -> Outcome[ValidationReport]:
    return Outcome(status=Status.SUCCEEDED, value=ValidationReport())


def _dirty() -> Outcome[ValidationReport]:
    return Outcome(
        status=Status.FAILED,
        value=ValidationReport(
            findings=(
                ValidationFinding(rule="F401", message="`pytest` imported but unused", path="t.py", line=19),
                ValidationFinding(rule="I001", message="Import block is un-sorted", path="t.py", line=152),
            )
        ),
    )


# -- the tool -----------------------------------------------------------------------------------


def test_gate_validate_1_the_tool_is_declared_read_only_beside_run_tests(tmp_path: Path) -> None:
    """GATE-VALIDATE-1, declared. `READS_REPO` and nothing else: a validator reads the tree where
    a suite executes it, which is why this needs no container and `run_tests` does."""
    tools, runner = read_write_execute(Workspace(root=tmp_path))
    assert "run_validate" in tools.names()
    assert tools.resolve("run_validate").capabilities == frozenset({Capability.READS_REPO})
    described = tools.resolve("run_validate").description
    assert "HEAD" in described, "the description has to say why run_script cannot do this"


def test_gate_validate_1_with_no_validate_bound_the_tool_refuses_and_says_what_to_bind(
    tmp_path: Path,
) -> None:
    """Declared and refusing, the shape `run_tests` and `run_script` already use: a set that could
    validate on some other configuration must not read as harmless on this one."""
    _, runner = read_write_execute(Workspace(root=tmp_path))
    answer = _call(runner)
    assert answer.startswith("refused: no Validate verb is bound")
    assert ".lockstep/lockstep.py" in answer


def test_the_tool_refuses_an_option_shaped_path_and_never_crashes(tmp_path: Path) -> None:
    async def boom(paths: tuple[str, ...] = ()) -> str:
        raise RuntimeError("the validator exploded")

    _, runner = read_write_execute(Workspace(root=tmp_path), validates=boom)
    assert _call(runner, paths=["--fix"]).startswith("refused: '--fix' looks like an option")
    assert _call(runner) == "error: could not run the validator: the validator exploded"


# -- what it runs over ----------------------------------------------------------------------------


def test_gate_validate_1_it_runs_over_a_worktree_of_the_staged_change_not_head(tmp_path: Path) -> None:
    """GATE-VALIDATE-1, the whole point. The tree handed to the verb is a materialised copy
    holding the session's writes -- not the repository, whose files are untouched."""
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=root, check=True)
    (root / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "init"], cwd=root, check=True
    )

    workspace = Workspace(root=root)
    ctx = _Ctx(_clean())
    _, runner = read_write_execute(workspace, validates=_validate_runner(ctx, str(root), workspace))
    asyncio.run(runner("builtin", "write_file", {"path": "b.py", "contents": "import os\n"}))
    assert _call(runner) == "clean: the validator found nothing."

    (request,) = ctx.seen
    assert request.root and request.root != str(root), "the verb was pointed at the repository"
    assert not Path(request.root).exists(), "the worktree is thrown away"
    assert not (root / "b.py").exists(), "the staged write never reached the disk"


def test_gate_validate_1_with_nothing_staged_it_refuses_rather_than_checking_head(tmp_path: Path) -> None:
    """The refusal that names the confusion. Checking HEAD is what `run_script` already does, and
    a clean answer about code the session has not changed is worse than no answer."""
    workspace = Workspace(root=tmp_path)
    ctx = _Ctx(_clean())
    _, runner = read_write_execute(workspace, validates=_validate_runner(ctx, str(tmp_path), workspace))
    assert _call(runner).startswith("refused: nothing is staged yet")
    assert ctx.seen == [], "a refusal ran no validator"


def test_a_run_with_no_validate_bound_refuses_before_it_materialises_anything(tmp_path: Path) -> None:
    workspace = Workspace(root=tmp_path)
    ctx = _Ctx(_clean(), bound=False)
    run = _validate_runner(ctx, str(tmp_path), workspace)
    assert asyncio.run(run()).startswith("refused: no Validate verb is bound")


# -- what it says --------------------------------------------------------------------------------


def test_gate_validate_1_the_findings_are_named_not_counted() -> None:
    """GATE-VALIDATE-1, what it says. The rule, the place and the message, for the reason
    GATE-VERDICT-2 gives about a red suite: a verdict that says a check failed and not which one
    sends the session back to run it again to learn what the run already knew."""
    said = _validation(_dirty())
    assert "2 finding(s)" in said
    assert "t.py:19: F401 `pytest` imported but unused" in said
    assert "t.py:152: I001 Import block is un-sorted" in said


def test_a_validator_that_did_not_report_says_so_rather_than_reading_as_clean() -> None:
    """`errored` with no findings is not a clean tree. Reporting it as clean would be the
    reassuring number this repository refuses everywhere else."""
    said = _validation(Outcome(status=Status.ERRORED, reason="ruff is not installed"))
    assert said == "the validator did not report: ruff is not installed"
    assert _validation(_clean()) == "clean: the validator found nothing."


# -- the verb takes a tree, like Test ------------------------------------------------------------


def test_gate_validate_1_both_shipped_validators_check_the_tree_they_are_given(tmp_path: Path) -> None:
    """`Validate` gained the `root` field `Test` already had, and both adapters honour it -- or
    the worktree above would be built and then ignored, and the check would be of HEAD again."""
    from in_lockstep.adapters.command import CommandValidate
    from in_lockstep.adapters.ruff_adapter import RuffValidate

    class _Recorder:
        def __init__(self) -> None:
            self.cwd: str | None = None
            self.command: list[str] = []

        async def run(self, command: list[str], *, cwd: str | None = None, timeout: float = 900.0) -> Any:
            self.cwd, self.command = cwd, list(command)
            return type("R", (), {"exit_code": 0, "stdout": "[]", "stderr": "", "how": "fake"})()

    tree = tmp_path / "worktree"
    tree.mkdir()
    ctx = type("Ctx", (), {"repo": type("R", (), {"root": str(tmp_path)})()})()

    generic = _Recorder()
    asyncio.run(CommandValidate(["eslint"], sandbox=generic).invoke(ctx, Validate(root=str(tree))))
    assert generic.cwd == str(tree)

    ruff = _Recorder()
    outcome = asyncio.run(RuffValidate(sandbox=ruff).invoke(ctx, Validate(root=str(tree))))
    assert ruff.cwd == str(tree), "ruff checked the repository instead of the staged tree"
    assert outcome.status is Status.SUCCEEDED


def test_a_changeset_with_no_changes_is_what_the_refusal_is_about() -> None:
    """The seam reads the workspace at call time, so an empty set is the honest empty."""
    assert ChangeSet().changes == ()
