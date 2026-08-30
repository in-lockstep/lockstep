"""Phase-3 gates: the controls that replace what the substrate provided."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
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


def test_the_restricted_classification_is_read_from_the_environment(monkeypatch) -> None:
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

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITLAB_CI", raising=False)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    report = doctor.run(".")
    assert any(c.code == "DOC110" for c in report.errors)


def test_gate_cfg_2_engages_on_a_gitlab_merge_request_pipeline(monkeypatch) -> None:
    """The check once read GITHUB_* directly, so a GitLab MR pipeline passed silently —
    configuration loading from the ref under review, with nothing reporting it."""
    from in_lockstep import doctor

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setenv("CI_PIPELINE_SOURCE", "merge_request_event")
    monkeypatch.delenv("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", raising=False)
    report = doctor.run(".")
    assert any(c.code == "DOC110" for c in report.errors)


def test_gate_cfg_2_stays_quiet_outside_any_ci(monkeypatch) -> None:
    for var in ("GITHUB_ACTIONS", "GITLAB_CI", "GITHUB_EVENT_NAME"):
        monkeypatch.delenv(var, raising=False)
    from in_lockstep import doctor

    report = doctor.run(".")
    assert not any(c.code == "DOC110" for c in report.checks)


def test_doctor_warns_about_pull_request_target(monkeypatch) -> None:
    from in_lockstep import doctor

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITLAB_CI", raising=False)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    report = doctor.run(".")
    assert any(c.code == "DOC111" for c in report.checks)


def _write_lifecycle(tmp_path, body: str) -> None:
    (tmp_path / ".lockstep").mkdir()
    (tmp_path / ".lockstep" / "lockstep.py").write_text(body)


def _not_in_ci(monkeypatch) -> None:
    """Doctor's route check loads config from the trusted BASE ref when it detects CI — correct
    in a real PR pipeline, but these tests point doctor at a bare tmp_path that has no `main` to
    resolve. Cleared here so the check loads the working-tree module the test actually wrote;
    without this the load raises UnresolvableConfigRef and the route check is silently skipped
    (which is why these pass on a laptop and failed only on a GitHub runner)."""
    for var in ("GITHUB_ACTIONS", "GITLAB_CI", "GITHUB_EVENT_NAME", "GITHUB_BASE_REF"):
        monkeypatch.delenv(var, raising=False)


def test_doctor_flags_a_route_to_an_unregistered_provider(tmp_path, monkeypatch) -> None:
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


def test_doctor_flags_an_unpriced_route_before_a_run_pays_for_the_lesson(tmp_path, monkeypatch) -> None:
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


def test_doctor_accepts_a_route_to_a_free_local_model(tmp_path, monkeypatch) -> None:
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


def test_doctor_strict_reads_the_loader_location_not_the_deprecated_root(tmp_path) -> None:
    from in_lockstep import doctor

    (tmp_path / ".lockstep").mkdir()
    (tmp_path / ".lockstep" / "lockstep.py").write_text("lockstep = None\n")
    report = doctor.run(tmp_path, strict=True)
    assert not any(c.code in ("DOC160", "DOC161") for c in report.checks)


def test_doctor_strict_flags_a_legacy_root_module(tmp_path) -> None:
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


def _ai_writer():
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


def _baseline_env(monkeypatch) -> None:
    """No inherited baseline: these tests assert exactly what they set."""
    _not_in_ci(monkeypatch)
    for var in ("IN_LOCKSTEP_REQUIRED_POLICIES", "IN_LOCKSTEP_MAX_BUDGET_USD", "IN_LOCKSTEP_MAX_TURNS"):
        monkeypatch.delenv(var, raising=False)


def test_strict_errors_on_a_missing_required_policy_layer(tmp_path, monkeypatch) -> None:
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


def test_strict_without_a_stated_baseline_adds_nothing(tmp_path, monkeypatch) -> None:
    from in_lockstep import doctor

    _baseline_env(monkeypatch)
    _write_lifecycle(tmp_path, "from in_lockstep import Lockstep\nlockstep = Lockstep()\n")
    report = doctor.run(tmp_path, strict=True)
    assert not any(c.code in ("DOC162", "DOC163") for c in report.checks)


def test_strict_errors_when_the_budget_exceeds_the_org_maximum(tmp_path, monkeypatch) -> None:
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


def test_strict_errors_when_the_org_caps_and_no_budget_is_declared(tmp_path, monkeypatch) -> None:
    """An absent ceiling is not a compliant ceiling."""
    from in_lockstep import doctor

    _baseline_env(monkeypatch)
    monkeypatch.setenv("IN_LOCKSTEP_MAX_BUDGET_USD", "1.00")
    _write_lifecycle(tmp_path, "from in_lockstep import Lockstep\nlockstep = Lockstep()\n")
    report = doctor.run(tmp_path, strict=True)
    assert any(c.code == "DOC163" and "declares no budget" in c.message for c in report.errors)


def test_strict_accepts_a_budget_at_or_under_the_org_maximum(tmp_path, monkeypatch) -> None:
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


def test_strict_errors_when_the_turn_ceiling_is_unbounded_or_over(tmp_path, monkeypatch) -> None:
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


def test_strict_names_the_egress_opt_out_where_the_fleet_looks(tmp_path, monkeypatch) -> None:
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


def test_strict_names_unsandboxed_run_even_inside_a_worktree_wrapper(tmp_path, monkeypatch) -> None:
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


def test_strict_errors_when_a_spending_writing_adapter_has_no_approval_path(tmp_path, monkeypatch) -> None:
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


def test_doctor_format_json_is_the_fleet_scanners_shape(tmp_path, monkeypatch) -> None:
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


def _repo_with_the_loop_wired(tmp_path, *, labels: tuple[str, ...]):  # noqa: ANN001, ANN202
    """A repository whose trampoline routes on `ai-generated`, plus a `gh` that lists `labels`."""
    (tmp_path / ".git").mkdir()
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ai-generated.yml").write_text(
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


def test_doctor_reports_a_missing_ai_generated_label(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """The label is the trigger AND the authorization, so its absence is not cosmetic.

    Without it a failed run pays for a model call and then cannot file the follow-up, and nothing
    would route to the fixing verb even if it could.
    """
    from in_lockstep import doctor

    bin_dir = _repo_with_the_loop_wired(tmp_path, labels=("bug", "enhancement"))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    report = doctor.run(str(tmp_path))
    finding = next(c for c in report.errors if c.code == "DOC123")
    assert "ai-generated.yml" in finding.message
    assert "gh label create ai-generated" in finding.hint


def test_doctor_reports_missing_attempt_labels_by_name(tmp_path, monkeypatch) -> None:  # noqa: ANN001
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


def test_doctor_is_quiet_when_every_escalation_label_exists(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    from in_lockstep import doctor

    bin_dir = _repo_with_the_loop_wired(
        tmp_path, labels=("ai-generated", "ai-attempt-1", "ai-attempt-2", "ai-attempt-3")
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    report = doctor.run(str(tmp_path))
    assert not any(c.code in ("DOC123", "DOC124") for c in report.checks)


def test_doctor_says_nothing_to_a_repository_that_never_wired_the_loop(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """A finding invented for a hook nobody asked for is how a code teaches people to ignore it."""
    from in_lockstep import doctor

    bin_dir = _repo_with_the_loop_wired(tmp_path, labels=("bug",))
    (tmp_path / ".github" / "workflows" / "ai-generated.yml").unlink()
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    report = doctor.run(str(tmp_path))
    assert not any(c.code in ("DOC122", "DOC123", "DOC124") for c in report.checks)
