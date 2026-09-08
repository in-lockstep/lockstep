"""A Test bound at a package directory of a monorepo runs THERE inside a materialised worktree.

Found on a fork of langchain-ai/langchain, whose root states nothing about how it builds: Test was
bound at `libs/core`, a strategy passed `Test(root=<worktree>)`, and the root replaced the bound
directory outright -- pytest ran two levels above its `pyproject.toml`, collected nothing, and the
change was judged on a suite that never ran.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from in_lockstep.adapters import CommandTest, PytestTest, tooling
from in_lockstep.core.types import Test


class _Recorder:
    """A runner that records where it was asked to run and with what."""

    def __init__(self) -> None:
        self.cwd: str | None = None
        self.command: list[str] = []

    async def run(self, command: list[str], *, cwd: str | None = None, timeout: float = 900.0) -> Any:
        self.cwd = cwd
        self.command = list(command)
        return type("R", (), {"exit_code": 0, "stdout": "1 passed in 0.01s\n", "stderr": "", "how": "fake"})()


def _ctx(repo: Path) -> Any:
    return type("Ctx", (), {"repo": type("Repo", (), {"root": str(repo)})()})()


def _repo_and_tree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "libs" / "core" / "tests").mkdir(parents=True)
    tree = tmp_path / "worktree"
    (tree / "libs" / "core" / "tests").mkdir(parents=True)
    return repo, tree


def test_gate_tooling_4_a_pytest_test_bound_at_a_package_directory_runs_there_inside_the_worktree(
    tmp_path: Path,
) -> None:
    """GATE-TOOLING-4: the bound `libs/core` becomes `<worktree>/libs/core`, and a path the model
    named from the repository's root is rebased to the package, while a package-relative path is
    left alone."""
    repo, tree = _repo_and_tree(tmp_path)
    runner = _Recorder()
    adapter = PytestTest(args=["-q"], cwd=str(repo / "libs" / "core"), sandbox=runner)

    asyncio.run(
        adapter.invoke(
            _ctx(repo), Test(root=str(tree), paths=("libs/core/tests/test_x.py", "tests/test_y.py"))
        )
    )

    assert runner.cwd == str(tree / "libs" / "core")
    assert runner.command[-2:] == ["tests/test_x.py", "tests/test_y.py"]


def test_gate_tooling_4_a_command_test_bound_at_a_relative_package_directory_runs_there_too(
    tmp_path: Path,
) -> None:
    """GATE-TOOLING-4, the generic runner: a relative `packages/query-core` joins the worktree
    the same way an absolute one under the repository does."""
    repo, tree = _repo_and_tree(tmp_path)
    runner = _Recorder()
    adapter = CommandTest(["node", "vitest.mjs", "run"], cwd="libs/core", sandbox=runner)

    asyncio.run(adapter.invoke(_ctx(repo), Test(root=str(tree), paths=("libs/core/tests/test_x.py",))))

    assert runner.cwd == str(tree / "libs" / "core")
    assert runner.command == ["node", "vitest.mjs", "run", "tests/test_x.py"]


def test_gate_tooling_4_without_a_worktree_the_bound_directory_is_used_as_before(tmp_path: Path) -> None:
    """GATE-TOOLING-4 changes nothing for a run over the real tree: the bound cwd is the cwd, and
    paths are passed through as given."""
    repo, _ = _repo_and_tree(tmp_path)
    runner = _Recorder()
    adapter = CommandTest(["make", "test"], cwd=str(repo / "libs" / "core"), sandbox=runner)

    asyncio.run(adapter.invoke(_ctx(repo), Test(paths=("libs/core/tests/test_x.py",))))

    assert runner.cwd == str(repo / "libs" / "core")
    assert runner.command[-1] == "libs/core/tests/test_x.py"


def test_gate_tooling_4_a_bound_directory_outside_the_repository_yields_the_worktree_root() -> None:
    """A worktree cannot contain a directory outside the tree it copies, so the old answer, the
    root, is the only one; nothing is rebased."""
    assert tooling.within("/tmp/tree", "/elsewhere/pkg", "/repo") == ("/tmp/tree", "")
    assert tooling.within("/tmp/tree", None, "/repo") == ("/tmp/tree", "")
    assert tooling.within("/tmp/tree", "/repo", "/repo") == ("/tmp/tree", "")
    assert tooling.rebase(("a/b.py",), "") == ("a/b.py",)


def test_gate_tooling_4_a_host_absolute_path_is_made_relative_so_a_container_can_see_it(
    tmp_path: Path,
) -> None:
    """GATE-TOOLING-4. A sandbox mounts the tree at a fixed path inside the image, so the
    repository's own absolute root names nothing there: `run selfcheck` with no `--paths`
    defaults to exactly that, and pytest answered `file or directory not found` with exit 4 and
    no summary -- a verdict about a suite that never ran. Relative to the working directory the
    runner is given, the same path names the same file on both sides.
    """
    repo = tmp_path / "repo"
    (repo / "libs" / "core").mkdir(parents=True)

    runner = _Recorder()
    adapter = PytestTest(sandbox=runner)
    asyncio.run(adapter.invoke(_ctx(repo), Test(paths=(str(repo),))))
    assert runner.command[-1] == ".", runner.command

    runner = _Recorder()
    asyncio.run(PytestTest(sandbox=runner).invoke(_ctx(repo), Test(paths=(str(repo / "libs" / "core"),))))
    assert runner.command[-1] == "libs/core"

    # A relative path needs no translation, and one outside the tree gets none: there is no
    # honest answer for a file the container cannot see, and inventing one resolves to the
    # wrong file rather than to nothing.
    runner = _Recorder()
    outside = str(tmp_path / "elsewhere" / "x.py")
    asyncio.run(PytestTest(sandbox=runner).invoke(_ctx(repo), Test(paths=("tests/a.py", outside))))
    assert runner.command[-2:] == ["tests/a.py", outside]


def test_gate_tooling_4_a_command_test_translates_the_same_paths(tmp_path: Path) -> None:
    """The same rule through the generic runner, because the two adapters are the two ways a
    repository binds a suite and a path that works through one must work through the other."""
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _Recorder()
    adapter = CommandTest(["npm", "test"], sandbox=runner)
    asyncio.run(adapter.invoke(_ctx(repo), Test(paths=(str(repo),))))
    assert runner.command[-1] == "."
