"""Phase-3 gates: the controls that replace what the substrate provided."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import pytest

from in_lockstep import doctor
from in_lockstep.adapters.sandbox import Sandbox, UnsandboxedRun
from in_lockstep.core.changes import ChangeGuard
from in_lockstep.core.container import Container
from in_lockstep.core.context import RepoInfo, RunContext
from in_lockstep.core.outcome import Outcome, Status
from in_lockstep.core.types import ChangeAuthor, ChangeSet, FileChange
from in_lockstep.core.verbs import Capability, Verb
from in_lockstep.middleware.approval import ApprovalGate, ApprovalRequired, assert_gated
from in_lockstep.privileged.egress import (
    EgressMode,
    EgressPolicy,
    EgressRefused,
    UnsandboxedEgress,
)


class Writer:
    verb: ClassVar[Verb] = Verb.IMPLEMENT
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.WRITES_FILES})

    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, ctx, inp):
        self.calls += 1
        return Outcome(status=Status.SUCCEEDED, value=inp)


class Reader:
    verb: ClassVar[Verb] = Verb.REVIEW
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.READS_REPO})

    async def invoke(self, ctx, inp):
        return Outcome(status=Status.SUCCEEDED, value=inp)


@dataclass(frozen=True)
class Thing:
    payload: str = ""


# -- GATE-EGRESS ---------------------------------------------------------------------


def test_gate_egress_1_untrusted_context_makes_enforcement_mandatory() -> None:
    """The case a capability-only rule exempts, and the one that actually happens."""
    policy = EgressPolicy(mode=EgressMode.NONE)
    with pytest.raises(EgressRefused) as exc:
        policy.check(capabilities=frozenset(), untrusted_context=True)
    assert exc.value.reason == "egress.unenforced"
    assert "untrusted external content" in str(exc.value)


def test_read_only_over_trusted_content_needs_no_enforcement() -> None:
    """A laptop reviewing its own code must not require Docker."""
    EgressPolicy(mode=EgressMode.NONE).check(
        capabilities=frozenset({Capability.READS_REPO}), untrusted_context=False
    )


def test_write_and_execute_capability_make_enforcement_mandatory() -> None:
    policy = EgressPolicy(mode=EgressMode.NONE)
    for capability in (Capability.WRITES_FILES, Capability.EXECUTES_CODE, Capability.REACHES_NETWORK):
        with pytest.raises(EgressRefused):
            policy.check(capabilities=frozenset({capability}), untrusted_context=False)


def test_restricted_repo_makes_enforcement_mandatory() -> None:
    policy = EgressPolicy(mode=EgressMode.NONE, restricted_repo=True)
    with pytest.raises(EgressRefused, match="restricted"):
        policy.check(capabilities=frozenset(), untrusted_context=False)


def test_the_restricted_classification_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """`restricted_repo` was a parameter nothing set — a trigger with no finger on it."""
    monkeypatch.setenv("IN_LOCKSTEP_RESTRICTED", "1")
    assert EgressPolicy.detect().restricted_repo is True
    monkeypatch.setenv("IN_LOCKSTEP_RESTRICTED", "no")
    assert EgressPolicy.detect().restricted_repo is False
    monkeypatch.delenv("IN_LOCKSTEP_RESTRICTED")
    assert EgressPolicy.detect(restricted_repo=True).restricted_repo is True, (
        "a binding that says restricted must not be un-said by an absent variable"
    )


def test_the_manifest_is_endpoints_plus_declared_extras() -> None:
    """What `allow` is for: the operator's additions to the computed proxy list."""
    policy = EgressPolicy(allow=("pypi.org", "api.github.com"))
    hosts = policy.manifest(["https://api.anthropic.com/v1", "http://localhost:11434"])
    assert hosts == ("api.anthropic.com", "api.github.com", "localhost", "pypi.org")


def test_the_manifest_deduplicates_and_survives_a_bare_host() -> None:
    policy = EgressPolicy(allow=("api.anthropic.com",))
    assert policy.manifest(["https://api.anthropic.com", "api.anthropic.com"]) == ("api.anthropic.com",)


def test_gate_egress_2_an_asserted_mode_a_probe_disproves_is_refused() -> None:
    """Fail-closed that can be satisfied by a lie is not fail-closed."""
    policy = EgressPolicy(mode=EgressMode.ENFORCED_EXTERNAL)
    policy._verified = False  # the probe reached the open internet
    with pytest.raises(EgressRefused) as exc:
        policy.check(capabilities=frozenset({Capability.WRITES_FILES}), untrusted_context=False)
    assert exc.value.reason == "egress.probe_failed"


def test_gate_egress_2_the_probe_is_consulted_once_and_a_connection_that_succeeds_refuses_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GATE-EGRESS-2, verified rather than attested. The neighbouring test sets the probe's answer
    by hand, which proves the refusal and nothing about the probe; this stubs `_can_connect`
    itself. An ENFORCED mode whose probe reaches the blocked host is refused before any call and
    the probe was asked exactly once for the session; one whose probe cannot connect permits the
    run, and a second `check` asks nothing again."""
    from in_lockstep.privileged import egress as module

    asked: list[tuple[str, int]] = []

    def connects(host: str, port: int) -> bool:
        asked.append((host, port))
        return True

    monkeypatch.setattr(module, "_can_connect", connects)
    policy = EgressPolicy(mode=EgressMode.ENFORCED_EXTERNAL)
    with pytest.raises(EgressRefused) as exc:
        policy.check(capabilities=frozenset({Capability.WRITES_FILES}), untrusted_context=False)
    assert exc.value.reason == "egress.probe_failed"
    assert asked == [(module.PROBE_HOST, module.PROBE_PORT)], "probed the blocked host, once"
    with pytest.raises(EgressRefused):
        policy.check(capabilities=frozenset({Capability.WRITES_FILES}), untrusted_context=False)
    assert len(asked) == 1, "cached for the session; a second check does not probe again"

    monkeypatch.setattr(module, "_can_connect", lambda host, port: False)
    sealed = EgressPolicy(mode=EgressMode.ENFORCED_CONTAINER)
    sealed.check(capabilities=frozenset({Capability.WRITES_FILES}), untrusted_context=False)
    assert sealed.verify() is True


def test_a_verified_mode_permits_the_run() -> None:
    policy = EgressPolicy(mode=EgressMode.ENFORCED_CONTAINER)
    policy._verified = True
    policy.check(capabilities=frozenset({Capability.WRITES_FILES}), untrusted_context=True)


def test_the_opt_out_is_named_after_what_it_does() -> None:
    """Greppable and reviewable, rather than a flag buried in an options object."""
    UnsandboxedEgress().check(capabilities=frozenset({Capability.EXECUTES_CODE}), untrusted_context=True)
    assert "Unsandboxed" in UnsandboxedEgress.__name__


def test_mode_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IN_LOCKSTEP_EGRESS", "enforced")
    assert EgressPolicy.detect().mode is EgressMode.ENFORCED_EXTERNAL
    monkeypatch.delenv("IN_LOCKSTEP_EGRESS")
    assert EgressPolicy.detect().mode is EgressMode.NONE


# -- GATE-APPROVAL -------------------------------------------------------------------


def test_gate_approval_1_dangerous_binding_refused_at_resolution_not_call_time() -> None:
    """A binding that grants write with no approval path is a configuration error."""
    container = Container()
    container.bind(Thing, Writer())
    with pytest.raises(ApprovalRequired, match="writes_files"):
        assert_gated(container, Thing)


def test_a_read_only_binding_needs_no_approval() -> None:
    container = Container()
    container.bind(Thing, Reader())
    assert_gated(container, Thing)


def test_approval_gate_blocks_a_writing_action_without_a_grant() -> None:
    adapter = Writer()
    container = Container()
    container.bind(Thing, adapter)
    ctx = RunContext(run_id="t", repo=RepoInfo(root="."), container=container, middleware=[ApprovalGate()])
    outcome = asyncio.run(ctx.do(Thing("x")))
    assert outcome.status is Status.BLOCKED
    assert outcome.reason == "approval.required"
    assert adapter.calls == 0


def test_approval_gate_blocks_a_via_supplied_writer_without_a_grant() -> None:
    """A call-scoped adapter is gated exactly like a bound one: `via=` names what serves the
    call, and the gate reads the capability declaration off that."""
    adapter = Writer()
    container = Container()
    ctx = RunContext(run_id="t", repo=RepoInfo(root="."), container=container, middleware=[ApprovalGate()])
    outcome = asyncio.run(ctx.do(Thing("x"), via=adapter))
    assert outcome.status is Status.BLOCKED
    assert outcome.reason == "approval.required"
    assert adapter.calls == 0


def test_approval_gate_admits_a_granted_action() -> None:
    adapter = Writer()
    container = Container()
    container.bind(Thing, adapter)
    ctx = RunContext(
        run_id="t",
        repo=RepoInfo(root="."),
        container=container,
        middleware=[ApprovalGate(granted=lambda call: True)],
    )
    assert asyncio.run(ctx.do(Thing("x"))).succeeded
    assert adapter.calls == 1


# -- GATE-SANDBOX --------------------------------------------------------------------


def test_gate_sandbox_1_a_child_cannot_read_the_parents_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """pytest executes repository conftest.py; in-process it would reach live Credentials.

    `sys.executable` rather than the bare name `python`, here and below, for the reason #112
    recorded about the oneshot fixtures and then missed in this file: these commands are EXECUTED,
    and macOS, Debian, Ubuntu and most slim images install the interpreter as `python3` with no
    alias. The bare name exits 127 there — and because `make check` runs under `uv run`, which
    prepends a virtualenv holding a `python` shim, this file passed on the one path anybody
    measures and failed on every other. A security assertion that only runs under one invocation
    of the test runner is worse than a red one, because nothing says so.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    result = asyncio.run(
        Sandbox().run(
            [sys.executable, "-c", "import os; print(os.environ.get('ANTHROPIC_API_KEY', 'ABSENT'))"]
        )
    )
    assert "sk-must-not-leak" not in result.stdout
    assert "ABSENT" in result.stdout


