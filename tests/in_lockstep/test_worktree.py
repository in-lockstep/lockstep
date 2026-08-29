"""The materialise primitive: run a staged change without touching the real tree.

This is goal 5's missing foundation — "nothing can test a staged ChangeSet". The tests use real
git so the worktree mechanics are exercised, not mocked, and one runs real pytest to show the whole
point: a staged change becomes a red-or-green verdict.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from in_lockstep.adapters.pytest_adapter import PytestTest
from in_lockstep.adapters.worktree import WorktreeError, materialize
from in_lockstep.core.types import ChangeSet, FileChange, TestSpec


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
            outcome = await adapter.invoke(ctx, TestSpec(root=tree))
            return outcome.status.value

    assert asyncio.run(verdict(failing)) == "failed"
    assert asyncio.run(verdict(passing)) == "succeeded"
    assert not (root / "test_staged.py").exists(), "the staged test never touched the real tree"


class _CwdRecordingSandbox:
    def __init__(self) -> None:
        self.cwd: str | None = None

    async def run(self, command, *, cwd=None, timeout=900.0):  # noqa: ANN001
        self.cwd = cwd
        return type("R", (), {"exit_code": 0, "stdout": "", "stderr": ""})()


def test_test_spec_root_points_the_suite_at_the_worktree(tmp_path: Path) -> None:
    """`TestSpec.root` wins over the adapter's bound cwd, which wins over the repo root."""
    sandbox = _CwdRecordingSandbox()
    adapter = PytestTest(cwd="/bound", sandbox=sandbox)
    ctx = type("C", (), {"repo": type("R", (), {"root": "/repo"})})()

    asyncio.run(adapter.invoke(ctx, TestSpec(root="/materialized")))
    assert sandbox.cwd == "/materialized"

    asyncio.run(adapter.invoke(ctx, TestSpec()))
    assert sandbox.cwd == "/bound", "with no root, the bound cwd stands"
