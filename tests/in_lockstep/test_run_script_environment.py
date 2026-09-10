"""`run_script` runs in a copy of HEAD that has the repository's own environment in it.

The gap this closes: for as long as `WorktreeRunner` existed it materialised the repository's FILES
and nothing else, so `make lint` and `python -c "import ..."` could not work there -- while
`executables=` truthfully declared `make` and `python` present, because the image carries them. The
declaration was true of the binary and false of the thing anybody would run it for, which is #401's
defect one layer down: a session spending turns to discover what its tools actually do.

Driven with fakes rather than a container: what is asserted is the ORDER and the network, which is
where the security property lives, and a real image would assert the same thing more slowly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from in_lockstep.adapters.worktree import WorktreeRunner, provision_steps
from in_lockstep.core.types import Provision


@dataclass
class _Result:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    how: str = "fake"


@dataclass
class _Runner:
    """A stand-in for the workshop's sandbox. Records what ran, where, and with the network open."""

    allow_network: bool = False
    calls: list[tuple[tuple[str, ...], str | None, bool]] | None = None
    fail_on: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    async def run(self, command: list[str], *, cwd: str | None = None, timeout: float = 900.0) -> _Result:
        assert self.calls is not None
        self.calls.append((tuple(command), cwd, self.allow_network))
        if self.fail_on is not None and tuple(command) == self.fail_on:
            return _Result(exit_code=2, stderr="could not reach the index")
        return _Result(stdout="ok")


@dataclass
class _Provision:
    steps: tuple[tuple[str, ...], ...]


class _Container:
    def __init__(self, provision: _Provision | None) -> None:
        self._provision = provision

    def has(self, verb: object) -> bool:
        return verb is Provision and self._provision is not None

    def resolve(self, verb: object) -> _Provision:
        assert self._provision is not None
        return self._provision


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repository, because `materialize` makes a worktree of one."""
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    for args in (("init", "-q", "."), ("branch", "-M", "main")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "src.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "base"], check=True, capture_output=True)
    return root


def _run(runner: WorktreeRunner, command: list[str]) -> Any:
    return asyncio.run(runner.run(command))


def test_the_repositorys_own_install_runs_before_the_command(repo: Path) -> None:
    """The point of the change: a command meets an environment rather than bare files."""
    inner = _Runner()
    runner = WorktreeRunner(inner, str(repo), _Container(_Provision((("uv", "sync"),))))

    _run(runner, ["make", "lint"])

    assert inner.calls is not None
    assert [call[0] for call in inner.calls] == [("uv", "sync"), ("make", "lint")]


def test_the_network_is_open_for_the_install_and_closed_for_the_command(repo: Path) -> None:
    """The security property, and the reason this is not an allowlist (#422). The tree holds only
    HEAD while the network is open -- `run_script` never applies a staged change at all."""
    inner = _Runner()
    runner = WorktreeRunner(inner, str(repo), _Container(_Provision((("uv", "sync"),))))

    _run(runner, ["make", "lint"])

    assert inner.calls is not None
    networked = {call[0]: call[2] for call in inner.calls}
    assert networked[("uv", "sync")] is True, "the install needs the index"
    assert networked[("make", "lint")] is False, "the command must not reach the network"


def test_the_install_and_the_command_run_in_the_same_throwaway_tree(repo: Path) -> None:
    """One tree, not two. An environment built somewhere the command does not run is no
    environment at all -- which is what mounting a host venv into an image amounted to (#419)."""
    inner = _Runner()
    runner = WorktreeRunner(inner, str(repo), _Container(_Provision((("uv", "sync"),))))

    _run(runner, ["make", "lint"])

    assert inner.calls is not None
    trees = {call[1] for call in inner.calls}
    assert len(trees) == 1, f"install and command ran in different places: {trees}"
    assert str(repo) not in trees, "neither may run in the live tree"


def test_each_call_gets_a_fresh_tree(repo: Path) -> None:
    """The property the class exists for, kept rather than traded for the install being cheaper.
    Nothing one command writes reaches the next, so each call provisions again."""
    inner = _Runner()
    runner = WorktreeRunner(inner, str(repo), _Container(_Provision((("uv", "sync"),))))

    _run(runner, ["make", "lint"])
    _run(runner, ["make", "test"])

    assert inner.calls is not None
    trees = [call[1] for call in inner.calls]
    assert len({t for t in trees}) == 2, "a reused tree would let one command write for the next"
    assert [call[0] for call in inner.calls].count(("uv", "sync")) == 2


def test_a_repository_that_declares_no_provision_installs_nothing(repo: Path) -> None:
    """O1: nothing is invented where the repository declared nothing. The command still runs."""
    inner = _Runner()
    runner = WorktreeRunner(inner, str(repo), _Container(None))

    result = _run(runner, ["make", "lint"])

    assert inner.calls is not None
    assert [call[0] for call in inner.calls] == [("make", "lint")]
    assert result.stderr == "", "nothing to report when nothing was asked for"


def test_a_runner_built_without_a_container_behaves_as_it_did(repo: Path) -> None:
    """A hand-written bind that names no container keeps working, with no environment."""
    inner = _Runner()
    runner = WorktreeRunner(inner, str(repo))

    _run(runner, ["make", "lint"])

    assert inner.calls is not None
    assert [call[0] for call in inner.calls] == [("make", "lint")]


def test_a_failed_install_is_carried_on_the_result_the_model_reads(repo: Path) -> None:
    """A command that failed because its environment was never built reads as a broken repository,
    and a model told that rewrites code which was fine. `run_script` renders stderr beside the exit
    code, so the sentence rides there."""
    inner = _Runner(fail_on=("uv", "sync"))
    runner = WorktreeRunner(inner, str(repo), _Container(_Provision((("uv", "sync"),))))

    result = _run(runner, ["make", "lint"])

    assert "environment was not installed" in result.stderr
    assert "could not reach the index" in result.stderr, "what the install actually said"
    assert inner.calls is not None
    assert ("make", "lint") in [call[0] for call in inner.calls], "the command still ran"


def test_a_successful_install_says_nothing(repo: Path) -> None:
    """The negative control for the note. A sentence on every result is prompt the session pays for
    on every later turn, and a successful install is not news."""
    inner = _Runner()
    runner = WorktreeRunner(inner, str(repo), _Container(_Provision((("uv", "sync"),))))

    assert _run(runner, ["make", "lint"]).stderr == ""


def test_provision_steps_is_the_one_reader_of_the_binding() -> None:
    """Both `_install` and `WorktreeRunner` need the steps without a run context, so there is one
    function that asks. Two spellings is one of them going stale."""
    assert provision_steps(_Container(_Provision((("uv", "sync"),)))) == (("uv", "sync"),)
    assert provision_steps(_Container(None)) == ()
    assert provision_steps(None) == ()