# -- GATE-SANDBOX-2: a file the MODEL staged runs in a container, or the run refuses by name ------


@pytest.mark.parametrize(
    "runner,why",
    [
        (None, "exposes no sandbox"),
        (Sandbox(), "names no container image"),
        (UnsandboxedRun(), "this host by name"),
    ],
)
def test_gate_sandbox_2_a_runner_that_would_use_the_host_is_named_before_anything_is_staged(
    runner: object, why: str
) -> None:
    """GATE-SANDBOX-2. `Sandbox()` -- the runner `init` scaffolded and this repository bound --
    ran a test the model wrote as a subprocess with `HOME` and an open socket (#308). The
    question is asked of what the runner DECLARES, so it costs nothing and materialises nothing."""
    from in_lockstep.adapters.sandbox import host_fallback

    answer = host_fallback(runner)
    assert answer is not None and why in answer


def test_gate_sandbox_2_a_runner_that_contains_by_construction_is_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An image plus `require_container` either runs in a container or refuses at run time, so
    it is contained whatever this machine has; an image alone is contained when a runtime is here
    and the fallback hole when one is not -- the one case that is probed rather than declared."""
    from in_lockstep.adapters.sandbox import host_fallback

    assert host_fallback(Sandbox(image="ghcr.io/x/ci:1", require_container=True)) is None
    monkeypatch.setattr(Sandbox, "runtime", lambda self: "/usr/bin/podman")
    assert host_fallback(Sandbox(image="ghcr.io/x/ci:1")) is None
    monkeypatch.setattr(Sandbox, "runtime", lambda self: None)
    assert "fall back to a subprocess" in str(host_fallback(Sandbox(image="ghcr.io/x/ci:1")))


def test_gate_sandbox_2_the_container_has_no_network_one_writable_mount_and_read_only_extras(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """What "cannot read outside the worktree and cannot open a socket" rests on, asserted on the
    argv the runtime is handed: `--network=none`, the tree as the only read-write mount, every
    `mounts=` entry `:ro`, and no `HOME` reaching the container -- the client's pass-through set
    is the client's."""
    from in_lockstep.adapters import sandbox as sandbox_module

    seen: dict[str, Any] = {}

    async def fake_exec(argv, *, cwd, env, timeout):  # noqa: ANN001
        seen["argv"] = list(argv)
        return 0, "", ""

    monkeypatch.setattr(sandbox_module, "_exec", fake_exec)
    monkeypatch.setattr(Sandbox, "runtime", lambda self: "/usr/bin/podman")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    venv = tmp_path / ".venv"
    box = Sandbox(image="python:3.11-slim", mounts=((str(venv), "/venv"),), extra_env={"PYTHONPATH": "/venv"})
    asyncio.run(box.run(["python", "-m", "pytest"], cwd=str(tmp_path / "tree")))
    argv = seen["argv"]
    assert "--network=none" in argv and "--cap-drop=ALL" in argv
    assert "--init" in argv, "without an init at PID 1 a staged test's killed children stay as zombies"
    volumes = [argv[i + 1] for i, flag in enumerate(argv) if flag == "-v"]
    writable = [v for v in volumes if not v.endswith(":ro")]
    assert writable == [f"{tmp_path / 'tree'}:/work"], volumes
    assert f"{venv}:/venv:ro" in volumes
    assert "HOME=/tmp" in argv and not any(
        item.startswith("HOME=") and item != "HOME=/tmp" for item in argv
    ), "the host's HOME reached the container"
    # As the host user, with podman told to keep the id: root inside with every capability
    # dropped cannot write a tree the host user owns, which is what the runner handed it (#312).
    assert f"{os.getuid()}:{os.getgid()}" in argv and "--userns=keep-id" in argv
    assert argv.index("--user") < argv.index("-v")
    # A name the image's passwd file cannot supply for that uid, and not the host user's.
    assert "USER=sandbox" in argv and "LOGNAME=sandbox" in argv
    assert not any(item.startswith(("USER=", "LOGNAME=")) and not item.endswith("=sandbox") for item in argv)


