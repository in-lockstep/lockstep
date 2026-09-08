"""GATE-SEARCH-1, the deterministic half: Graft is provisioned by the framework into its own cache,
indexed before a model starts, fingerprinted, and run with nothing in hand (#375).

Every Graft process in these tests goes through a recording runner handed in as the sandbox
factory, so what ran, with which environment, from which directory, is asserted rather than
trusted -- and no test here needs Node, npm or the network.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click.testing import CliRunner

from in_lockstep.adapters.graft import (
    GRAFT_ENV,
    GRAFT_VERSION,
    NODE_MAJOR,
    Graft,
    _sandbox,
    argv_for,
    cache_root,
)
from in_lockstep.adapters.sandbox import SandboxResult
from in_lockstep.cli import main

PACKAGE = Path("src/in_lockstep/adapters/graft")


@dataclass
class _Call:
    command: list[str]
    cwd: str | None
    network: bool
    env: dict[str, str]


@dataclass
class _Fake:
    """Node, npm and graft, as recorded subprocesses: the answers a real host would give, with
    every process's argv, cwd, network allowance and environment kept for the assertions."""

    node: str = f"v{NODE_MAJOR + 2}.0.0"
    node_exit: int = 0
    npm_exit: int = 0
    npm_stderr: str = ""
    build_exit: int = 0
    calls: list[_Call] = field(default_factory=list)

    def factory(self, network: bool, env: dict[str, str]) -> _Fake:
        self._network, self._env = network, env
        return self

    async def run(
        self, command: list[str], *, cwd: str | None = None, timeout: float = 900.0
    ) -> SandboxResult:
        self.calls.append(_Call(list(command), cwd, self._network, dict(self._env)))
        tool = os.path.basename(command[0])
        if tool == "node":
            return SandboxResult(
                self.node_exit, self.node + "\n", "" if not self.node_exit else "not found", False, "fake"
            )
        if tool == "npm":
            if self.npm_exit == 0:
                prefix = Path(command[command.index("--prefix") + 1])
                binary = prefix / "node_modules" / ".bin" / "graft"
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_text("#!/bin/sh\n")
            return SandboxResult(self.npm_exit, "", self.npm_stderr, False, "fake")
        if tool == "graft":
            if "--version" in command:
                return SandboxResult(0, GRAFT_VERSION + "\n", "", False, "fake")
            if "build" in command:
                index = Path(command[command.index("--dir") + 1])
                index.mkdir(parents=True, exist_ok=True)
                (index / "INDEX.md").write_text("# index\n")
                return SandboxResult(self.build_exit, "", "boom" if self.build_exit else "", False, "fake")
            return SandboxResult(0, "{}", "", False, "fake")
        raise AssertionError(f"unexpected process {command}")

    def commands(self, tool: str) -> list[_Call]:
        return [c for c in self.calls if os.path.basename(c.command[0]) == tool]


def _graft(tmp_path: Path, fake: _Fake) -> Graft:
    return Graft(cache=tmp_path / "cache", sandbox_factory=fake.factory)


def _git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "a.py").write_text("x = 1\n")
    (root / ".gitignore").write_text("*.pyc\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "init"], cwd=root, check=True
    )
    return root


# -- provisioned in the cache -------------------------------------------------------------------


def test_gate_search_1_provision_installs_the_pin_into_the_cache_and_nothing_into_the_repository_or_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GATE-SEARCH-1, provisioned in the cache. `npm ci` runs against the shipped lockfile, under
    a prefix in the cache, with the network allowed for that one process; the repository, the
    global prefix and `$HOME` get nothing; a second provision with the same pin installs nothing."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _git_repo(tmp_path / "repo")
    fake = _Fake()
    graft = _graft(tmp_path, fake)
    assert asyncio.run(graft.ensure_installed()) is None
    (install,) = fake.commands("npm")
    assert install.command[:2] == ["npm", "ci"] and "--prefix" in install.command
    assert install.network is True, "the install is the one process that reaches a registry"
    assert graft.prefix == tmp_path / "cache" / "graft" / GRAFT_VERSION
    assert graft.binary.exists()
    assert (
        json.loads((graft.prefix / "package-lock.json").read_text())["packages"][
            "node_modules/@nanonets/graft"
        ]["version"]
        == GRAFT_VERSION
    )
    assert not (repo / "node_modules").exists() and (repo / ".gitignore").read_text() == "*.pyc\n"
    assert not (tmp_path / "home").exists() or not any((tmp_path / "home").iterdir()), "nothing under $HOME"
    assert graft.installs == 1
    assert asyncio.run(graft.ensure_installed()) is None
    assert graft.installs == 1 and len(fake.commands("npm")) == 1, "a warm cache is a probe, not an install"


