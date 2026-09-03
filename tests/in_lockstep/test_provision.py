"""The repository's own environment, built by the framework before anything runs in it
(GATE-PROVISION-1, issue 185).

The scaffolded pipelines run every verb through `uvx`, whose interpreter holds `in-lockstep` and
nothing of the repository's, so the work jobs could never have run the suite a strategy proves a
change with. `Provision` is the verb that builds that environment, bound only from a lockfile that
exists; the trampoline invokes it with one line that is the same in every repository. These tests
state what the adapter does, what the command prints, and that what it builds is what the other
adapters then find.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from in_lockstep.adapters import CommandProvision, Provision, tooling
from in_lockstep.adapters.sandbox import Sandbox, SandboxResult
from in_lockstep.cli import main
from in_lockstep.core.outcome import Status
from in_lockstep.core.types import Locatable, ProvisionResult
from in_lockstep.core.verbs import SHIPPED_VERBS, Verb


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A directory to run in, with the CI variables that would make `detect()` look elsewhere
    removed; the same shape as test_tooling's fixture."""
    monkeypatch.chdir(tmp_path)
    for name in [k for k in os.environ if k.startswith("GITHUB_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("IN_LOCKSTEP_DISABLE", raising=False)
    yield tmp_path


def _executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _uv_repo(root: Path) -> None:
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n")
    (root / "uv.lock").write_text("version = 1\n")


def _ctx(root: Path) -> SimpleNamespace:
    return SimpleNamespace(repo=SimpleNamespace(root=str(root)))


class _Recording:
    """A sandbox that records what ran and answers with the exit code per tool."""

    image = ""
    allow_network = True

    def __init__(self, exits: dict[str, int] | None = None) -> None:
        self.commands: list[list[str]] = []
        self.cwds: list[str | None] = []
        self.exits = exits or {}

    def runtime(self) -> None:
        return None

    async def run(self, command, *, cwd=None, timeout=900.0):  # noqa: ANN001
        self.commands.append(list(command))
        self.cwds.append(cwd)
        code = self.exits.get(os.path.basename(command[0]), 0)
        if command[1:3] == ["-m", "venv"]:
            # What `python -m venv .venv` does, for the test that watches the second step run
            # from the interpreter the first one made.
            _executable(Path(cwd or ".") / ".venv" / "bin" / "python")
        return SandboxResult(
            exit_code=code, stdout="", stderr="lock is stale\n" if code else "", sandboxed=False, how="fake"
        )


# -- the adapter --------------------------------------------------------------------------------


def test_gate_provision_1_steps_run_in_order_and_stop_at_the_first_failure(tmp_path: Path) -> None:
    """An install that half-happened is not an environment, and the step that failed is the one
    to name: the third step never runs, and the finding carries the second's tail."""
    sandbox = _Recording(exits={"npm": 1})
    adapter = CommandProvision([["uv", "sync", "--locked"], ["npm", "ci"], ["make", "deps"]], sandbox=sandbox)
    outcome = asyncio.run(adapter.invoke(_ctx(tmp_path), Provision()))
    assert [c[1:] for c in sandbox.commands] == [["sync", "--locked"], ["ci"]]
    assert outcome.failed and not outcome.blocked
    assert isinstance(outcome.value, ProvisionResult)
    assert outcome.value.steps == ("uv sync --locked", "npm ci")
    assert outcome.findings[0].id == "provision.command_failed" and outcome.findings[0].blocking
    assert "npm ci exited 1" in outcome.findings[0].message and "lock is stale" in outcome.findings[0].message
    assert all(cwd == str(tmp_path) for cwd in sandbox.cwds), "every step runs in the repository"


def test_every_step_that_ran_is_in_the_result_when_all_succeed(tmp_path: Path) -> None:
    sandbox = _Recording()
    adapter = CommandProvision([["uv", "sync", "--locked"], ["npm", "ci"]], sandbox=sandbox)
    outcome = asyncio.run(adapter.invoke(_ctx(tmp_path), Provision()))
    assert outcome.succeeded
    assert outcome.value is not None and outcome.value.steps == ("uv sync --locked", "npm ci")


def test_a_provisioner_found_nowhere_is_a_refusal_naming_every_place_looked(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    """Exit 127 is an environment fact, not a verdict: `errored`, with the venv path and PATH in
    the reason, the same shape `CommandTest` gives a missing runner (GATE-TOOLING-1)."""
    monkeypatch.setattr(tooling.shutil, "which", lambda name: None)
    sandbox = _Recording(exits={"uv": 127})
    adapter = CommandProvision([["uv", "sync", "--locked"]], sandbox=sandbox)
    outcome = asyncio.run(adapter.invoke(_ctx(tmp_path), Provision()))
    # `Outcome.errored` is the constructor, not a property like `failed`: check the member.
    assert outcome.status is Status.ERRORED
    assert outcome.reason is not None and outcome.reason.startswith("uv could not be run; looked for ")
    assert str(tmp_path / ".venv" / "bin" / "uv") in outcome.reason and "uv on PATH" in outcome.reason


def test_a_network_less_container_is_blocked_not_run(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """The control working: a sandbox that denies the network cannot reach a registry, and running
    it would produce a failure that reads as the registry's. Nothing is executed."""
    monkeypatch.setattr(Sandbox, "runtime", lambda self: "/usr/bin/docker")

    async def never(self, command, *, cwd=None, timeout=900.0):  # noqa: ANN001
        raise AssertionError(f"ran {command}")

    monkeypatch.setattr(Sandbox, "run", never)
    adapter = CommandProvision([["uv", "sync"]], sandbox=Sandbox(image="python:3.12-slim"))
    outcome = asyncio.run(adapter.invoke(_ctx(tmp_path), Provision()))
    assert outcome.blocked
    assert outcome.reason is not None and "allow_network=True" in outcome.reason


def test_gate_provision_1_an_image_that_denies_the_network_is_blocked_even_with_no_runtime(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    """Without a runtime, `Sandbox` would run the steps on the host with the network it has: the
    binding said container and no network, and quietly doing less is the failure a security
    control must not have. Refused, not substituted."""
    monkeypatch.setattr(Sandbox, "runtime", lambda self: None)

    async def never(self, command, *, cwd=None, timeout=900.0):  # noqa: ANN001
        raise AssertionError(f"ran {command}")

    monkeypatch.setattr(Sandbox, "run", never)
    adapter = CommandProvision([["uv", "sync"]], sandbox=Sandbox(image="python:3.12-slim"))
    outcome = asyncio.run(adapter.invoke(_ctx(tmp_path), Provision()))
    assert outcome.blocked and outcome.reason is not None and "Sandbox()" in outcome.reason


def test_a_sandbox_that_requires_a_container_and_finds_none_is_blocked_not_failed(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    """The sandbox's own refusal (exit 126, `refused:no-container`) is the control working, and
    it must not read as the install failing."""
    monkeypatch.setattr(Sandbox, "runtime", lambda self: None)
    sandbox = Sandbox(image="python:3.12-slim", allow_network=True, require_container=True)
    adapter = CommandProvision([["uv", "sync"]], sandbox=sandbox)
    outcome = asyncio.run(adapter.invoke(_ctx(tmp_path), Provision()))
    assert outcome.blocked
    assert outcome.reason is not None and "refusing to run outside a container" in outcome.reason


def test_a_runner_that_is_not_a_sandbox_runs_the_steps(tmp_path: Path) -> None:
    """`UnsandboxedRun` is the named opt-out every sibling adapter accepts, and a fake with only
    `run` is what the sibling tests bind; neither has `image` or `allow_network`."""
    from in_lockstep.adapters.sandbox import UnsandboxedRun

    adapter = CommandProvision([["sh", "-c", "exit 0"]], sandbox=UnsandboxedRun())
    outcome = asyncio.run(adapter.invoke(_ctx(tmp_path), Provision()))
    assert outcome.succeeded, outcome

    class RunOnly:
        async def run(self, command, *, cwd=None, timeout=900.0):  # noqa: ANN001
            return SandboxResult(exit_code=0, stdout="", stderr="", sandboxed=False, how="fake")

    outcome = asyncio.run(
        CommandProvision([["uv", "sync"]], sandbox=RunOnly()).invoke(_ctx(tmp_path), Provision())
    )
    assert outcome.succeeded


def test_exit_127_from_a_tool_that_was_found_is_the_install_failing_not_the_tool_absent(
    tmp_path: Path,
) -> None:
    """Provisioning is where a lockfile's hooks run, and npm passes a `preinstall` that names a
    missing command through as 127. With npm found, that is `provision.command_failed` with the
    tail, not a claim that npm is nowhere."""
    npm = _executable(tmp_path / ".venv" / "bin" / "npm")
    sandbox = _Recording(exits={"npm": 127})
    adapter = CommandProvision([["npm", "ci"]], sandbox=sandbox)
    outcome = asyncio.run(adapter.invoke(_ctx(tmp_path), Provision()))
    assert outcome.status is Status.FAILED
    assert outcome.findings[0].id == "provision.command_failed"
    assert f"{npm} ci exited 127" in outcome.findings[0].message


def test_the_default_sandbox_allows_the_network_and_still_drops_credentials(monkeypatch) -> None:  # noqa: ANN001
    """The one deterministic adapter whose job is to reach a registry. A lockfile's install hooks
    are repository-authored code, so the provider key is still not in their environment."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-real")
    adapter = CommandProvision([["uv", "sync"]])
    assert adapter.sandbox.allow_network is True
    assert "ANTHROPIC_API_KEY" not in adapter.sandbox.clean_env()


def test_the_second_step_runs_from_the_interpreter_the_first_one_made(tmp_path: Path) -> None:
    """The requirements.txt shape: `python -m venv .venv`, then the venv's own python installs.
    The second step names the venv path detection spelled from `VENV_BIN`, so what `ls` prints
    for it is the command that runs; the first resolves `python` the way the suite's interpreter
    does, by the path found, minus the `import pytest` probe."""
    venv_python = os.path.join(*tooling.VENV_BIN, "python")
    sandbox = _Recording()
    adapter = CommandProvision(
        [["python", "-m", "venv", ".venv"], [venv_python, "-m", "pip", "install", "-r", "requirements.txt"]],
        sandbox=sandbox,
    )
    where = adapter.locations(str(tmp_path))
    assert [r.tool for r in where] == ["python", venv_python]
    assert where[0].probe == (), "the interpreter that creates a venv need not have pytest in it"
    assert where[1].how.startswith("as bound, relative to the working directory")

    outcome = asyncio.run(adapter.invoke(_ctx(tmp_path), Provision()))
    assert outcome.succeeded, outcome
    first, second = sandbox.commands
    assert os.path.isabs(first[0]) and first[1:] == ["-m", "venv", ".venv"]
    assert second[0] == venv_python and second[-1] == "requirements.txt"


def test_one_location_per_distinct_tool_and_the_venv_copy_by_path(tmp_path: Path) -> None:
    """`uv` twice is one line, and a tool found in the repository's .venv runs by its path, since
    the venv is on nobody's PATH (the `_argv0` rule, GATE-TOOLING-1)."""
    uv = _executable(tmp_path / ".venv" / "bin" / "uv")
    sandbox = _Recording()
    adapter = CommandProvision([["uv", "sync"], ["uv", "run", "prebuild"]], sandbox=sandbox)
    where = adapter.locations(str(tmp_path))
    assert len(where) == 1 and where[0].path == str(uv) and where[0].how == tooling.REPOSITORY_VENV
    asyncio.run(adapter.invoke(_ctx(tmp_path), Provision()))
    assert [c[0] for c in sandbox.commands] == [str(uv), str(uv)]


def test_a_request_root_wins_over_the_bound_cwd_wins_over_the_repository(tmp_path: Path) -> None:
    sandbox = _Recording()
    (tmp_path / "bound").mkdir()
    (tmp_path / "asked").mkdir()
    adapter = CommandProvision([["uv", "sync"]], cwd=str(tmp_path / "bound"), sandbox=sandbox)
    asyncio.run(adapter.invoke(_ctx(tmp_path), Provision()))
    asyncio.run(adapter.invoke(_ctx(tmp_path), Provision(root=str(tmp_path / "asked"))))
    assert sandbox.cwds == [str(tmp_path / "bound"), str(tmp_path / "asked")]


def test_an_empty_step_list_or_an_empty_step_is_refused() -> None:
    with pytest.raises(ValueError):
        CommandProvision([])
    with pytest.raises(ValueError):
        CommandProvision([["uv", "sync"], []])


def test_provision_is_a_shipped_verb_with_the_capabilities_policy_keys_on() -> None:
    """Shipped, so `ls` treats it unbound as ordinary rather than as a typo; and it declares
    that it writes, executes and reaches the network, because it does all three."""
    from in_lockstep.core.verbs import Capability

    assert "provision" in SHIPPED_VERBS and Verb("provision") is Verb.PROVISION
    assert CommandProvision.verb is Verb.PROVISION
    assert {Capability.EXECUTES_CODE, Capability.WRITES_FILES, Capability.REACHES_NETWORK} <= set(
        CommandProvision.capabilities
    )
    assert isinstance(CommandProvision([["uv", "sync"]]), Locatable)


# -- ls and doctor see it ---------------------------------------------------------------------


def test_ls_prints_the_provisioner_and_where_its_tool_came_from(repo: Path) -> None:
    """A repository with no module runs on detected defaults, and the binding shows up with its
    resolution line before anything runs."""
    _uv_repo(repo)
    uv = _executable(repo / ".venv" / "bin" / "uv")
    result = CliRunner().invoke(main, ["ls"])
    assert result.exit_code == 0, result.output
    assert "Provision              -> CommandProvision" in result.output
    assert f"uv  {uv}  (the repository's .venv)" in result.output
    assert "provision: uv sync --locked" in result.output
    assert "verbs defined but unbound" not in result.output


def test_doctor_raises_doc180_for_a_bound_provisioner_it_cannot_find(repo: Path, monkeypatch) -> None:  # noqa: ANN001
    """Through the `Locatable` seam and no doctor code of its own: the scaffolded module binds
    Provision from the lockfile, and a `uv` that is nowhere is named before any run."""
    _uv_repo(repo)
    assert CliRunner().invoke(main, ["init"]).exit_code == 0
    monkeypatch.setattr(tooling.shutil, "which", lambda name: None)
    result = CliRunner().invoke(main, ["doctor"])
    assert "DOC180" in result.output, result.output
    assert "Provision -> CommandProvision found no uv" in result.output


# -- the command --------------------------------------------------------------------------------


def test_provision_with_nothing_bound_says_not_bound_and_exits_zero(repo: Path) -> None:
    """Absent is reported as absent, never as success, and the job goes on: a Go repository with a
    toolchain on the runner has nothing to provision and nothing wrong."""
    result = CliRunner().invoke(main, ["provision"])
    assert result.exit_code == 0, result.output
    assert "provision  not bound" in result.output
    assert "succeeded" not in result.output
    assert "uv.lock" in result.output and "lockstep.bind(Provision" not in result.output


def test_provision_runs_the_bound_steps_and_reports_each(repo: Path) -> None:
    _uv_repo(repo)
    uv = _executable(repo / ".venv" / "bin" / "uv", '#!/bin/sh\necho "$@" > provisioned\n')
    result = CliRunner().invoke(main, ["provision"])
    assert result.exit_code == 0, result.output
    assert f"uv  {uv}  (the repository's .venv)" in result.output
    assert f"ran {uv} sync --locked" in result.output
    assert "provision  succeeded" in result.output
    assert (repo / "provisioned").read_text().strip() == "sync --locked"


def test_provision_exits_nonzero_and_names_the_step_when_it_fails(repo: Path) -> None:
    """Fails here, by name, rather than twenty minutes later as a red suite."""
    _uv_repo(repo)
    _executable(
        repo / ".venv" / "bin" / "uv", "#!/bin/sh\necho 'the lockfile needs to be updated' >&2\nexit 2\n"
    )
    result = CliRunner().invoke(main, ["provision"])
    assert result.exit_code == 1, result.output
    assert "provision  failed" in result.output
    assert "provision.command_failed" in result.output
    assert "sync --locked exited 2" in result.output and "the lockfile needs to be updated" in result.output


def test_the_kill_switch_refuses_provision_before_the_module_is_even_loaded(repo: Path, monkeypatch) -> None:  # noqa: ANN001
    """ "Nothing executes" has to include the module's own import-time code, so the switch is read
    before `_default_lockstep` loads it."""
    _uv_repo(repo)
    _executable(repo / ".venv" / "bin" / "uv", "#!/bin/sh\ntouch provisioned\n")
    assert CliRunner().invoke(main, ["init"]).exit_code == 0
    module = repo / ".lockstep" / "lockstep.py"
    module.write_text(module.read_text() + "\nimport pathlib\npathlib.Path('imported').touch()\n")
    monkeypatch.setenv("IN_LOCKSTEP_DISABLE", "1")
    result = CliRunner().invoke(main, ["provision"])
    assert result.exit_code == 3, result.output
    assert "DISABLED" in result.output
    assert not (repo / "provisioned").exists() and not (repo / "imported").exists()


def test_provision_names_the_module_not_detection_when_a_module_binds_nothing(repo: Path) -> None:
    """Every repository scaffolded before Provision existed is in this state after upgrading: a
    module is the truth, detection was never consulted, and a uv.lock in the tree is not
    "detection found no uv.lock"."""
    assert CliRunner().invoke(main, ["init"]).exit_code == 0
    (repo / "uv.lock").write_text("version = 1\n")
    result = CliRunner().invoke(main, ["provision"])
    assert result.exit_code == 0, result.output
    assert "provision  not bound" in result.output
    assert "the module" in result.output and "detection found" not in result.output


def test_gate_provision_1_the_interpreter_the_suite_runs_on_is_the_one_provision_built(
    repo: Path, monkeypatch
) -> None:  # noqa: ANN001
    """End to end. Before, `python` resolves outside the repository; `provision` runs the `uv` on
    PATH (a stand-in that does what `uv sync` does: makes `.venv/bin/python`); after, the same
    resolution `PytestTest` makes finds the repository's own venv first (GATE-TOOLING-1)."""
    _uv_repo(repo)
    fakebin = repo.parent / "fakebin"
    _executable(
        fakebin / "uv",
        "#!/bin/sh\nmkdir -p .venv/bin && printf '#!/bin/sh\\nexit 0\\n' > .venv/bin/python "
        "&& chmod +x .venv/bin/python\n",
    )
    monkeypatch.setenv("PATH", f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}")
    before = tooling.interpreter(str(repo), Sandbox())
    assert before.how != tooling.REPOSITORY_VENV

    result = CliRunner().invoke(main, ["provision"])
    assert result.exit_code == 0, result.output
    assert "ran uv sync --locked" in result.output, "a PATH tool keeps its bare name"

    after = tooling.interpreter(str(repo), Sandbox())
    assert after.path == str(repo / ".venv" / "bin" / "python") and after.how == tooling.REPOSITORY_VENV