def test_gate_sandbox_2_a_refused_container_is_a_blocked_test_not_a_broken_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Sandbox(require_container=True)` finding no runtime ran nothing, and `PytestTest` read that
    exit as "no summary, errored" -- a sentence about the suite for what was the control working.
    `CommandTest` had the guard since #256; the pytest adapter now maps the same `how`."""
    from in_lockstep.adapters.pytest_adapter import PytestTest
    from in_lockstep.core.outcome import Status
    from in_lockstep.core.types import Test

    monkeypatch.setattr(Sandbox, "runtime", lambda self: None)
    adapter = PytestTest(sandbox=Sandbox(image="ghcr.io/x/ci:1", require_container=True))
    outcome = asyncio.run(adapter.invoke(object(), Test(root=str(tmp_path))))
    assert outcome.status is Status.BLOCKED, outcome
    assert "refusing to run outside a container" in str(outcome.reason)


def _image_present(image: str) -> str | None:
    """The runtime that already holds `image`, or None. A pull is a network act this suite does
    not make; the live half of GATE-SANDBOX-2 runs where somebody has pulled the image and skips
    by name everywhere else, which is the honest state and is written in the row."""
    runtime = Sandbox().runtime()
    if runtime is None:
        return None
    done = subprocess.run([runtime, "image", "exists", image], capture_output=True)
    if done.returncode != 0:
        done = subprocess.run([runtime, "image", "inspect", image], capture_output=True)
    return runtime if done.returncode == 0 else None


_LIVE_IMAGE = "docker.io/library/python:3.12-slim"


def test_gate_sandbox_2_a_staged_test_in_the_container_reaches_neither_the_host_nor_a_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live half: the file a model staged, run under the shipped flags, cannot read a path
    outside its tree and cannot open a socket. `os.getuid` differs between runtimes, so it reads
    what is universal -- a file the test planted beside the tree, and a connect() to a loopback
    port nothing listens on, which under `--network=none` fails before it is refused."""
    if _image_present(_LIVE_IMAGE) is None:
        pytest.skip(f"{_LIVE_IMAGE} is not pulled here; the live half of GATE-SANDBOX-2 runs where it is")
    outside = tmp_path / "outside.txt"
    outside.write_text("must-not-be-readable\n")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "probe.py").write_text(
        "import os, socket, sys\n"
        f"print('outside:', os.path.exists({str(outside)!r}))\n"
        "print('home:', os.environ.get('HOME', 'ABSENT'))\n"
        "try:\n"
        "    socket.create_connection(('127.0.0.1', 9), timeout=1)\n"
        "    print('socket: opened')\n"
        "except OSError as e:\n"
        "    print('socket: refused', type(e).__name__)\n"
    )
    result = asyncio.run(
        Sandbox(image=_LIVE_IMAGE, require_container=True).run(["python", "probe.py"], cwd=str(tree))
    )
    assert result.sandboxed, result
    assert "outside: False" in result.stdout, result.stdout
    assert "socket: refused" in result.stdout, result.stdout
    assert str(tmp_path) not in result.stdout, "the host's HOME reached the container"


def test_the_named_opt_out_does_leak_which_is_why_it_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leaks-here")
    result = asyncio.run(
        UnsandboxedRun().run(
            [sys.executable, "-c", "import os; print(os.environ.get('ANTHROPIC_API_KEY', 'ABSENT'))"]
        )
    )
    assert "sk-leaks-here" in result.stdout
    assert result.how == "unsandboxed"


def test_the_fallback_says_it_is_not_a_kernel_sandbox() -> None:
    result = asyncio.run(Sandbox().run([sys.executable, "-c", "print(1)"]))
    assert result.how == "subprocess:no-credentials"
    assert result.sandboxed is False, "honest about what it is"


def test_a_sandboxed_command_that_hangs_is_killed() -> None:
    result = asyncio.run(Sandbox().run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5))
    assert result.exit_code == 124


def test_a_sandboxed_command_that_hangs_is_killed_with_the_processes_it_started(tmp_path: Path) -> None:
    """`npm start` is `sh -c "node server.js"`. Killing npm alone left the server bound to its
    port after the run had reported 124, so the child runs in its own session and the whole
    group is killed."""
    import os
    import signal
    import time

    pidfile = tmp_path / "pid"
    result = asyncio.run(Sandbox().run(["sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"], timeout=0.5))
    assert result.exit_code == 124
    grandchild = int(pidfile.read_text())
    for _ in range(60):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(grandchild, signal.SIGKILL)
        raise AssertionError("the sleep the shell started outlived the run")


def test_a_containers_variables_travel_as_flags_and_never_reach_the_runtimes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`extra_env` used to land on the docker/podman client's environment, which a container does
    not inherit, so a `Run.env` was honoured by the subprocess fallback and dropped by the
    stronger path. Worse, `DOCKER_HOST` there would have pointed the whole run at another daemon.
    The variables go in as `-e` flags; the client sees only the pass-through set."""
    from in_lockstep.adapters import sandbox as sandbox_module

    seen: dict[str, Any] = {}

    async def fake_exec(argv, *, cwd, env, timeout):  # noqa: ANN001
        seen["argv"], seen["env"] = list(argv), dict(env)
        return 0, "", ""

    monkeypatch.setattr(sandbox_module, "_exec", fake_exec)
    monkeypatch.setattr(Sandbox, "runtime", lambda self: "/usr/bin/docker")
    monkeypatch.setenv("DOCKER_HOST", "tcp://elsewhere:2375")
    box = Sandbox(image="python:3.12", extra_env={"PORT": "8080", "DOCKER_HOST": "tcp://evil:2375"})
    result = asyncio.run(box.run(["make", "run"], cwd=None))
    argv = seen["argv"]
    assert result.how == "docker:python:3.12"
    assert "-e" in argv and "PORT=8080" in argv and "DOCKER_HOST=tcp://evil:2375" in argv
    assert argv.index("PORT=8080") < argv.index("python:3.12"), "flags precede the image"
    assert "PORT" not in seen["env"], "and never reach the client"
    assert seen["env"].get("DOCKER_HOST") != "tcp://evil:2375"


# -- GATE-GUARD-2 --------------------------------------------------------------------


def test_gate_guard_2_symlink_out_of_the_repo_is_refused_post_change_tree() -> None:
    changeset = ChangeSet(
        changes=(FileChange(path="docs/note", contents="", symlink_target="../../../etc/passwd"),)
    )
    refusals = ChangeGuard().check(changeset)
    assert refusals and refusals[0].rule == "symlink-outside-repo-root"


def test_the_guard_runs_over_agent_changes_only() -> None:
    """Or the framework's own ledger commit is denied by its own tier 1."""
    changeset = ChangeSet(
        changes=(FileChange(path=".in-lockstep/ledger/r.json", contents="{}", author=ChangeAuthor.FRAMEWORK),)
    )
    assert ChangeGuard().check(changeset) == []


def test_an_agent_writing_the_ledger_is_still_refused() -> None:
    """The distinction is the author, not the path."""
    changeset = ChangeSet(
        changes=(FileChange(path=".in-lockstep/ledger/r.json", contents="{}", author=ChangeAuthor.AGENT),)
    )
    assert ChangeGuard().check(changeset)


# -- doctor --------------------------------------------------------------------------


def test_doctor_fails_without_an_attested_spend_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """GATE-COST-5. The per-day ceiling is gone; this is what notices."""
    from in_lockstep import doctor

    monkeypatch.delenv("IN_LOCKSTEP_ORG_SPEND_LIMIT", raising=False)
    report = doctor.run(".")
    assert any(c.code == "DOC101" for c in report.errors)


def test_doctor_records_an_attestation_as_an_attestation(monkeypatch: pytest.MonkeyPatch) -> None:
    from in_lockstep import doctor

    monkeypatch.setenv("IN_LOCKSTEP_ORG_SPEND_LIMIT", "500")
    report = doctor.run(".")
    note = next(c for c in report.checks if c.code == "DOC102")
    assert "not a verification" in note.hint


def test_gate_cfg_2_doctor_refuses_a_review_with_no_base_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    from in_lockstep import doctor

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITLAB_CI", raising=False)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    report = doctor.run(".")
    assert any(c.code == "DOC110" for c in report.errors)


def test_gate_cfg_2_engages_on_a_gitlab_merge_request_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check once read GITHUB_* directly, so a GitLab MR pipeline passed silently —
    configuration loading from the ref under review, with nothing reporting it."""
    from in_lockstep import doctor

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setenv("CI_PIPELINE_SOURCE", "merge_request_event")
    monkeypatch.delenv("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", raising=False)
    report = doctor.run(".")
    assert any(c.code == "DOC110" for c in report.errors)


def test_gate_cfg_2_stays_quiet_outside_any_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("GITHUB_ACTIONS", "GITLAB_CI", "GITHUB_EVENT_NAME"):
        monkeypatch.delenv(var, raising=False)
    from in_lockstep import doctor

    report = doctor.run(".")
    assert not any(c.code == "DOC110" for c in report.checks)


def test_doctor_warns_about_pull_request_target(monkeypatch: pytest.MonkeyPatch) -> None:
    from in_lockstep import doctor

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITLAB_CI", raising=False)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    report = doctor.run(".")
    assert any(c.code == "DOC111" for c in report.checks)


def _write_lifecycle(tmp_path: Path, body: str) -> None:
    (tmp_path / ".lockstep").mkdir()
    (tmp_path / ".lockstep" / "lockstep.py").write_text(body)


def _not_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor's route check loads config from the trusted BASE ref when it detects CI — correct
    in a real PR pipeline, but these tests point doctor at a bare tmp_path that has no `main` to
    resolve. Cleared here so the check loads the working-tree module the test actually wrote;
    without this the load raises UnresolvableConfigRef and the route check is silently skipped
    (which is why these pass on a laptop and failed only on a GitHub runner)."""
    for var in ("GITHUB_ACTIONS", "GITLAB_CI", "GITHUB_EVENT_NAME", "GITHUB_BASE_REF"):
        monkeypatch.delenv(var, raising=False)


def test_doctor_flags_a_route_to_an_unregistered_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from in_lockstep import doctor

    _not_in_ci(monkeypatch)
    _write_lifecycle(
        tmp_path,
        "from in_lockstep import Lockstep\n"
        "lockstep = Lockstep()\n"
        "lockstep.models.route('review', 'nope:some-model')\n",
    )
    report = doctor.run(tmp_path)
    assert any(c.code == "DOC150" for c in report.checks)


def test_doctor_flags_an_unpriced_route_before_a_run_pays_for_the_lesson(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from in_lockstep import doctor

    _not_in_ci(monkeypatch)
    _write_lifecycle(
        tmp_path,
        "from in_lockstep import Lockstep\n"
        "lockstep = Lockstep()\n"
        "lockstep.models.route('review', 'anthropic:acme-finetune')\n",
    )
    report = doctor.run(tmp_path)
    assert any(c.code == "DOC151" for c in report.checks)


def test_doctor_flags_a_route_to_a_model_registered_without_structured_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GATE-MODEL-1, at the place a person sees it before anything is spent. The shipped
    registrations all declare the capability, so the one that does not is constructed here --
    which is also the honest statement of what the check covers: the operator's declaration."""
    from in_lockstep import doctor
    from in_lockstep.ai import bootstrap
    from in_lockstep.llm.interface import DataPolicy, ProviderSettings
    from in_lockstep.llm.registry import ModelCaps, ProviderRegistry

    def _registry(*args: object, **kwargs: object) -> ProviderRegistry:
        registry = ProviderRegistry()
        registry.register(
            "tiny",
            lambda settings, creds: None,  # type: ignore[arg-type,return-value]
            settings=ProviderSettings(base_url="http://localhost:8080"),
            data_policy=DataPolicy.INTERNAL,
            endpoint="http://localhost:8080",
            caps=ModelCaps(structured_output=False),
            free=True,
        )
        return registry

    monkeypatch.setattr(bootstrap, "default_registry", _registry)
    _not_in_ci(monkeypatch)
    _write_lifecycle(
        tmp_path,
        "from in_lockstep import Lockstep\n"
        "lockstep = Lockstep()\n"
        "lockstep.models.route('review', 'tiny:t')\n",
    )
    report = doctor.run(tmp_path)
    flagged = [c for c in report.checks if c.code == "DOC152"]
    assert flagged and "tiny:t" in flagged[0].message, [c.code for c in report.checks]
    assert not any(c.code == "DOC151" for c in report.checks), "capability is the prior question"


def test_doctor_accepts_a_route_to_a_free_local_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dogfood config routes triage to local:qwen3-8b; doctor must not call that a problem."""
    from in_lockstep import doctor

    _not_in_ci(monkeypatch)
    _write_lifecycle(
        tmp_path,
        "from in_lockstep import Lockstep\n"
        "lockstep = Lockstep()\n"
        "lockstep.models.route('triage', 'local:qwen3-8b')\n",
    )
    report = doctor.run(tmp_path)
    assert not any(c.code in ("DOC150", "DOC151") for c in report.checks)


def test_doctor_strict_reads_the_loader_location_not_the_deprecated_root(tmp_path: Path) -> None:
    from in_lockstep import doctor

    (tmp_path / ".lockstep").mkdir()
    (tmp_path / ".lockstep" / "lockstep.py").write_text("lockstep = None\n")
    report = doctor.run(tmp_path, strict=True)
    assert not any(c.code in ("DOC160", "DOC161") for c in report.checks)


def test_doctor_strict_flags_a_legacy_root_module(tmp_path: Path) -> None:
    from in_lockstep import doctor

    (tmp_path / "lockstep.py").write_text("lockstep = None\n")
    report = doctor.run(tmp_path, strict=True)
    assert any(c.code == "DOC161" for c in report.checks)
    assert not any(c.code == "DOC160" for c in report.checks)


def test_doctor_checks_every_shipped_prompt_body_exists() -> None:
    from in_lockstep import doctor

    report = doctor.run(".")
    assert not any(c.code == "DOC140" for c in report.checks), "shipped bodies must resolve"


# -- GATE-TESTGUARD-1: R1-QA-1's second half --------------------------------------------------
#
# `fix` asked to make CI green has an obvious shortcut, and `fix/*` strategies make it reachable:
# delete the failing test, or mark it skip. Neither is expressible as a path rule — tests must
# stay writable, which is why no tier lists them — so this is a rule about the shape of a change.


def _agent_change(path: str, contents: str | None = "x = 1") -> ChangeSet:
    return ChangeSet(changes=(FileChange(path=path, contents=contents, author=ChangeAuthor.AGENT),))


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_orders.py",
        "test_orders.py",
        "src/orders_test.py",
        "tests/unit/helpers.py",
        "web/checkout.test.ts",
        "web/checkout.spec.tsx",
        "internal/orders_test.go",
        "src/OrderTest.java",
        "spec/order_spec.rb",
    ],
)
def test_gate_testguard_1_deleting_a_test_without_a_ticket_is_refused(path: str) -> None:
    refusals = ChangeGuard().check(_agent_change(path, contents=None))
    assert [r.rule for r in refusals] == ["test-deleted-without-ticket"], path


def test_deleting_a_test_with_a_ticket_is_allowed() -> None:
    """The rule turns silencing a test into something a person signed, not something forbidden."""
    changeset = ChangeSet(
        changes=(FileChange(path="tests/test_x.py", contents=None, author=ChangeAuthor.AGENT),),
        ticket="PROJ-12",
    )
    assert ChangeGuard().check(changeset) == []


def test_deleting_ordinary_source_is_not_this_rule() -> None:
    assert ChangeGuard().check(_agent_change("src/orders.py", contents=None)) == []


@pytest.mark.parametrize(
    "marker",
    [
        "@pytest.mark.skip\ndef test_x(): ...",
        "@pytest.mark.xfail\ndef test_x(): ...",
        "def test_x():\n    pytest.skip('later')",
        "@unittest.skip('flaky')\ndef test_x(): ...",
        "it.skip('works', () => {})",
        "xit('works', () => {})",
        "func TestX(t *testing.T) { t.Skip() }",
        "#[ignore]\nfn test_x() {}",
        "@Disabled\nvoid testX() {}",
    ],
)
def test_silencing_a_test_without_a_ticket_is_refused(marker: str) -> None:
    refusals = ChangeGuard().check(_agent_change("tests/test_x.py", contents=marker))
    assert [r.rule for r in refusals] == ["test-silenced-without-ticket"], marker


def test_a_skip_that_was_already_there_is_not_an_addition() -> None:
    """The rule is about what this change did, not about what the file contains."""
    before = "@pytest.mark.skip\ndef test_x(): ...\n"
    after = before + "\ndef test_y():\n    assert True\n"

    guard = ChangeGuard()
    changeset = _agent_change("tests/test_x.py", contents=after)

    assert guard.check(changeset, read=lambda p: before) == []
    # And without a reader it fails closed, which is the documented fallback.
    assert [r.rule for r in guard.check(changeset)] == ["test-silenced-without-ticket"]


def test_a_human_authored_change_is_not_this_rule() -> None:
    """The guard runs over agent-authored entries. A person deleting a test is a person's call."""
    changeset = ChangeSet(
        changes=(FileChange(path="tests/test_x.py", contents=None, author=ChangeAuthor.FRAMEWORK),)
    )
    assert ChangeGuard().check(changeset) == []


def test_writing_a_new_test_is_untouched() -> None:
    """Writing tests is a core feature; a guard that made it harder would be the wrong trade."""
    assert ChangeGuard().check(_agent_change("tests/test_new.py", "def test_x():\n    assert 1\n")) == []


# -- GATE-APPROVAL-1: refused at startup, not at call time ------------------------------------


def _ai_writer() -> type[Any]:
    from in_lockstep.core.verbs import Capability, Verb

    class AiImplement:
        verb = Verb.IMPLEMENT
        capabilities = frozenset({Capability.SPENDS_BUDGET, Capability.WRITES_FILES})

        async def invoke(self, ctx, inp):  # pragma: no cover - never reached
            raise AssertionError

    return AiImplement


def test_gate_approval_1_a_model_that_can_write_needs_an_approval_path() -> None:
    from in_lockstep.core.spend import Budget
    from in_lockstep.core.verbs import UngatedAgency
    from in_lockstep.lockstep import Lockstep

    class Implement: ...

    lockstep = Lockstep.detect()
    lockstep.budget = Budget(usd=1.0)
    lockstep.bind(Implement, _ai_writer()())

    with pytest.raises(UngatedAgency) as exc:
        lockstep.context(run_id="r")
    assert "AiImplement" in str(exc.value)
    assert "ApprovalGate" in str(exc.value)


def test_an_approval_gate_in_the_chain_satisfies_it() -> None:
    from in_lockstep.core.spend import Budget
    from in_lockstep.lockstep import Lockstep
    from in_lockstep.middleware import ApprovalGate

    class Implement: ...

    lockstep = Lockstep.detect()
    lockstep.budget = Budget(usd=1.0)
    lockstep.bind(Implement, _ai_writer()())
    lockstep.middleware += [ApprovalGate()]
    assert lockstep.context(run_id="r") is not None


def test_a_house_gate_satisfies_it_by_declaring_so() -> None:
    """Declared, not recognised by class.

    An organisation routing approvals through its own system of record has satisfied the
    requirement, and an isinstance check would tell it that it had not.
    """
    from in_lockstep.core.spend import Budget
    from in_lockstep.lockstep import Lockstep

    class OurApprovals:
        provides_approval = True

        async def __call__(self, ctx, call, next):  # pragma: no cover - never invoked
            return await next()

    class Implement: ...

    lockstep = Lockstep.detect()
    lockstep.budget = Budget(usd=1.0)
    lockstep.bind(Implement, _ai_writer()())
    lockstep.middleware += [OurApprovals()]
    assert lockstep.context(run_id="r") is not None


def test_running_tests_does_not_need_approval() -> None:
    """PytestTest declares EXECUTES_CODE and means it.

    The gate's literal wording would refuse every repository that runs its own suite, and a
    control everybody switches off is not a control. Sandbox is the answer there; approval is the
    answer when a MODEL is choosing.
    """
    from in_lockstep.adapters import PytestTest
    from in_lockstep.adapters.pytest_adapter import Test
    from in_lockstep.lockstep import Lockstep

    lockstep = Lockstep.detect()
    lockstep.bind(Test, PytestTest())
    assert lockstep.context(run_id="r") is not None


def test_a_read_only_ai_verb_does_not_need_approval() -> None:
    """Reviewing spends money and writes nothing. Nothing shipped today trips this."""
    from in_lockstep.core.spend import Budget
    from in_lockstep.core.verbs import Capability, Verb
    from in_lockstep.lockstep import Lockstep

    class AiReviewLike:
        verb = Verb.REVIEW
        capabilities = frozenset({Capability.SPENDS_BUDGET, Capability.READS_REPO})

        async def invoke(self, ctx, inp):  # pragma: no cover - never reached
            raise AssertionError

    class Review: ...

    lockstep = Lockstep.detect()
    lockstep.budget = Budget(usd=1.0)
    lockstep.bind(Review, AiReviewLike())
    assert lockstep.context(run_id="r") is not None


# -- doctor --strict: the org baseline (item 19) ---------------------------------------


def _baseline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No inherited baseline: these tests assert exactly what they set."""
    _not_in_ci(monkeypatch)
    for var in ("IN_LOCKSTEP_REQUIRED_POLICIES", "IN_LOCKSTEP_MAX_BUDGET_USD", "IN_LOCKSTEP_MAX_TURNS"):
        monkeypatch.delenv(var, raising=False)


def test_strict_errors_on_a_missing_required_policy_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The KISS answer to the Tier.MANDATE debate: a deleted standard is a visible diff, and the
    required check the organisation controls is what sees it."""
    from in_lockstep import doctor

    _baseline_env(monkeypatch)
    monkeypatch.setenv("IN_LOCKSTEP_REQUIRED_POLICIES", "org-floor, sec-base")
    _write_lifecycle(
        tmp_path,
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.core.policy import Policy\n"
        "lockstep = Lockstep()\n"
        "lockstep.contribute(Policy(name='org-floor', source='acme'))\n",
    )
    report = doctor.run(tmp_path, strict=True)
    missing = [c for c in report.errors if c.code == "DOC162"]
    assert len(missing) == 1, "org-floor is present; only sec-base is missing"
    assert "sec-base" in missing[0].message


def test_strict_without_a_stated_baseline_adds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from in_lockstep import doctor

    _baseline_env(monkeypatch)
    _write_lifecycle(tmp_path, "from in_lockstep import Lockstep\nlockstep = Lockstep()\n")
    report = doctor.run(tmp_path, strict=True)
    assert not any(c.code in ("DOC162", "DOC163") for c in report.checks)


def test_strict_errors_when_the_budget_exceeds_the_org_maximum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from in_lockstep import doctor

    _baseline_env(monkeypatch)
    monkeypatch.setenv("IN_LOCKSTEP_MAX_BUDGET_USD", "1.00")
    _write_lifecycle(
        tmp_path,
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.core.spend import Budget\n"
        "lockstep = Lockstep()\n"
        "lockstep.budget = Budget(usd=2.00)\n",
    )
    report = doctor.run(tmp_path, strict=True)
    assert any(c.code == "DOC163" and "$2.00 exceeds" in c.message for c in report.errors)


def test_strict_errors_when_the_org_caps_and_no_budget_is_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent ceiling is not a compliant ceiling."""
    from in_lockstep import doctor

    _baseline_env(monkeypatch)
    monkeypatch.setenv("IN_LOCKSTEP_MAX_BUDGET_USD", "1.00")
    _write_lifecycle(tmp_path, "from in_lockstep import Lockstep\nlockstep = Lockstep()\n")
    report = doctor.run(tmp_path, strict=True)
    assert any(c.code == "DOC163" and "declares no budget" in c.message for c in report.errors)


def test_strict_accepts_a_budget_at_or_under_the_org_maximum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from in_lockstep import doctor

    _baseline_env(monkeypatch)
    monkeypatch.setenv("IN_LOCKSTEP_MAX_BUDGET_USD", "1.00")
    _write_lifecycle(
        tmp_path,
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.core.spend import Budget\n"
        "lockstep = Lockstep()\n"
        "lockstep.budget = Budget(usd=0.25)\n",
    )
    report = doctor.run(tmp_path, strict=True)
    assert not any(c.code == "DOC163" for c in report.checks)


def test_strict_errors_when_the_turn_ceiling_is_unbounded_or_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from in_lockstep import doctor

    _baseline_env(monkeypatch)
    monkeypatch.setenv("IN_LOCKSTEP_MAX_TURNS", "20")
    _write_lifecycle(tmp_path, "from in_lockstep import Lockstep\nlockstep = Lockstep()\n")
    report = doctor.run(tmp_path, strict=True)
    assert any(c.code == "DOC163" and "unbounded" in c.message for c in report.errors)

    (tmp_path / ".lockstep" / "lockstep.py").write_text(
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.core.policy import Policy\n"
        "lockstep = Lockstep()\n"
        "lockstep.contribute(Policy(name='floor', max_turns=12))\n"
    )
    report = doctor.run(tmp_path, strict=True)
    assert not any(c.code == "DOC163" for c in report.checks), "12 <= 20 complies"


def test_strict_names_the_egress_opt_out_where_the_fleet_looks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Visibility, not impossibility: the greppable line in the diff becomes a named finding in
    the check an organisation requires."""
    from in_lockstep import doctor

    _baseline_env(monkeypatch)
    _write_lifecycle(
        tmp_path,
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.privileged.egress import EgressPolicy, UnsandboxedEgress\n"
        "lockstep = Lockstep()\n"
        "lockstep.bind(EgressPolicy, UnsandboxedEgress())\n",
    )
    assert any(c.code == "DOC165" for c in doctor.run(tmp_path, strict=True).checks)
    assert not any(c.code == "DOC165" for c in doctor.run(tmp_path).checks), "strict-only"


def test_strict_names_unsandboxed_run_even_inside_a_worktree_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from in_lockstep import doctor

    _baseline_env(monkeypatch)
    _write_lifecycle(
        tmp_path,
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.adapters.sandbox import UnsandboxedRun\n"
        "from in_lockstep.adapters.worktree import WorktreeRunner\n"
        "lockstep = Lockstep()\n"
        "class Verb: pass\n"
        "class Adapter:\n"
        "    def __init__(self):\n"
        "        self.commands = WorktreeRunner(UnsandboxedRun(), '.')\n"
        "lockstep.bind(Verb, Adapter())\n",
    )
    report = doctor.run(tmp_path, strict=True)
    assert any(c.code == "DOC166" and "Verb" in c.message for c in report.checks)


def test_strict_errors_when_a_spending_writing_adapter_has_no_approval_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Lockstep.context` refuses this at run time; the required check says so before a trigger
    finds out."""
    from in_lockstep import doctor

    _baseline_env(monkeypatch)
    body = (
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.core.verbs import Capability\n"
        "lockstep = Lockstep()\n"
        "class Verb: pass\n"
        "class Adapter:\n"
        "    capabilities = frozenset({Capability.SPENDS_BUDGET, Capability.WRITES_FILES})\n"
        "lockstep.bind(Verb, Adapter())\n"
    )
    _write_lifecycle(tmp_path, body)
    report = doctor.run(tmp_path, strict=True)
    assert any(c.code == "DOC164" for c in report.errors)

    (tmp_path / ".lockstep" / "lockstep.py").write_text(
        body + "from in_lockstep.middleware.approval import ApprovalGate\n"
        "lockstep.middleware += [ApprovalGate()]\n"
    )
    report = doctor.run(tmp_path, strict=True)
    assert not any(c.code == "DOC164" for c in report.checks)


def test_doctor_format_json_is_the_fleet_scanners_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json as _json

    from click.testing import CliRunner

    from in_lockstep.cli import main

    _baseline_env(monkeypatch)
    monkeypatch.delenv("IN_LOCKSTEP_ORG_SPEND_LIMIT", raising=False)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["doctor", "--format", "json"])
    payload = _json.loads(result.output)
    assert payload["ok"] is False and payload["errors"] >= 1
    assert any(c["code"] == "DOC101" for c in payload["checks"])
    assert all({"code", "severity", "message"} <= set(c) for c in payload["checks"])
    assert result.exit_code != 0, "the exit code is the same contract in both formats"


# -- doctor: the escalation labels are a control -------------------------------------------------


def _repo_with_the_loop_wired(tmp_path: Path, *, labels: tuple[str, ...]) -> Path:
    """A repository whose trampoline routes on `ai-generated`, plus a `gh` that lists `labels`."""
    (tmp_path / ".git").mkdir()
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "lockstep-ai-generated.yml").write_text(
        "on:\n  issues:\n    types: [opened, labeled]\njobs:\n  fix:\n"
        "    if: github.event.label.name == 'ai-generated'\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    listing = "\n".join(labels)
    gh.write_text(
        f"#!/bin/sh\ncase \"$*\" in\n  *labels*) printf '%s\\n' '{listing}' ;;\n  *) exit 1 ;;\nesac\n"
    )
    gh.chmod(0o755)
    return bin_dir


def test_doctor_reports_a_missing_ai_generated_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """The label is the trigger AND the authorization, so its absence is not cosmetic.

    Without it a failed run pays for a model call and then cannot file the follow-up, and nothing
    would route to the fixing verb even if it could.
    """
    from in_lockstep import doctor

    bin_dir = _repo_with_the_loop_wired(tmp_path, labels=("bug", "enhancement"))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    report = doctor.run(str(tmp_path))
    finding = next(c for c in report.errors if c.code == "DOC123")
    assert "lockstep-ai-generated.yml" in finding.message
    assert "gh label create ai-generated" in finding.hint


def test_doctor_reports_missing_attempt_labels_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """One per attempt the cap allows, because `escalate` names the label after the number."""
    from in_lockstep import doctor

    bin_dir = _repo_with_the_loop_wired(tmp_path, labels=("ai-generated", "ai-attempt-1"))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    report = doctor.run(str(tmp_path))
    assert not any(c.code == "DOC123" for c in report.checks), "the routing label is present"
    finding = next(c for c in report.errors if c.code == "DOC124")
    assert "ai-attempt-2" in finding.message and "ai-attempt-3" in finding.message
    assert "ai-attempt-1" not in finding.message
    assert "stops bounding anything" in finding.hint


def test_doctor_is_quiet_when_every_escalation_label_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    from in_lockstep import doctor

    bin_dir = _repo_with_the_loop_wired(
        tmp_path, labels=("ai-generated", "ai-attempt-1", "ai-attempt-2", "ai-attempt-3")
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    report = doctor.run(str(tmp_path))
    assert not any(c.code in ("DOC123", "DOC124") for c in report.checks)


def test_doctor_says_nothing_to_a_repository_that_never_wired_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """A finding invented for a hook nobody asked for is how a code teaches people to ignore it."""
    from in_lockstep import doctor

    bin_dir = _repo_with_the_loop_wired(tmp_path, labels=("bug",))
    (tmp_path / ".github" / "workflows" / "lockstep-ai-generated.yml").unlink()
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    report = doctor.run(str(tmp_path))
    assert not any(c.code in ("DOC122", "DOC123", "DOC124") for c in report.checks)


# -- the framework has to lose no race it needs evidence from ------------------------------------


def test_every_model_job_outlives_the_session_deadline_it_runs() -> None:
    """A job timeout skips the steps after it, so the framework must be what stops first.

    Both numbers were 30 minutes: `deadline_seconds=1800` on the bindings in
    `.lockstep/lockstep.py` and `timeout-minutes: 30` on the jobs that run them. A session that
    actually reached its deadline raced the host to end the job, and when the host won,
    `history --bundle` and the artifact upload never ran — so the run that most needed a record was
    exactly the one that lost it.

    Scoped to jobs that dispatch a WORKFLOW (`in-lockstep run ...`) while holding the provider
    extra, because those are precisely the sessions whose deadline this module declares. The
    review job in lockstep.yml is a different mechanism — `review` builds its own policy in the
    CLI, with a much shorter deadline — and asserting it here would need a second source of truth
    to be honest about. Its own headroom is 20 minutes against 5.
    """
    import yaml

    from in_lockstep import Workshop

    root = Path(__file__).resolve().parents[2]
    module = (root / ".lockstep" / "lockstep.py").read_text()
    # Two sources, and the module may use either. `Workshop` carries the default every strategy
    # `use()` completes inherits; an explicit `deadline_seconds=` on a constructor overrides it for
    # that one strategy. The longest session any of them may run is what a job has to outlive.
    declared = {int(m) for m in re.findall(r"deadline_seconds=(\d+)", module)}
    longest_session_minutes = max({Workshop().deadline_seconds, *declared}) / 60
    assert longest_session_minutes > 0, "no session deadline anywhere; nothing to compare against"

    checked = []
    for path in sorted((root / ".github" / "workflows").glob("*.yml")):
        jobs = (yaml.safe_load(path.read_text()) or {}).get("jobs") or {}
        for name, job in jobs.items():
            runs = [str(step.get("run", "")) for step in (job.get("steps") or [])]
            # The provider extra marks the half that can reach a model; `run <id>` marks a session
            # this module's bindings govern. Both, or the job is somebody else's problem.
            if not any("--extra anthropic" in r for r in runs):
                continue
            if not any("in-lockstep run " in r for r in runs):
                continue
            checked.append(f"{path.name}:{name}")
            timeout = job.get("timeout-minutes")
            assert timeout is not None, f"{path.name}:{name} has no timeout-minutes"
            assert timeout > longest_session_minutes, (
                f"{path.name}:{name} allows {timeout} minutes, and a session may run for "
                f"{longest_session_minutes}. The host would end the job before the framework "
                f"could write its record."
            )
    assert len(checked) >= 3, f"expected every model-dispatching job to be checked, saw {checked}"


# -- doctor: a propose job that cannot propose ---------------------------------------------------


def _repo_that_opens_changes(tmp_path: Path, *, payload: str) -> Path:
    """A repository with a propose job, and a `gh` that answers with `payload`."""
    (tmp_path / ".git").mkdir()
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "lockstep-implement.yml").write_text(
        "jobs:\n  propose:\n    permissions:\n      contents: write\n"
        "      pull-requests: write\n      issues: write\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f"  *permissions/workflow*) printf '%s' '{payload}' ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    gh.chmod(0o755)
    return bin_dir


def test_doctor_reports_that_actions_may_not_open_a_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """The setting that let a paid run do all its work and die on its last call.

    `fix/propose` pushed its branch, then failed with `GitHub Actions is not permitted to create or
    approve pull requests` — after the unprivileged half had already spent $9.62 on a model. The
    configuration, the credentials and the change were all fine.
    """
    from in_lockstep import doctor

    bin_dir = _repo_that_opens_changes(tmp_path, payload='{"can_approve_pull_request_reviews": false}')
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    report = doctor.run(str(tmp_path))
    finding = next(c for c in report.errors if c.code == "DOC126")
    assert "lockstep-implement.yml" in finding.message
    assert "Allow GitHub Actions to create and approve pull requests" in finding.hint


def test_doctor_is_quiet_when_actions_may_open_a_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    from in_lockstep import doctor

    bin_dir = _repo_that_opens_changes(tmp_path, payload='{"can_approve_pull_request_reviews": true}')
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    report = doctor.run(str(tmp_path))
    assert not any(c.code in ("DOC125", "DOC126") for c in report.checks)


def test_doctor_does_not_read_a_missing_field_as_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """Absent is unknown, not false. A host that stops reporting this must not manufacture an
    error about a setting nobody can see."""
    from in_lockstep import doctor

    bin_dir = _repo_that_opens_changes(tmp_path, payload='{"default_workflow_permissions": "write"}')
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    report = doctor.run(str(tmp_path))
    assert not any(c.code == "DOC126" for c in report.checks)
    assert any(c.code == "DOC125" and c.severity is doctor.Severity.NOTE for c in report.checks)


def test_doctor_says_nothing_to_a_repository_that_opens_no_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """A repository whose trampolines only review never reaches `open_change`."""
    from in_lockstep import doctor

    bin_dir = _repo_that_opens_changes(tmp_path, payload='{"can_approve_pull_request_reviews": false}')
    (tmp_path / ".github" / "workflows" / "lockstep-implement.yml").write_text(
        "jobs:\n  review:\n    permissions:\n      contents: read\n"
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    report = doctor.run(str(tmp_path))
    assert not any(c.code in ("DOC125", "DOC126") for c in report.checks)


def test_gate_guard_1_tightening_the_path_policy_cannot_weaken_it() -> None:
    """Discipline #2, at the most security-relevant point in the codebase.

    `PathPolicy` is a dataclass whose `deny_always` field invites exactly this — an organisation
    adding its own protected prefix to the shipped set. The tier-1 basename and suffix rules used
    to be gated on `prefixes is DENY_ALWAYS`, an identity test, so building a new tuple silently
    stopped protecting `conftest.py`, `*.pem` and `.env*`: adding a rule removed four. No test
    covered a customised policy, which is why it survived.
    """
    from in_lockstep.core.changes import DENY_ALWAYS, PathPolicy

    shipped = ChangeGuard(PathPolicy())
    tightened = ChangeGuard(PathPolicy(deny_always=DENY_ALWAYS + ("infra/",)))

    for path in ("tests/conftest.py", "keys/server.pem", ".env.production", "pkg/sitecustomize.py"):
        assert shipped.check_path(path) is not None, f"the shipped policy stopped protecting {path}"
        assert tightened.check_path(path) is not None, (
            f"adding a prefix to deny_always stopped protecting {path} — tightening must not weaken"
        )

    assert tightened.check_path("infra/main.tf") is not None, "the added prefix does apply"
    assert shipped.check_path("infra/main.tf") is None, "and it is genuinely an addition"
    assert tightened.check_path("src/app.py") is None, "an ordinary path is still writable"


# -- DOC168: what is recorded, and whether git would commit it ---------------------------------


def _repo_holding_a_recording(root: Path, *, ignored: bool) -> Path:
    """A git repository with one cassette on disk, ignored or not as asked."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / ".lockstep" / "cassettes").mkdir(parents=True)
    (root / ".lockstep" / "cassettes" / "review.json").write_text('{"provider_calls": {}}')
    if ignored:
        (root / ".gitignore").write_text(".lockstep/cassettes/\n")
    return root


def test_doc168_says_what_is_recorded_when_git_is_ignoring_it(tmp_path: Path) -> None:  # noqa: ANN001
    """A note, not a finding: recordings in the directory the CLI writes to, out of git's way."""
    from in_lockstep import doctor

    report = doctor.run(str(_repo_holding_a_recording(tmp_path, ignored=True)))
    found = [c for c in report.checks if c.code == "DOC168"]
    assert found, "doctor said nothing about a recording on disk"
    assert found[0].severity is doctor.Severity.NOTE
    assert "1 recording" in found[0].message


def test_doc168_warns_when_a_recording_is_not_ignored(tmp_path: Path) -> None:  # noqa: ANN001
    """The case worth catching. A cassette holds the whole composed prompt and the whole diff, so
    a commit from that tree publishes both -- and redaction masks credentials, not source."""
    from in_lockstep import doctor

    report = doctor.run(str(_repo_holding_a_recording(tmp_path, ignored=False)))
    found = [c for c in report.checks if c.code == "DOC168"]
    assert found, "doctor said nothing about a recording git would commit"
    assert found[0].severity is doctor.Severity.WARNING
    assert "NOT ignoring" in found[0].message


def test_doc168_never_fails_a_run(tmp_path: Path) -> None:  # noqa: ANN001
    """Recording is on by default now, and a default must not turn every doctor exit non-zero."""
    from in_lockstep import doctor

    report = doctor.run(str(_repo_holding_a_recording(tmp_path, ignored=False)))
    assert not [c for c in report.errors if c.code == "DOC168"]


def test_doc168_is_silent_when_nothing_was_recorded(tmp_path: Path) -> None:  # noqa: ANN001
    """A note about an empty directory is noise, and noise is how a report stops being read."""
    from in_lockstep import doctor

    (tmp_path / ".lockstep" / "cassettes").mkdir(parents=True)
    assert not [c for c in doctor.run(str(tmp_path)).checks if c.code == "DOC168"]


# -- doctor's verdict has to be worth acting on (GATE-CI-3, issue 249) --------------------------
#
# `continue-on-error: true` sat on all four of this repository's `doctor` steps, so no run had
# ever been stopped by a control it reported missing. The reason it had to be there is this check:
# a CI job's token cannot read the branch-protection API, every non-zero `gh` exit was an ERROR,
# and so a fully protected `main` was reported as unprotected on every single run. A diagnostic
# that cries wolf gets its verdict discarded, and then gates nothing at all.


def _looks_like_a_repo(tmp_path: Path) -> Path:
    """A root `_branch_protection` will not decline. It returns early without a `.git`, so these
    tests were reading the AMBIENT directory and passing because the suite runs from inside this
    repository -- the same shape as CLAUDE.md's rule about a `git` call that inherits its working
    directory. Run over a copy of the working tree, which has no `.git`, all four went red."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    return tmp_path


def _protection_says(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    branch_rc: int = 0,
    branch_out: str = "",
    name: str = "main",
) -> doctor.Report:
    """Drive `_branch_protection` with a stubbed `gh`, keyed on which subcommand is called."""
    import subprocess

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["gh", "repo"]:
            return subprocess.CompletedProcess(argv, 0 if name else 1, name, "")
        return subprocess.CompletedProcess(argv, branch_rc, "", branch_out)

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = doctor.Report()
    doctor._branch_protection(report, _looks_like_a_repo(tmp_path))
    return report


def _bypasses_say(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    protection: dict[str, Any],
    rulesets: list[dict[str, Any]],
) -> doctor.Report:
    """`_branch_protection` with a rule that reads, plus whatever rulesets the host lists."""
    import json
    import subprocess

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["gh", "repo"]:
            return subprocess.CompletedProcess(argv, 0, "main", "")
        target = argv[2]
        if target.endswith("/protection"):
            return subprocess.CompletedProcess(argv, 0, json.dumps(protection), "")
        if target.endswith("/rulesets"):
            return subprocess.CompletedProcess(argv, 0, json.dumps(rulesets), "")
        wanted = target.rsplit("/", 1)[-1]
        found = [r for r in rulesets if str(r.get("id")) == wanted]
        return subprocess.CompletedProcess(argv, 0 if found else 1, json.dumps(found[0]) if found else "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = doctor.Report()
    doctor._branch_protection(report, _looks_like_a_repo(tmp_path))
    return report


def test_an_admin_who_can_step_around_the_required_check_is_warned_about_by_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue 312. Both were true here: `enforce_admins` off on the classic rule, and the active
    ruleset carrying a bypass for the admin role -- so `required` was enforceable only by choice,
    and doctor said nothing. WARNINGs naming the setting, the way DOC121 names a missing rule."""
    report = _bypasses_say(
        monkeypatch,
        tmp_path,
        protection={"enforce_admins": {"enabled": False}},
        rulesets=[
            {
                "id": 21300993,
                "name": "Protect Main",
                "enforcement": "active",
                "rules": [{"type": "deletion"}, {"type": "pull_request"}],
                "bypass_actors": [
                    {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}
                ],
            },
            {
                "id": 7,
                "name": "Disabled",
                "enforcement": "disabled",
                "rules": [{"type": "pull_request"}],
                "bypass_actors": [{"actor_id": 1, "actor_type": "Integration"}],
            },
        ],
    )
    codes = {f.code: f for f in report.checks}
    assert codes["DOC127"].severity is doctor.Severity.WARNING
    assert "enforce_admins" in codes["DOC127"].message
    assert codes["DOC128"].severity is doctor.Severity.WARNING
    assert (
        "'Protect Main'" in codes["DOC128"].message
        and "RepositoryRole 5 (pull_request)" in codes["DOC128"].message
    )
    assert "pull_request" in codes["DOC128"].message
    assert [f.code for f in report.checks].count("DOC128") == 1, "a disabled ruleset is not a bypass"


def test_a_rule_enforced_for_everyone_and_a_ruleset_nobody_bypasses_warn_of_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = _bypasses_say(
        monkeypatch,
        tmp_path,
        protection={"enforce_admins": {"enabled": True}},
        rulesets=[
            {
                "id": 1,
                "name": "r",
                "enforcement": "active",
                "rules": [{"type": "pull_request"}],
                "bypass_actors": [],
            }
        ],
    )
    assert [f.code for f in report.checks] == []


def test_gate_ci_3_an_unprotected_default_branch_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The finding this check exists for, and the only answer that earns an ERROR."""
    report = _protection_says(
        monkeypatch,
        tmp_path,
        branch_rc=1,
        branch_out="gh: Branch not protected (HTTP 404)",
    )
    assert any(c.code == "DOC121" for c in report.errors)


def test_gate_ci_3_a_protection_api_that_cannot_be_read_is_a_note_not_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Absent is not zero, in the check that made this repository discard doctor's verdict.

    A job token gets `Resource not accessible by integration` for a branch that is fully
    protected. Reporting that as "the default branch has no protection rule" is a control being
    declared missing on the strength of not having looked.
    """
    report = _protection_says(
        monkeypatch, tmp_path, branch_rc=1, branch_out="gh: Resource not accessible by integration (HTTP 403)"
    )
    assert not any(c.code == "DOC121" for c in report.checks), "an unreadable API is not a verdict"
    note = next(c for c in report.checks if c.code == "DOC120")
    assert note.severity is not __import__("in_lockstep").doctor.Severity.ERROR
    assert "Resource not accessible" in note.hint, "what gh said is quoted, not paraphrased"


def test_gate_ci_3_a_protected_default_branch_reports_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The control: a check that fired on success too would be noise on every green run."""
    report = _protection_says(monkeypatch, tmp_path, branch_rc=0)
    assert not [c for c in report.checks if c.code in ("DOC120", "DOC121")]


def test_gate_ci_3_the_default_branch_is_asked_for_not_assumed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """This asked about `branches/main/protection` literally, so a repository whose default is
    `master` got `Branch not found` — and, under the old reporting, an ERROR about a rule it may
    well have had."""
    asked: list[str] = []
    import subprocess

    from in_lockstep import doctor

    def fake_run(argv, **kwargs):
        asked.append(" ".join(argv))
        if argv[:2] == ["gh", "repo"]:
            return subprocess.CompletedProcess(argv, 0, "master", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    doctor._branch_protection(doctor.Report(), _looks_like_a_repo(tmp_path))
    assert any("branches/master/protection" in a for a in asked), asked


# -- blocked is not a failure, in eight more places (issue 256) ---------------------------------
#
# `Status` distinguishes blocked (a control working), errored (infrastructure broke) and failed
# (the domain said no). Each of these collapsed that to a boolean, so a control firing was
# reported as the work going wrong -- in the sentence on a pull request, in the census `improve`
# reads, and in the number a pack is judged by.


def test_a_sandbox_that_refuses_is_blocked_in_every_command_adapter() -> None:
    """`CommandProvision` had the guard. The other four mapped exit 126 like any other failure,
    so a container runtime an adopter asked for and does not have read as "your tests failed"."""
    import asyncio
    import types

    from in_lockstep.adapters import command as c
    from in_lockstep.adapters.pytest_adapter import Test
    from in_lockstep.adapters.sandbox import Sandbox
    from in_lockstep.core.outcome import Status
    from in_lockstep.core.types import Build, Provision, Run, Validate

    sandbox = Sandbox(image="python:3.11-slim", require_container=True)
    # No runtime on PATH: the condition an adopter without a container engine is actually in.
    sandbox.runtime = lambda: None  # type: ignore[method-assign]
    ctx = types.SimpleNamespace(repo=types.SimpleNamespace(root="."))

    cases: list[tuple[Any, Any]] = [
        (c.CommandTest(["pytest"], sandbox=sandbox), Test(paths=())),
        (c.CommandValidate(["ruff", "check"], sandbox=sandbox), Validate(paths=())),
        (c.CommandBuild(["make", "build"], sandbox=sandbox), Build()),
        (c.CommandRun(["make", "run"], sandbox=sandbox), Run()),
        (c.CommandProvision([["uv", "sync"]], sandbox=sandbox), Provision()),
    ]
    for adapter, request in cases:
        outcome = asyncio.run(adapter.invoke(ctx, request))
        assert outcome.status is Status.BLOCKED, (
            f"{type(adapter).__name__} reported a refused sandbox as {outcome.status.value}"
        )


def test_a_blocked_review_does_not_say_no_findings_on_the_pull_request() -> None:
    """A sentence only a review that read the diff can earn. It was gated on `decided`, which
    defaults to True, so the killswitch path posted it."""
    from in_lockstep.core.outcome import Outcome
    from in_lockstep.platform.report import review_comment

    body = review_comment("security", Outcome.blocked_by("review.killswitch"))
    assert "No findings." not in body
    assert "Refused before it could review" in body


def test_a_control_refusal_is_not_rendered_as_a_finding_about_the_change() -> None:
    """`cost.budget_exceeded` rendered in the findings table beside real review findings, so a
    budget ceiling read as something the reviewer noticed about the code."""
    from in_lockstep.core.outcome import Finding, Outcome, Severity
    from in_lockstep.platform.report import review_comment

    refusal = Finding(
        id="cost.budget_exceeded",
        message="the run would exceed $0.75",
        severity=Severity.ERROR,
        blocking=True,
    )
    body = review_comment("security", Outcome.blocked_by("cost.budget_exceeded", findings=(refusal,)))
    assert "| | location | finding |" not in body, "a refusal was rendered in the findings table"
    assert "cost.budget_exceeded" in body and "A control stopped this run" in body


def test_a_blocked_run_decides_nothing() -> None:
    """`undecided_rate` counts `decided is False`, and every blocked outcome carried True — so a
    run whose judgement never happened was counted among the ones that reached a verdict."""
    from in_lockstep.core.outcome import Outcome

    assert Outcome.blocked_by("approval.required").decided is False
    assert Outcome.blocked_by("x", decided=True).decided is True, "a caller may still say otherwise"


def test_a_control_refusal_is_not_counted_among_what_the_reviewer_keeps_finding() -> None:
    """`improve --explain` listed `approval.required` under **trend**, as though a control firing
    were a recurring defect the prompt should be changed to avoid."""
    from in_lockstep.metrics import recurring

    records = [
        {
            "run_id": "r1",
            "status": "blocked",
            "findings": {"count": 1, "items": [{"id": "approval.required"}]},
        },
        {
            "run_id": "r2",
            "status": "succeeded",
            "findings": {"count": 1, "items": [{"id": "review.security"}]},
        },
    ]
    names = {row.finding for row in recurring(records)}
    assert "review.security" in names, "a real finding was dropped"
    assert "approval.required" not in names, "a control refusal was counted as a finding"


def test_a_ceiling_on_the_test_is_not_reported_as_a_test_that_disagreed() -> None:
    """`if status is not SUCCEEDED` is a two-way split over a six-member enum, so a budget ceiling
    on the third Test of a TDD run produced `tdd.not_green` about a suite that never ran."""
    from in_lockstep.adapters.ai.strategy import not_a_verdict
    from in_lockstep.core.outcome import Finding, Outcome, Severity, Status

    ceiling = Finding(id="cost.budget_exceeded", message="ceiling", severity=Severity.ERROR)
    blocked: Outcome[Any] = Outcome.blocked_by("cost.budget_exceeded", findings=(ceiling,))
    passed = not_a_verdict(blocked)
    assert passed is not None and passed.status is Status.BLOCKED
    assert passed.findings[0].id == "cost.budget_exceeded", "the refusal must travel as itself"

    # FAILED is exactly the case those findings ARE about: the suite ran and disagreed.
    assert not_a_verdict(Outcome(status=Status.FAILED)) is None
    assert not_a_verdict(Outcome(status=Status.SUCCEEDED)) is None
    assert not_a_verdict(Outcome(status=Status.ERRORED)) is not None


def test_a_pack_refused_by_a_control_is_not_a_pack_that_broke() -> None:
    """A restricted-residency or unpriced-model refusal read as `errored`, which is the number a
    pack is judged by."""
    from in_lockstep.trial import ERRORED, REFUSED

    assert REFUSED != ERRORED


# -- one cached answer counted thirty times (issue 259) -----------------------------------------


def _replay(run: str, finding: str = "review.security") -> dict[str, Any]:
    return {
        "run_id": run,
        "status": "succeeded",
        "cost_usd": 0.0,
        "billed_fraction": 0.0,
        "ts": f"2026-09-0{1 + int(run[-1]) % 9}T00:00:00Z",
        "findings": {"count": 1, "items": [{"id": finding}]},
    }


def _paid(run: str, finding: str = "review.security") -> dict[str, Any]:
    # No `billed_fraction`: this repository's eight paid records carry `cost_usd` and not that
    # field, which is exactly why reading one signal could not tell these populations apart.
    return {
        "run_id": run,
        "status": "succeeded",
        "cost_usd": 9.61,
        "ts": "2026-09-02T00:00:00Z",
        "findings": {"count": 1, "items": [{"id": finding}]},
    }


def test_a_trend_is_not_cleared_by_replaying_one_cassette() -> None:
    """`review.security` reached 30 of 52 runs here entirely from `review --offline` replaying one
    shipped cassette: the same composed prompt, the same two findings, $0.00. The census counted
    one cached answer thirty times as thirty independent judgments, and that census is what #163's
    learning loop would propose prompt changes from."""
    from in_lockstep.metrics import recurring

    records = [_replay(f"r{i}") for i in range(30)]
    trend = next(t for t in recurring(records) if t.finding == "review.security")

    assert trend.runs == 30, "the run count itself is honest and stays"
    assert trend.billed_runs == 0 and trend.replayed_runs == 30
    assert not trend.qualifies, "thirty replays of one answer cleared a threshold for five"


def test_a_trend_over_real_calls_still_qualifies() -> None:
    """The control. If billing were read as a filter rather than as the denominator, a real trend
    would stop qualifying too, and the census would be useless in the other direction."""
    from in_lockstep.metrics import recurring

    records = [_paid(f"p{i}") for i in range(5)]
    for i, record in enumerate(records):
        record["ts"] = f"2026-0{8 + i % 2}-0{1 + i}T00:00:00Z"
    trend = next(t for t in recurring(records) if t.finding == "review.security")
    assert trend.billed_runs == 5
    assert trend.qualifies, "five billed runs across two weeks is exactly what a trend is"


def test_a_record_carrying_no_billing_signal_is_neither_billed_nor_replayed() -> None:
    """Absent is not zero, and it is not one either. A record with neither field is its own
    bucket, because folding it into `replayed` would under-count real judgments and folding it
    into `billed` would let unmeasured records clear a threshold."""
    from in_lockstep.metrics import recurring

    records = [{"run_id": "u1", "status": "succeeded", "findings": {"count": 1, "items": [{"id": "x"}]}}]
    trend = next(t for t in recurring(records) if t.finding == "x")
    assert (trend.billed_runs, trend.replayed_runs, trend.unmeasured_runs) == (0, 0, 1)


def test_the_census_says_which_runs_were_paid_for() -> None:
    """A number nobody can see the population of is the thing this file exists to refuse."""
    from in_lockstep.metrics import as_trend_text, recurring

    text = "\n".join(as_trend_text(recurring([_replay("r1"), _replay("r2"), _paid("p1")])))
    assert "1 billed, 2 replayed" in text


def test_the_spend_report_does_not_average_a_field_the_paid_runs_lack() -> None:
    """`report` printed "actually billed 0%" beside a spend total of $275: the mean was taken over
    the records carrying `billed_fraction`, every one of them a replay reporting 0.0, while the
    runs that actually spent carried the field not at all."""
    from in_lockstep.metrics import as_text, build

    text = "\n".join(as_text(build([_replay("r1"), _replay("r2"), _paid("p1")])))
    assert "actually billed 0%" not in text
    assert "1 billed, 2 replayed" in text