def test_gate_search_1_without_node_the_refusal_names_the_version_and_nothing_is_installed(
    tmp_path: Path,
) -> None:
    """GATE-SEARCH-1, refuses by name. No Node, or one older than the pin's `engines`, is
    `search_code.no_node` naming what was found and what is needed, with zero installs attempted."""
    absent = _Fake(node_exit=127)
    graft = _graft(tmp_path, absent)
    refusal = asyncio.run(graft.ensure_installed())
    assert refusal is not None and refusal.startswith("refused: search_code.no_node:")
    assert f"Node >= {NODE_MAJOR}" in refusal
    assert graft.installs == 0 and not absent.commands("npm")

    old = _Fake(node="v18.19.0")
    refusal = asyncio.run(_graft(tmp_path / "b", old).ensure_installed())
    assert refusal is not None and "found v18.19.0" in refusal and not old.commands("npm")


def test_gate_search_1_a_failed_install_refuses_by_name_with_npms_tail_and_the_toolchain_sentence(
    tmp_path: Path,
) -> None:
    """GATE-SEARCH-1, refuses by name. A failed `npm ci` is `search_code.unavailable` carrying
    npm's last lines; when those name node-gyp the refusal says the one thing that fixes it."""
    fake = _Fake(npm_exit=1, npm_stderr="npm ERR! gyp ERR! build error\nnpm ERR! not ok\n")
    refusal = asyncio.run(_graft(tmp_path, fake).ensure_installed())
    assert refusal is not None and refusal.startswith("refused: search_code.unavailable:")
    assert "exited 1" in refusal and "npm ERR! not ok" in refusal
    assert "C/C++ toolchain and python3" in refusal


# -- sealed --------------------------------------------------------------------------------------


def test_gate_search_1_every_graft_process_is_sealed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GATE-SEARCH-1, sealed. Every process Graft runs as carries the four variables, a `HOME`
    in the cache, a `cwd` in the cache and never the repository, `--json` on every query, and
    never `--deep`; and the sandbox those processes go through drops a provider key that is in
    this process's environment."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-for-graft")
    repo = _git_repo(tmp_path / "repo")
    fake = _Fake()
    graft = _graft(tmp_path, fake)
    assert asyncio.run(graft.ensure_installed()) is None
    print_ = asyncio.run(graft.fingerprint(str(repo)))
    assert asyncio.run(graft.ensure_index(str(repo), str(repo), print_)) is None
    asyncio.run(graft.query(str(repo), argv_for("grep", {"query": "-x"}, str(repo))))
    assert fake.calls, "nothing ran"
    for call in fake.calls:
        for name, value in GRAFT_ENV.items():
            assert call.env.get(name) == value, (name, call.command)
        assert call.env["HOME"] == str(graft.home)
        assert call.cwd == str(graft.cache), "fact 2: Graft's dotenv must find nothing to load"
        assert "--deep" not in call.command
    query = fake.calls[-1].command
    assert query[1:5] == ["--dir", str(graft.index_dir(str(repo))), "grep", "--json"]
    assert query[5:] == ["--", "-x", str(repo)], "the model's words come after `--`, so a dash is a pattern"
    with pytest.raises(ValueError, match="never on a Graft argv"):
        asyncio.run(graft.query(str(repo), ["build", "--deep", str(repo)]))
    env = _sandbox(False, graft._env()).clean_env()
    assert "ANTHROPIC_API_KEY" not in env and env["DO_NOT_TRACK"] == "1"


# -- fingerprinted -------------------------------------------------------------------------------


def test_gate_search_1_the_index_lives_outside_the_tree_and_is_rebuilt_only_when_the_tree_changed(
    tmp_path: Path,
) -> None:
    """GATE-SEARCH-1, fingerprinted. Same bytes, same fingerprint, no second build; one changed
    file, or a staged write, or a staged deletion, a different fingerprint and a rebuild; the
    index directory is under the cache and `.gitignore` is untouched; a failed build leaves no
    sidecar, so the next query builds again."""
    repo = _git_repo(tmp_path / "repo")
    fake = _Fake()
    graft = _graft(tmp_path, fake)
    asyncio.run(graft.ensure_installed())
    first = asyncio.run(graft.fingerprint(str(repo)))
    assert first == asyncio.run(graft.fingerprint(str(repo)))
    assert asyncio.run(graft.ensure_index(str(repo), str(repo), first)) is None
    assert asyncio.run(graft.ensure_index(str(repo), str(repo), first)) is None
    assert graft.builds == 1, "the sidecar says this fingerprint; nothing to build"
    index = graft.index_dir(str(repo))
    assert index.is_relative_to(graft.cache) and not index.is_relative_to(repo)
    assert (repo / ".gitignore").read_text() == "*.pyc\n"

    (repo / "a.py").write_text("x = 2\n")
    second = asyncio.run(graft.fingerprint(str(repo)))
    assert second != first, "a dirty checkout is fingerprinted by its bytes"
    assert asyncio.run(graft.ensure_index(str(repo), str(repo), second)) is None
    assert graft.builds == 2

    staged = asyncio.run(graft.fingerprint(str(repo), (("b.py", "y = 1\n"),)))
    deleted = asyncio.run(graft.fingerprint(str(repo), (("a.py", None),)))
    assert len({second, staged, deleted}) == 3, "the staged set is in the fingerprint"

    failing = _Fake(build_exit=1)
    broken = _graft(tmp_path / "broken", failing)
    asyncio.run(broken.ensure_installed())
    refusal = asyncio.run(broken.ensure_index(str(repo), str(repo), first))
    assert refusal is not None and refusal.startswith("refused: search_code.no_index:")
    assert not (broken.index_dir(str(repo)) / "fingerprint").exists()


