"""Phase-3 gates: the controls that replace what the substrate provided."""

from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest

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


class Thing:
    pass


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


def test_gate_egress_2_an_asserted_mode_a_probe_disproves_is_refused() -> None:
    """Fail-closed that can be satisfied by a lie is not fail-closed."""
    policy = EgressPolicy(mode=EgressMode.ENFORCED_EXTERNAL)
    policy._verified = False  # the probe reached the open internet
    with pytest.raises(EgressRefused) as exc:
        policy.check(capabilities=frozenset({Capability.WRITES_FILES}), untrusted_context=False)
    assert exc.value.reason == "egress.probe_failed"


def test_a_verified_mode_permits_the_run() -> None:
    policy = EgressPolicy(mode=EgressMode.ENFORCED_CONTAINER)
    policy._verified = True
    policy.check(capabilities=frozenset({Capability.WRITES_FILES}), untrusted_context=True)


def test_the_opt_out_is_named_after_what_it_does() -> None:
    """Greppable and reviewable, rather than a flag buried in an options object."""
    UnsandboxedEgress().check(capabilities=frozenset({Capability.EXECUTES_CODE}), untrusted_context=True)
    assert "Unsandboxed" in UnsandboxedEgress.__name__


def test_mode_is_read_from_the_environment(monkeypatch) -> None:
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
    outcome = asyncio.run(ctx.do(Thing, "x"))
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
    assert asyncio.run(ctx.do(Thing, "x")).succeeded
    assert adapter.calls == 1


# -- GATE-SANDBOX --------------------------------------------------------------------


def test_gate_sandbox_1_a_child_cannot_read_the_parents_credentials(monkeypatch) -> None:
    """pytest executes repository conftest.py; in-process it would reach live Credentials."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    result = asyncio.run(
        Sandbox().run(["python", "-c", "import os; print(os.environ.get('ANTHROPIC_API_KEY', 'ABSENT'))"])
    )
    assert "sk-must-not-leak" not in result.stdout
    assert "ABSENT" in result.stdout


def test_the_named_opt_out_does_leak_which_is_why_it_is_named(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leaks-here")
    result = asyncio.run(
        UnsandboxedRun().run(
            ["python", "-c", "import os; print(os.environ.get('ANTHROPIC_API_KEY', 'ABSENT'))"]
        )
    )
    assert "sk-leaks-here" in result.stdout
    assert result.how == "unsandboxed"


def test_the_fallback_says_it_is_not_a_kernel_sandbox() -> None:
    result = asyncio.run(Sandbox().run(["python", "-c", "print(1)"]))
    assert result.how == "subprocess:no-credentials"
    assert result.sandboxed is False, "honest about what it is"


def test_a_sandboxed_command_that_hangs_is_killed() -> None:
    result = asyncio.run(Sandbox().run(["python", "-c", "import time; time.sleep(30)"], timeout=0.5))
    assert result.exit_code == 124


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


def test_doctor_fails_without_an_attested_spend_limit(monkeypatch) -> None:
    """GATE-COST-5. The per-day ceiling is gone; this is what notices."""
    from in_lockstep import doctor

    monkeypatch.delenv("IN_LOCKSTEP_ORG_SPEND_LIMIT", raising=False)
    report = doctor.run(".")
    assert any(c.code == "DOC101" for c in report.errors)


def test_doctor_records_an_attestation_as_an_attestation(monkeypatch) -> None:
    from in_lockstep import doctor

    monkeypatch.setenv("IN_LOCKSTEP_ORG_SPEND_LIMIT", "500")
    report = doctor.run(".")
    note = next(c for c in report.checks if c.code == "DOC102")
    assert "not a verification" in note.hint


def test_gate_cfg_2_doctor_refuses_a_review_with_no_base_ref(monkeypatch) -> None:
    from in_lockstep import doctor

    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    report = doctor.run(".")
    assert any(c.code == "DOC110" for c in report.errors)


def test_doctor_warns_about_pull_request_target(monkeypatch) -> None:
    from in_lockstep import doctor

    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    report = doctor.run(".")
    assert any(c.code == "DOC111" for c in report.checks)


def test_doctor_checks_every_shipped_prompt_body_exists() -> None:
    from in_lockstep import doctor

    report = doctor.run(".")
    assert not any(c.code == "DOC140" for c in report.checks), "shipped bodies must resolve"
