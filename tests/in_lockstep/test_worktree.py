"""The materialise primitive: run a staged change without touching the real tree.

This is goal 5's missing foundation — "nothing can test a staged ChangeSet". The tests use real
git so the worktree mechanics are exercised, not mocked, and one runs real pytest to show the whole
point: a staged change becomes a red-or-green verdict.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from in_lockstep.adapters.pytest_adapter import PytestTest
from in_lockstep.adapters.worktree import (
    WorktreeError,
    WorktreeRunner,
    head_state,
    materialize,
    verdict_over_staged,
)
from in_lockstep.core.outcome import Status
from in_lockstep.core.types import ChangeSet, FileChange, Test, TestReport, TestVerdict


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()

    def run(*args: str) -> None:
        subprocess.run(args, cwd=root, capture_output=True, check=True)

    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.test")
    run("git", "config", "user.name", "t")
    (root / "app.py").write_text("VALUE = 1\n")
    (root / "keep.py").write_text("# stays\n")
    run("git", "add", "-A")
    run("git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "base")
    run("git", "branch", "-M", "main")
    return root


def _worktrees(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=root, capture_output=True, text=True
    ).stdout
    return [line[len("worktree ") :] for line in out.splitlines() if line.startswith("worktree ")]


async def _materialize_paths(root: Path, changeset: ChangeSet) -> dict[str, str | None]:
    """The materialised tree's view of a few paths, captured inside the context."""
    captured: dict[str, str | None] = {}
    async with materialize(str(root), changeset) as tree:
        for name in ("app.py", "keep.py", "new.py"):
            p = Path(tree) / name
            captured[name] = p.read_text() if p.exists() else None
        captured["_tree"] = tree
    return captured