def test_the_lockfile_pins_the_version_the_module_names() -> None:
    """Two pins, one answer. `GRAFT_VERSION` is what the record will carry and the lockfile is
    what `npm ci` installs; a bump that moves one and not the other fails here."""
    lock = json.loads((PACKAGE / "package-lock.json").read_text())
    manifest = json.loads((PACKAGE / "package.json").read_text())
    entry = lock["packages"]["node_modules/@nanonets/graft"]
    assert entry["version"] == GRAFT_VERSION == manifest["dependencies"]["@nanonets/graft"]
    assert entry["integrity"].startswith("sha512-"), "the pin is by integrity, not only by version"
    assert manifest.get("private") is True


def test_the_cache_is_never_the_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert cache_root() == tmp_path / "xdg" / "in-lockstep"
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "runner"))
    assert cache_root() == tmp_path / "runner" / "in-lockstep"


def test_locations_name_node_on_path_and_graft_in_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What `ls` and `doctor` are told: a cold cache is `graft not found` with the command that
    fills it in `tried`, and a warm one probes the binary's version so a wrong pin is `DOC181`."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/node" if name == "node" else None)
    graft = _graft(tmp_path, _Fake())
    node, cold = graft.locations(str(tmp_path))
    assert node.path == "/usr/bin/node" and node.probe == ("/usr/bin/node", "--version")
    assert cold.path is None and "in-lockstep provision" in cold.tried[0]
    asyncio.run(graft.ensure_installed())
    _, warm = graft.locations(str(tmp_path))
    assert warm.path == str(graft.binary) and warm.probe == (str(graft.binary), "--version")


# -- `in-lockstep provision` runs the framework's own installs -----------------------------------


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.chdir(tmp_path)
    for name in [k for k in os.environ if k.startswith("GITHUB_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("IN_LOCKSTEP_DISABLE", raising=False)
    yield tmp_path


_MODULE = """
from pathlib import Path
from typing import ClassVar

from in_lockstep import Lockstep
from in_lockstep.adapters.graft import GRAFT_VERSION, Graft
from in_lockstep.adapters.sandbox import SandboxResult
from in_lockstep.core.types import Test
from in_lockstep.core.verbs import Capability, Verb

lockstep = Lockstep.detect()


class _Host:
    def __init__(self, network, env):
        self.network = network

    async def run(self, command, *, cwd=None, timeout=900.0):
        tool = Path(command[0]).name
        if tool == "node":
            return SandboxResult(0, "v22.0.0", "", False, "fake")
        if tool == "npm":
            binary = Path(command[command.index("--prefix") + 1]) / "node_modules" / ".bin" / "graft"
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("#!/bin/sh\\n")
            Path("npm-ran").write_text("network=%s" % self.network)
            return SandboxResult(0, "", "", False, "fake")
        return SandboxResult(0, GRAFT_VERSION, "", False, "fake")


class NeedsGraft:
    verb: ClassVar[Verb] = Verb.TEST
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.READS_REPO})
    provisions = (Graft(cache=Path("cache").resolve(), sandbox_factory=_Host),)

    async def invoke(self, ctx, inp):
        raise NotImplementedError


lockstep.bind(Test, NeedsGraft())
"""


def test_gate_search_1_provision_runs_the_frameworks_own_installs_after_the_adopters(repo: Path) -> None:
    """GATE-SEARCH-1, provisioned in the cache. A bound adapter that names a framework-provisioned
    tool in `provisions` gets it installed by `in-lockstep provision`, the job that already reaches
    the network, whether or not the module bound `Provision` -- and the line says so by name."""
    module = repo / ".lockstep" / "lockstep.py"
    module.parent.mkdir(parents=True)
    module.write_text(_MODULE)
    result = CliRunner().invoke(main, ["provision"])
    assert result.exit_code == 0, result.output
    assert "provision  not bound" in result.output, "the adopter's half is reported as before"
    assert "graft      ready" in result.output
    assert (repo / "npm-ran").read_text() == "network=True"
    assert (repo / "cache" / "graft" / GRAFT_VERSION / "node_modules" / ".bin" / "graft").exists()