def test_materialize_applies_the_change_and_leaves_the_real_tree_untouched(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    changeset = ChangeSet(
        changes=(
            FileChange(path="app.py", contents="VALUE = 2\n"),
            FileChange(path="new.py", contents="added = True\n"),
        )
    )
    captured = asyncio.run(_materialize_paths(root, changeset))

    # The worktree shows HEAD plus the change.
    assert captured["app.py"] == "VALUE = 2\n"
    assert captured["new.py"] == "added = True\n"
    assert captured["keep.py"] == "# stays\n"  # untouched files come from HEAD

    # The real working tree is exactly as it was: the whole reason for a throwaway worktree.
    assert (root / "app.py").read_text() == "VALUE = 1\n"
    assert not (root / "new.py").exists()


def test_materialize_handles_deletes_and_symlinks(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    changeset = ChangeSet(
        changes=(
            FileChange(path="keep.py", contents=None),  # a deletion
            FileChange(path="link.py", symlink_target="app.py"),
        )
    )

    async def check() -> None:
        async with materialize(str(root), changeset) as tree:
            assert not (Path(tree) / "keep.py").exists()
            link = Path(tree) / "link.py"
            assert link.is_symlink()
            assert link.readlink() == Path("app.py")

    asyncio.run(check())
    # The real tree still has keep.py.
    assert (root / "keep.py").exists()


def test_materialize_applies_an_executable_mode(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    changeset = ChangeSet(changes=(FileChange(path="run.sh", contents="#!/bin/sh\n", mode=0o755),))

    async def check() -> None:
        async with materialize(str(root), changeset) as tree:
            assert (Path(tree) / "run.sh").stat().st_mode & 0o111  # executable bits set

    asyncio.run(check())


def test_materialize_strips_setuid_and_setgid_bits(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    changeset = ChangeSet(changes=(FileChange(path="run.sh", contents="#!/bin/sh\n", mode=0o6755),))

    async def check() -> None:
        async with materialize(str(root), changeset) as tree:
            mode = (Path(tree) / "run.sh").stat().st_mode
            assert mode & 0o111, "the execute bit a test run needs is kept"
            assert not mode & 0o6000, "setuid/setgid are masked off"

    asyncio.run(check())


def test_materialize_removes_the_worktree_afterwards(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    seen: list[str] = []

    async def run() -> None:
        async with materialize(str(root), ChangeSet()) as tree:
            seen.append(tree)
            assert Path(tree).exists()

    asyncio.run(run())
    assert not Path(seen[0]).exists(), "the worktree directory should be gone"
    assert seen[0] not in _worktrees(root), "git should no longer list the worktree"
    assert _worktrees(root) == [str(root)], "only the main worktree remains"


def test_materialize_refuses_a_source_that_is_not_a_git_repo(tmp_path: Path) -> None:
    plain = tmp_path / "not-git"
    plain.mkdir()

    async def run() -> None:
        async with materialize(str(plain), ChangeSet()):
            pass

    with pytest.raises(WorktreeError, match="git repository"):
        asyncio.run(run())


def test_materialize_refuses_a_path_that_escapes_the_worktree(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    changeset = ChangeSet(changes=(FileChange(path="../escape.py", contents="x\n"),))

    async def run() -> None:
        async with materialize(str(root), changeset):
            pass

    with pytest.raises(WorktreeError, match="escapes the worktree"):
        asyncio.run(run())
    assert not (tmp_path / "escape.py").exists()


@pytest.mark.parametrize("escaping", ["/etc/passwd", "../../secret", "../outside.py"])
def test_materialize_refuses_a_symlink_that_points_outside_the_worktree(
    tmp_path: Path, escaping: str
) -> None:
    """A symlink is a write that lands where it points; one aimed out of the tree is the out-of-root
    write ChangeGuard exists to stop, and materialisation is a second write point that must too."""
    root = _repo(tmp_path)
    changeset = ChangeSet(changes=(FileChange(path="link", symlink_target=escaping),))

    async def run() -> None:
        async with materialize(str(root), changeset):
            pass

    with pytest.raises(WorktreeError, match="points outside the worktree"):
        asyncio.run(run())


def test_materialize_allows_a_symlink_that_stays_inside(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    changeset = ChangeSet(changes=(FileChange(path="pkg/link.py", symlink_target="../app.py"),))

    async def run() -> None:
        async with materialize(str(root), changeset) as tree:
            link = Path(tree) / "pkg" / "link.py"
            assert link.is_symlink()

    asyncio.run(run())


def test_materialize_refuses_an_escape_through_an_earlier_symlink(tmp_path: Path) -> None:
    """First plant an in-tree symlink to the tree root, then aim a second link through it and out.
    The lexical pre-check could miss it; the post-creation realpath check must not."""
    root = _repo(tmp_path)
    changeset = ChangeSet(
        changes=(
            FileChange(path="hop", symlink_target="."),  # in-tree: the worktree root itself
            FileChange(path="escape", symlink_target="hop/../../outside"),  # out via `hop`
        )
    )

    async def run() -> None:
        async with materialize(str(root), changeset):
            pass

    with pytest.raises(WorktreeError, match="outside the worktree"):
        asyncio.run(run())


def test_materialize_refuses_a_ref_that_looks_like_an_option(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    async def run() -> None:
        async with materialize(str(root), ChangeSet(), ref="--lock"):
            pass

    with pytest.raises(WorktreeError, match="looks like an option"):
        asyncio.run(run())


def test_a_staged_change_becomes_a_red_or_green_verdict(tmp_path: Path) -> None:
    """The goal-5 loop, end to end: a change the model has not committed is run and judged.

    A change that stages a failing test comes back red; one that stages a passing test comes back
    green — and the real working tree never sees either test.
    """
    root = _repo(tmp_path)
    adapter = PytestTest(args=["-q"])
    ctx = type("C", (), {"repo": type("R", (), {"root": str(root)})})()

    failing = ChangeSet(
        changes=(FileChange(path="test_staged.py", contents="def test_it():\n    assert False\n"),)
    )
    passing = ChangeSet(
        changes=(FileChange(path="test_staged.py", contents="def test_it():\n    assert True\n"),)
    )

    async def verdict(changeset: ChangeSet) -> str:
        async with materialize(str(root), changeset) as tree:
            outcome = await adapter.invoke(ctx, Test(root=tree))
            return outcome.status.value

    assert asyncio.run(verdict(failing)) == "failed"
    assert asyncio.run(verdict(passing)) == "succeeded"
    assert not (root / "test_staged.py").exists(), "the staged test never touched the real tree"


class _Ctx:
    """A ctx whose Test verb is a real PytestTest, so `verdict_over_staged` is exercised end to end
    against a materialised worktree rather than a stub."""

    def __init__(self, root: Path, *, test_bound: bool = True) -> None:
        self.repo = type("R", (), {"root": str(root)})()

        class _Container:
            def has(self, _verb: object) -> bool:
                return test_bound

        self.container = _Container()

    async def do(self, request: Test):  # noqa: ANN202
        return await PytestTest(args=["-q"]).invoke(self, request)


def test_verdict_over_staged_reports_green_for_a_passing_staged_change(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    changeset = ChangeSet(
        changes=(FileChange(path="test_staged.py", contents="def test_it():\n    assert True\n"),)
    )
    verdict = asyncio.run(verdict_over_staged(_Ctx(root), str(root), changeset))
    assert verdict is not None
    assert verdict.green
    assert not (root / "test_staged.py").exists()


def test_verdict_over_staged_reports_red_for_a_failing_staged_change(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    changeset = ChangeSet(
        changes=(FileChange(path="test_staged.py", contents="def test_it():\n    assert False\n"),)
    )
    verdict = asyncio.run(verdict_over_staged(_Ctx(root), str(root), changeset))
    assert verdict is not None
    assert not verdict.green
    assert verdict.failed >= 1


def test_verdict_over_staged_is_none_when_no_test_is_bound(tmp_path: Path) -> None:
    """An honest 'unverified' rather than a silent green."""
    root = _repo(tmp_path)
    changeset = ChangeSet(changes=(FileChange(path="x.py", contents="y = 1\n"),))
    verdict = asyncio.run(verdict_over_staged(_Ctx(root, test_bound=False), str(root), changeset))
    assert verdict is None


# -- WorktreeRunner: run_script cannot write the real tree --------------------------------------


class _WritingInner:
    """Stands in for the Sandbox: records where it ran, reads a HEAD file to prove the tree is
    there, and writes into that tree the way a model's command would. The test's whole point is
    that 'that tree' is a discarded copy, not the real repository."""

    def __init__(self) -> None:
        self.cwd: str | None = None
        self.head_view: str | None = None

    async def run(self, command, *, cwd=None, timeout=900.0):  # noqa: ANN001, ANN202
        self.cwd = cwd
        tree = Path(cwd)
        self.head_view = (tree / "app.py").read_text()  # the copy carries HEAD
        (tree / "scratch.txt").write_text("junk")  # an ordinary in-tree write
        (tree / ".lockstep").mkdir(exist_ok=True)
        (tree / ".lockstep" / "lockstep.py").write_text("PWNED\n")  # the attack this closes
        return type("R", (), {"exit_code": 0, "stdout": "", "stderr": "", "how": "fake"})()


def test_worktree_runner_isolates_a_commands_writes_from_the_real_tree(tmp_path: Path) -> None:
    root = _repo(tmp_path)  # app.py, keep.py — no .lockstep
    inner = _WritingInner()

    result = asyncio.run(WorktreeRunner(inner, str(root)).run(["pytest"]))
    assert result.exit_code == 0

    # It ran in a copy of HEAD, not the live tree.
    assert inner.cwd is not None and inner.cwd != str(root)
    assert inner.head_view == "VALUE = 1\n"
    # None of the command's writes reached the real repository — this is the hole item 14 closes.
    assert not (root / "scratch.txt").exists()
    assert not (root / ".lockstep").exists()
    assert (root / "app.py").read_text() == "VALUE = 1\n"
    # And the copy is gone.
    assert not Path(inner.cwd).exists()


def test_worktree_runner_needs_a_git_repository(tmp_path: Path) -> None:
    plain = tmp_path / "not-git"
    plain.mkdir()
    with pytest.raises(WorktreeError, match="git repository"):
        asyncio.run(WorktreeRunner(_WritingInner(), str(plain)).run(["pytest"]))


# -- the revert primitive: ChangeSet.inverse + head_state ---------------------------------------


def test_inverse_of_a_create_is_a_delete() -> None:
    inv = ChangeSet(changes=(FileChange(path="new.py", contents="x\n"),)).inverse({"new.py": None})
    assert inv.changes[0].path == "new.py"
    assert inv.changes[0].deleted


def test_inverse_of_a_modification_restores_the_prior_contents() -> None:
    change = ChangeSet(changes=(FileChange(path="a.py", contents="new\n"),))
    inv = change.inverse({"a.py": FileChange(path="a.py", contents="old\n")})
    assert inv.changes[0].contents == "old\n"


def test_inverse_of_a_deletion_recreates_the_file() -> None:
    change = ChangeSet(changes=(FileChange(path="gone.py", contents=None),))
    inv = change.inverse({"gone.py": FileChange(path="gone.py", contents="was here\n")})
    assert inv.changes[0].contents == "was here\n"


def test_head_state_reads_committed_contents_and_absence(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    state = asyncio.run(head_state(str(root), ["app.py", "absent.py"]))
    assert state["app.py"] is not None
    assert state["app.py"].contents == "VALUE = 1\n"
    assert state["absent.py"] is None


def test_head_state_refuses_a_ref_that_looks_like_an_option(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    with pytest.raises(WorktreeError, match="looks like an option"):
        asyncio.run(head_state(str(root), ["app.py"], ref="--output=/tmp/x"))


def test_inverse_read_from_head_round_trips_a_change_back(tmp_path: Path) -> None:
    """A change plus its inverse (pre-image read from HEAD) materialises back to HEAD."""
    root = _repo(tmp_path)
    change = ChangeSet(
        changes=(
            FileChange(path="app.py", contents="VALUE = 2\n"),  # a modification
            FileChange(path="new.py", contents="added\n"),  # a creation
        )
    )
    before = asyncio.run(head_state(str(root), list(change.paths())))
    undo = change.inverse(before)

    async def check() -> None:
        # Apply the change then its inverse, in order: the net tree is HEAD again.
        async with materialize(str(root), ChangeSet(changes=(*change.changes, *undo.changes))) as tree:
            assert (Path(tree) / "app.py").read_text() == "VALUE = 1\n"  # modification undone
            assert not (Path(tree) / "new.py").exists()  # creation undone

    asyncio.run(check())


class _CwdRecordingSandbox:
    def __init__(self) -> None:
        self.cwd: str | None = None

    async def run(self, command, *, cwd=None, timeout=900.0):  # noqa: ANN001
        self.cwd = cwd
        return type("R", (), {"exit_code": 0, "stdout": "", "stderr": ""})()


def test_test_spec_root_points_the_suite_at_the_worktree(tmp_path: Path) -> None:
    """`Test.root` wins over the adapter's bound cwd, which wins over the repo root."""
    sandbox = _CwdRecordingSandbox()
    adapter = PytestTest(cwd="/bound", sandbox=sandbox)
    ctx = type("C", (), {"repo": type("R", (), {"root": "/repo"})})()

    asyncio.run(adapter.invoke(ctx, Test(root="/materialized")))
    assert sandbox.cwd == "/materialized"

    asyncio.run(adapter.invoke(ctx, Test()))
    assert sandbox.cwd == "/bound", "with no root, the bound cwd stands"


class _CommandRecordingSandbox(_CwdRecordingSandbox):
    """Records argv as well as cwd, so a test can assert which interpreter was invoked."""

    def __init__(self) -> None:
        super().__init__()
        self.command: list[str] = []

    async def run(self, command, *, cwd=None, timeout=900.0):  # noqa: ANN001
        self.command = list(command)
        return await super().run(command, cwd=cwd, timeout=timeout)


def _ctx(root: Path) -> object:
    return type("C", (), {"repo": type("R", (), {"root": str(root)})})()


def _executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def test_gate_tooling_1_the_repositorys_venv_runs_the_suite_not_the_interpreter_running_us(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    """GATE-TOOLING-1. `sys.executable` was the rule, on the reasoning that the interpreter running
    this process is the one set up for the repository. Under `uv tool install` it is the tool's
    own isolated interpreter, with no pytest in it, and two first-time users got a red suite for
    a repository whose `.venv` had one (#167). The repository's environment comes first, and the
    materialized worktree (`Test.root`) is where the suite runs, not where the venv is looked for.
    """
    import in_lockstep.adapters.tooling as tooling

    venv_python = _executable(tmp_path / ".venv" / "bin" / "python")
    monkeypatch.setattr(tooling.shutil, "which", lambda name: "/opt/homebrew/bin/python3")
    sandbox = _CommandRecordingSandbox()

    outcome = asyncio.run(PytestTest(sandbox=sandbox).invoke(_ctx(tmp_path), Test(root="/materialized")))

    assert outcome.status is not Status.ERRORED
    assert sandbox.command[0] == str(venv_python)
    assert sandbox.cwd == "/materialized"


def test_this_process_runs_the_suite_only_when_it_lives_inside_the_repository(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    """`uv run` in the checkout, tox, a virtualenv under the tree: the process is the repository's
    (GATE-TOOLING-1). An installed tool's interpreter is not, and PATH is the fallback then."""
    import in_lockstep.adapters.tooling as tooling

    inside = _executable(tmp_path / ".tox" / "py" / "bin" / "python")
    monkeypatch.setattr(tooling.sys, "executable", str(inside))
    monkeypatch.setattr(
        tooling.shutil, "which", lambda name: "/usr/bin/python3" if name == "python3" else None
    )
    sandbox = _CommandRecordingSandbox()
    asyncio.run(PytestTest(sandbox=sandbox).invoke(_ctx(tmp_path), Test()))
    assert sandbox.command[0] == str(inside)

    monkeypatch.setattr(tooling.sys, "executable", "/opt/tool/bin/python")
    asyncio.run(PytestTest(sandbox=sandbox).invoke(_ctx(tmp_path), Test()))
    assert sandbox.command[0] == "/usr/bin/python3", "outside the repository, PATH is what is left"


def test_nothing_found_is_a_refusal_that_names_every_place_it_looked(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """Absent is not guessed, and it is not a red suite either (GATE-TOOLING-1)."""
    import in_lockstep.adapters.tooling as tooling

    monkeypatch.setattr(tooling.sys, "executable", "")
    monkeypatch.setattr(tooling.shutil, "which", lambda name: None)
    sandbox = _CommandRecordingSandbox()

    outcome = asyncio.run(PytestTest(sandbox=sandbox).invoke(_ctx(tmp_path), Test()))

    assert outcome.status is Status.ERRORED
    assert ".venv/bin/python" in outcome.reason and "python3 on PATH" in outcome.reason
    assert sandbox.command == [], "nothing ran"


def test_this_process_is_the_last_resort_when_path_has_no_python_at_all(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """Better than nothing only when PATH has nothing: the tool's own interpreter, named as such."""
    import in_lockstep.adapters.tooling as tooling

    monkeypatch.setattr(tooling.sys, "executable", "/opt/tool/bin/python")
    monkeypatch.setattr(tooling.shutil, "which", lambda name: None)
    (found,) = PytestTest().locations(str(tmp_path))
    assert found.path == "/opt/tool/bin/python"
    assert "PATH has no python" in found.how
    assert any("this process, outside the repository" in t for t in found.tried)


def test_a_symlinked_venv_interpreter_inside_the_repository_counts_as_inside(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    """A venv's python is a symlink to the base interpreter outside the tree. Resolving it said
    every venv lived outside the repository and the branch never fired for a real one; the
    interpreter's directory is what places it (GATE-TOOLING-1)."""
    import in_lockstep.adapters.tooling as tooling

    link = tmp_path / "venv" / "bin" / "python"
    link.parent.mkdir(parents=True)
    link.symlink_to(sys.executable)
    monkeypatch.setattr(tooling.sys, "executable", str(link))
    monkeypatch.setattr(tooling.shutil, "which", lambda name: "/usr/bin/python3")
    sandbox = _CommandRecordingSandbox()

    asyncio.run(PytestTest(sandbox=sandbox).invoke(_ctx(tmp_path), Test()))
    assert sandbox.command[0] == str(link)


def test_pytest_missing_from_the_resolved_interpreter_is_an_error_not_a_red_suite(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    """The interpreter exists and has no pytest. Reading `No module named pytest` as a failed
    suite blamed the change for the environment (GATE-TOOLING-1)."""
    import in_lockstep.adapters.tooling as tooling

    venv_python = _executable(tmp_path / ".venv" / "bin" / "python")

    class _NoPytest(_CommandRecordingSandbox):
        async def run(self, command, *, cwd=None, timeout=900.0):  # noqa: ANN001
            await super().run(command, cwd=cwd, timeout=timeout)
            return type(
                "R", (), {"exit_code": 1, "stdout": "", "stderr": "python: No module named pytest\n"}
            )()

    monkeypatch.setattr(tooling.shutil, "which", lambda name: None)
    outcome = asyncio.run(PytestTest(sandbox=_NoPytest()).invoke(_ctx(tmp_path), Test()))
    assert outcome.status is Status.ERRORED
    assert outcome.reason == f"pytest is not installed in {venv_python} (the repository's .venv)"


def test_a_containerized_run_does_not_probe_this_host(monkeypatch) -> None:  # noqa: ANN001
    """The image resolves the name, so what is or is not on THIS host says nothing about it."""
    import in_lockstep.adapters.pytest_adapter as adapter_mod

    monkeypatch.setattr(adapter_mod.shutil, "which", lambda name: None)
    sandbox = _CommandRecordingSandbox()
    sandbox.image = "docker.io/library/python:3.12-slim"
    sandbox.runtime = lambda: "/usr/bin/docker"

    asyncio.run(PytestTest(sandbox=sandbox).invoke(object(), Test(root="/materialized")))

    assert sandbox.command[0] == "python", "the plain name travels into the container"


def test_an_errored_suite_is_neither_green_nor_red() -> None:
    """The distinction the propose step turns on.

    Escalation asks `verdict.red`, not `not verdict.green`, because those differ exactly here: an
    errored run is decided in the sense that it produced a status and green in no sense, but it
    learned nothing about the change. Treating it as red files an `ai-generated` bug against code
    that was never tested, and then spends the loop's attempts on it.
    """
    errored = TestVerdict.of("errored", True, TestReport())
    assert errored.green is False
    assert errored.red is False

    really_red = TestVerdict.of("failed", True, TestReport(total=9, passed=7, failed=2))
    assert really_red.red is True

    undecided = TestVerdict.of("succeeded", False, TestReport())
    assert undecided.red is False and undecided.green is False
