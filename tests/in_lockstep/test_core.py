"""Phase-1 gates over the dispatch core."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from typing import ClassVar

import pytest

from in_lockstep.core import (
    ChangeAuthor,
    ChangeGuard,
    ChangeSet,
    Container,
    Cost,
    FileChange,
    Improvable,
    Outcome,
    PathPolicy,
    Policy,
    PolicyStack,
    RepoInfo,
    ResolutionError,
    RunContext,
    Spend,
    Status,
    TestReport,
)
from in_lockstep.core.container import Tier
from in_lockstep.core.context import DISABLE_ENV
from in_lockstep.core.spend import Budget
from in_lockstep.core.verbs import Capability, Verb
from in_lockstep.core.workflow import DuplicateWorkflow, id_of, restore, snapshot, workflow
from in_lockstep.middleware.budget import CostBudget
from in_lockstep.middleware.otel import Recorder, otel
from in_lockstep.middleware.retry import Retry


@dataclass(frozen=True)
class Thing:
    """A verb request."""

    payload: str = ""


class Ok:
    verb: ClassVar[Verb] = Verb.TEST
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.READS_REPO})

    def __init__(self, cost: Cost | None = None) -> None:
        self.calls = 0
        self.cost = cost or Cost()

    async def invoke(self, ctx, inp):
        self.calls += 1
        return Outcome(status=Status.SUCCEEDED, value=inp, cost=self.cost)


class Flaky:
    verb: ClassVar[Verb] = Verb.TEST
    capabilities: ClassVar[frozenset[Capability]] = frozenset()

    def __init__(self, fail_times: int) -> None:
        self.calls = 0
        self.fail_times = fail_times

    async def invoke(self, ctx, inp):
        self.calls += 1
        if self.calls <= self.fail_times:
            return Outcome.errored("transient")
        return Outcome(status=Status.SUCCEEDED, value=inp)


class Spender:
    verb: ClassVar[Verb] = Verb.IMPLEMENT
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.SPENDS_BUDGET})

    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, ctx, inp):
        self.calls += 1
        return Outcome.errored("provider 500")


def ctx_with(*bindings, middleware=None) -> tuple[RunContext, Container]:
    container = Container()
    for iface, impl in bindings:
        container.bind(iface, impl)
    ctx = RunContext(
        run_id="t",
        repo=RepoInfo(root="."),
        container=container,
        middleware=list(middleware or []),
    )
    return ctx, container


# -- GATE-OUT-1 ---------------------------------------------------------------------


def test_gate_out_1_status_is_closed_at_six_members() -> None:
    """UNDECIDED is not a status: evidence is orthogonal to how a run ended."""
    assert len(Status) == 6
    assert {s.name for s in Status} == {
        "SUCCEEDED",
        "FAILED",
        "ERRORED",
        "BLOCKED",
        "SKIPPED",
        "PARKED",
    }
    assert not hasattr(Status, "UNDECIDED")


def test_parked_exists_but_nothing_produces_it_at_1_0() -> None:
    """Commitment 1: the member is committed now because adding to a closed enum is breaking."""
    assert Status.PARKED
    assert not Status.PARKED.terminal, "a parked run is waiting, not done"


def test_decided_is_orthogonal_to_status() -> None:
    """A successful run that judged nothing is not a cache hit, and must not look like one."""
    undecided = Outcome(status=Status.SUCCEEDED, decided=False)
    cache_hit = Outcome.skipped()
    assert undecided.succeeded and not undecided.decided
    assert cache_hit.status is Status.SKIPPED and cache_hit.decided


def test_reason_refines_status_without_new_members() -> None:
    assert Outcome.blocked_by("expired").reason == "expired"
    assert Outcome.blocked_by("killswitch").status is Status.BLOCKED


# -- GATE-COST-1 --------------------------------------------------------------------


def test_gate_cost_1_spend_is_run_scoped_not_global() -> None:
    """The upstream tracker was a module singleton; two runs would cross-stamp each other."""
    a = Spend()
    b = Spend()
    a.charge(Cost(usd=1.0))
    assert a.charged.usd == 1.0
    assert b.charged.usd == 0.0, "spend must not be shared between runs"


def test_gate_cost_1_no_module_level_accumulator() -> None:
    import in_lockstep.core.spend as module

    globals_ = {
        k: v
        for k, v in vars(module).items()
        if not k.startswith("_") and isinstance(v, (Spend, list, dict, set))
    }
    assert not globals_, f"module-level mutable state: {sorted(globals_)}"


def test_predictive_check_refuses_before_spending() -> None:
    """The whole point: asked BEFORE the call, with what the call would cost."""
    spend = Spend(budget=Budget(usd=0.10))
    spend.charge(Cost(usd=0.08))
    assert spend.exceeded() is None, "not yet over"
    assert spend.would_exceed(Cost(usd=0.05)) is not None, "but this call would cross it"


def test_ceilings_merge_lowest_not_last() -> None:
    merged = Budget(usd=5.0).merge(Budget(usd=1.0)).merge(Budget(usd=3.0))
    assert merged.usd == 1.0


# -- the kill switch ----------------------------------------------------------------


def test_killswitch_halts_before_any_adapter_runs(monkeypatch) -> None:
    """GATE-ASYNC-3 (P1 form). Checked before the chain, so --no-middleware cannot reach past it."""
    adapter = Ok()
    ctx, _ = ctx_with((Thing, adapter))
    monkeypatch.setenv(DISABLE_ENV, "1")
    outcome = asyncio.run(ctx.do(Thing("x")))
    assert outcome.status is Status.BLOCKED
    assert outcome.reason == "killswitch"
    assert adapter.calls == 0, "the adapter must not have executed"


def test_killswitch_is_not_middleware(monkeypatch) -> None:
    """It must hold with an empty chain, which is what --no-middleware produces."""
    adapter = Ok()
    ctx, _ = ctx_with((Thing, adapter), middleware=[])
    monkeypatch.setenv(DISABLE_ENV, "1")
    assert asyncio.run(ctx.do(Thing("x"))).reason == "killswitch"
    assert adapter.calls == 0


# -- dispatch, steps, container ------------------------------------------------------


def test_do_is_call_then_run_call() -> None:
    """Branches must be describable before they run; an eager-only entry point forecloses that."""
    adapter = Ok()
    ctx, _ = ctx_with((Thing, adapter))
    call = ctx.call(Thing("payload"))
    assert adapter.calls == 0, "declaring must not start it"
    outcome = asyncio.run(ctx.run_call(call))
    assert outcome.value == Thing("payload")
    assert adapter.calls == 1


def test_via_binds_at_the_call_and_only_at_the_call() -> None:
    """`ctx.do(request, via=adapter)` serves this call with the named adapter.

    The container is not consulted and not mutated: a call may name its adapter with nothing
    bound at all, and a later call without `via=` still resolves the binding."""
    bound = Ok()
    supplied = Ok()
    ctx, _ = ctx_with((Thing, bound))

    outcome = asyncio.run(ctx.do(Thing("x"), via=supplied))
    assert outcome.succeeded
    assert supplied.calls == 1 and bound.calls == 0, "the call site's choice served the call"

    asyncio.run(ctx.do(Thing("x")))
    assert bound.calls == 1, "the next call still resolves the container binding"

    unbound_ctx, _ = ctx_with()
    lone = Ok()
    assert asyncio.run(unbound_ctx.do(Thing("x"), via=lone)).succeeded
    assert lone.calls == 1, "via needs no binding to exist"


def test_capabilities_follow_the_via_adapter() -> None:
    """Capability-keyed middleware gates what actually serves the call, bound or supplied."""
    from in_lockstep.core.middleware import ActionCall, capabilities_for

    class Dangerous:
        verb = Verb.TEST
        capabilities = frozenset({Capability.WRITES_FILES})

        async def invoke(self, ctx, inp):  # pragma: no cover - never invoked here
            raise AssertionError

    ctx, _ = ctx_with((Thing, Ok()))
    call = ActionCall(Thing("x"), via=Dangerous())
    assert capabilities_for(ctx, call) == frozenset({Capability.WRITES_FILES}), (
        "the declaration travels with the adapter the call names, not the binding"
    )


def test_step_ids_are_scoped_and_stable() -> None:
    adapter = Ok()
    ctx, _ = ctx_with((Thing, adapter))
    asyncio.run(ctx.do(Thing("a")))
    first = ctx.last_step
    asyncio.run(ctx.do(Thing("a")))
    second = ctx.last_step
    assert first is not None and second is not None
    assert first.scope_path == "", "flat at 1.0, structured so branches can key their own space"
    assert first.input_hash == second.input_hash, "same input, same hash"
    assert first.call_site != second.call_site, "distinct call sites get distinct ids"


def test_container_resolution_order_repo_beats_org() -> None:
    container = Container()
    container.bind(Thing, Ok(), tier=Tier.DEFAULT)
    shipped = container.resolve(Thing)
    container.bind(Thing, Flaky(0), tier=Tier.EXPLICIT)
    assert container.resolve(Thing) is not shipped, "an explicit bind overrides a shipped default"


def test_container_ignores_a_lower_priority_rebind() -> None:
    container = Container()
    explicit = Ok()
    container.bind(Thing, explicit, tier=Tier.EXPLICIT)
    container.bind(Thing, Ok(), tier=Tier.PLUGIN)
    assert container.resolve(Thing) is explicit


def test_unbound_interface_says_so() -> None:
    with pytest.raises(ResolutionError, match="Thing"):
        Container().resolve(Thing)


# -- middleware ----------------------------------------------------------------------


def test_middleware_wraps_and_records() -> None:
    recorder = Recorder()
    ctx, _ = ctx_with((Thing, Ok(cost=Cost(usd=0.25))), middleware=[otel(recorder)])
    asyncio.run(ctx.do(Thing("x")))
    assert len(recorder.spans) == 1
    names = {m.name for m in recorder.metrics}
    assert "in_lockstep.action.outcome" in names
    assert "in_lockstep.cost.usd" in names


def test_decided_is_a_metric_dimension() -> None:
    """Without it, an eval suite that judged nothing reads green on every dashboard."""
    recorder = Recorder()
    ctx, _ = ctx_with((Thing, Ok()), middleware=[otel(recorder)])
    asyncio.run(ctx.do(Thing("x")))
    outcome_metric = next(m for m in recorder.metrics if m.name == "in_lockstep.action.outcome")
    assert "decided" in outcome_metric.dimensions


def test_metrics_carry_no_run_id() -> None:
    """run_id on a metric is unbounded cardinality. It belongs on spans."""
    recorder = Recorder()
    ctx, _ = ctx_with((Thing, Ok()), middleware=[otel(recorder)])
    asyncio.run(ctx.do(Thing("x")))
    for metric in recorder.metrics:
        assert "run_id" not in metric.dimensions
    assert any("in_lockstep.run_id" in s.attributes for s in recorder.spans)


def test_retry_targets_errored_only() -> None:
    adapter = Flaky(fail_times=2)
    ctx, _ = ctx_with((Thing, adapter), middleware=[Retry(attempts=3, base_delay=0)])
    outcome = asyncio.run(ctx.do(Thing("x")))
    assert outcome.succeeded
    assert adapter.calls == 3


def test_gate_retry_5_refuses_to_retry_a_budgeted_action() -> None:
    """Re-invoking a loop re-pays every turn already spent."""
    adapter = Spender()
    ctx, _ = ctx_with((Thing, adapter), middleware=[Retry(attempts=3, base_delay=0)])
    outcome = asyncio.run(ctx.do(Thing("x")))
    assert adapter.calls == 1, "must be invoked exactly once"
    assert any(f.id == "retry.refused_budgeted_action" for f in outcome.findings)


def test_budget_blocks_when_the_ceiling_is_already_crossed() -> None:
    ctx, _ = ctx_with((Thing, Ok(cost=Cost(usd=5.0))), middleware=[CostBudget(usd=1.0)])
    first = asyncio.run(ctx.do(Thing("x")))
    assert first.status is Status.BLOCKED, "reconciliation catches the overrun"
    assert first.reason and first.reason.startswith("budget:")
    second = asyncio.run(ctx.do(Thing("y")))
    assert second.status is Status.BLOCKED, "and the next call is refused up front"


# -- PolicyStack: GATE-POLICY-1 -------------------------------------------------------


def test_gate_policy_1_deny_all_is_an_irreversible_floor() -> None:
    """A repo inheriting two upstreams must not have the second undo the first's egress rule."""
    stack = PolicyStack()
    stack.contribute(Policy(name="org", network="deny-all"))
    stack.contribute(Policy(name="local", network="allow-list"))
    assert stack.resolve().network == "deny-all"


def test_gate_policy_1_ceilings_take_the_lowest_not_the_last() -> None:
    stack = PolicyStack()
    stack.contribute(Policy(name="a", max_turns=5))
    stack.contribute(Policy(name="b", max_turns=20))
    assert stack.resolve().max_turns == 5


def test_gate_policy_1_deny_tools_union_and_strictest_scan() -> None:
    stack = PolicyStack()
    stack.contribute(Policy(name="a", deny_tools=("x",), scan_input="warn"))
    stack.contribute(Policy(name="b", deny_tools=("y", "x"), scan_input="block"))
    resolved = stack.resolve()
    assert set(resolved.deny_tools) == {"x", "y"}
    assert len(resolved.deny_tools) == 2, "union, not concatenation"
    assert resolved.scan_input == "block"


def test_policy_stack_has_no_removal_api() -> None:
    """The capability preserved is visibility of removal — via a diff, not via an API."""
    for name in ("remove", "pop", "clear", "exclude", "delete"):
        assert not hasattr(PolicyStack, name), f"PolicyStack must not expose {name}()"


# -- ChangeGuard ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "lockstep.py",
        ".in-lockstep/skills/x.md",
        ".github/workflows/ci.yml",
        ".git/hooks/pre-commit",
        "pyproject.toml",
        "src/pkg/conftest.py",
        "CODEOWNERS",
        ".env",
        "deploy/key.pem",
    ],
)
def test_tier_1_paths_are_refused_absolutely(path: str) -> None:
    """lockstep.py leads: config is executable now, so editing it grants arbitrary capability."""
    assert ChangeGuard().check_path(path) is not None, f"{path} must be denied"


def test_paths_escaping_the_repo_are_refused() -> None:
    guard = ChangeGuard()
    assert guard.check_path("../../etc/passwd") is not None
    assert guard.check_path("/etc/passwd") is not None


def test_symlink_target_is_evaluated_on_the_post_change_tree() -> None:
    """A symlink written this turn is an out-of-root write next turn."""
    guard = ChangeGuard()
    change = FileChange(path="docs/link", contents="", symlink_target="../../../etc/passwd")
    refusal = guard.check_change(change)
    assert refusal is not None and refusal.rule == "symlink-outside-repo-root"


def test_ordinary_source_is_writable() -> None:
    assert ChangeGuard().check_path("src/myapp/service.py") is None
    assert ChangeGuard().check_path("tests/test_service.py") is None, "writing tests is core"


def test_framework_authored_ledger_write_is_not_denied_by_its_own_policy() -> None:
    """The guard runs over agent-authored entries only, or the ledger commit refuses itself."""
    changeset = ChangeSet(
        changes=(
            FileChange(path=".in-lockstep/ledger/run-1.json", contents="{}", author=ChangeAuthor.FRAMEWORK),
            FileChange(path="src/app.py", contents="x", author=ChangeAuthor.AGENT),
        )
    )
    assert ChangeGuard().check(changeset) == []


def test_tier_2_grant_is_keyed_on_a_workflow_id() -> None:
    """Never on a strategy id: strategy selection can be steered by untrusted ticket labels."""
    policy = PathPolicy(grants=frozenset({"prompts/"}), granted_to_workflow="improve")
    guard = ChangeGuard(policy)
    assert guard.check_path("prompts/review.md", workflow_id="improve") is None
    assert guard.check_path("prompts/review.md", workflow_id="implement") is not None
    assert guard.check_path("prompts/review.md") is not None


# -- workflows ------------------------------------------------------------------------


def test_workflows_register_under_a_stable_id() -> None:
    # `snapshot`/`restore`, never `clear`. The registry is process-global and the framework's own
    # `selfcheck` lives in it, so clearing removes it for every later test — which used to be
    # invisible because the dispatcher special-cased `selfcheck` before consulting the registry,
    # and became `unknown workflow 'selfcheck'` the moment it stopped.
    state = snapshot()
    try:

        @workflow(id="fix-ci/after-review")
        async def after_review(ctx):
            return None

        assert id_of(after_review) == "fix-ci/after-review"
        renamed = after_review  # a later refactor renames the function, not the id
        assert id_of(renamed) == "fix-ci/after-review"
    finally:
        restore(state)


def test_duplicate_workflow_ids_are_refused() -> None:
    state = snapshot()
    try:

        @workflow(id="dup")
        async def a(ctx):
            return None

        with pytest.raises(DuplicateWorkflow, match="dup"):

            @workflow(id="dup")
            async def b(ctx):
                return None
    finally:
        restore(state)


# -- TestReport.flaky -----------------------------------------------------------------


def test_test_report_carries_flaky_from_1_0() -> None:
    """Commitment 8: a field on a checkpointed, ledgered type cannot be added later."""
    report = TestReport(total=1, passed=1)
    assert report.flaky == ()
    assert TestReport(flaky=("tests/test_x.py::test_y",)).flaky


def test_no_tests_collected_is_undecided_not_a_pass() -> None:
    """A suite that ran nothing must not look like a suite that passed everything.

    This is the reassuring-number failure in miniature, and it is the reason `decided` is on
    Outcome rather than folded into Status.
    """
    import asyncio

    from in_lockstep.adapters.pytest_adapter import PytestTest
    from in_lockstep.core.types import Test

    ctx, container = ctx_with()
    container.bind(Test, PytestTest(args=["-q", "--no-header"], cwd="."))
    outcome = asyncio.run(ctx.do(Test(paths=("src/in_lockstep/core",))))

    assert outcome.status is Status.SUCCEEDED, "collecting nothing is not a red suite"
    assert not outcome.decided, "and it is not a green one either"
    assert any(f.id == "test.no_tests_collected" for f in outcome.findings)


def test_capabilities_for_reads_the_binding_not_the_call() -> None:
    """The mistake this helper exists to prevent fails OPEN, which is why it is a helper.

    An `ActionCall` names an interface; capabilities belong to whatever is bound to serve it.
    `capabilities_of(call)` type-checks, returns an empty set, and a capability gate written that
    way silently permits everything. Both shipped middleware open-coded the lookup before this.
    """
    from in_lockstep.core.middleware import ActionCall, capabilities_for
    from in_lockstep.core.verbs import capabilities_of
    from in_lockstep.lockstep import Lockstep

    class Iface: ...

    class Dangerous:
        verb = Verb.TEST
        capabilities = frozenset({Capability.WRITES_FILES})

        async def invoke(self, ctx, inp):  # pragma: no cover - never invoked here
            raise AssertionError

    lockstep = Lockstep.detect()
    lockstep.bind(Iface, Dangerous())
    ctx = lockstep.context(run_id="caps")
    call = ActionCall(Iface())

    assert capabilities_for(ctx, call) == frozenset({Capability.WRITES_FILES})
    # The trap, asserted so it cannot be mistaken for equivalent.
    assert capabilities_of(call) == frozenset()


def test_capabilities_for_is_empty_when_nothing_is_bound() -> None:
    from in_lockstep.core.middleware import ActionCall, capabilities_for
    from in_lockstep.lockstep import Lockstep

    class Unbound: ...

    ctx = Lockstep.detect().context(run_id="caps")
    assert capabilities_for(ctx, ActionCall(Unbound())) == frozenset()


# -- Verb: open to extension, identical for the verbs that shipped ---------------------------


@pytest.fixture(autouse=True)
def _no_verb_leakage() -> Iterator[None]:
    from in_lockstep.core.verbs import Verb

    yield
    Verb.forget_custom()


def test_a_user_can_define_a_verb_the_framework_never_heard_of() -> None:
    """The point of the change.

    Binding a new interface already worked. What did not was *naming* the work: `verb` had to be
    a member of a closed enum, so a benchmark adapter borrowed `Verb.RUN` and then reported itself
    as `run` in every span, metric dimension and step id.
    """
    from in_lockstep.core.verbs import Verb

    benchmark = Verb("benchmark")
    assert benchmark.value == "benchmark"
    assert benchmark is not Verb.RUN
    assert benchmark in Verb.known()


def test_verbs_are_interned_so_identity_still_works() -> None:
    """`strategy.py` compares with `is`. Interning is what let the enum become an open type."""
    from in_lockstep.core.verbs import Verb

    assert Verb("test") is Verb.TEST
    assert Verb("TEST") is Verb.TEST, "normalised, so case cannot fork a verb"
    assert Verb("  test ") is Verb.TEST
    assert Verb("benchmark") is Verb("benchmark")


def test_a_typo_is_a_distinct_verb_not_a_silent_alias() -> None:
    """It surfaces as a verb nothing is bound to, rather than work routed somewhere wrong."""
    from in_lockstep.core.verbs import Verb

    assert Verb("reviwe") is not Verb.REVIEW


def test_a_verb_is_immutable() -> None:
    from in_lockstep.core.verbs import Verb

    with pytest.raises(AttributeError):
        Verb.TEST.value = "something else"  # type: ignore[misc]


def test_a_verb_survives_a_round_trip_with_its_identity() -> None:
    """Interning that a pickle breaks would make `is` comparisons fail after a checkpoint."""
    import pickle

    from in_lockstep.core.verbs import Verb

    assert pickle.loads(pickle.dumps(Verb.TEST)) is Verb.TEST
    assert pickle.loads(pickle.dumps(Verb("benchmark"))) is Verb("benchmark")


def test_a_custom_verb_labels_its_own_telemetry() -> None:
    """A borrowed verb was not merely inelegant: it put a benchmark's spans under `run`."""
    from in_lockstep.core.middleware import ActionCall
    from in_lockstep.core.verbs import Verb

    class Benchmark: ...

    call = ActionCall(Benchmark())
    call.verb = Verb("benchmark")
    assert call.verb is not None
    assert call.verb.value == "benchmark"
    assert "benchmark" in repr(call)


# -- GATE-BUDGET-1 ----------------------------------------------------------------------------
#
# The substrate this replaced made a missing budget a compile-time ERROR: shipping an agent
# without a ceiling was structurally impossible. Porting that into an advisory `doctor` check
# would have been a downgrade wearing the name of a port, so it refuses at startup.


def _spender():
    from in_lockstep.core.verbs import Capability, Verb

    class Spender:
        verb = Verb.REVIEW
        capabilities = frozenset({Capability.SPENDS_BUDGET})

        async def invoke(self, ctx, inp):  # pragma: no cover - never reached
            raise AssertionError

    return Spender


def test_gate_budget_1_a_lifecycle_that_spends_without_a_ceiling_is_refused() -> None:
    from in_lockstep.core.spend import UndeclaredBudget
    from in_lockstep.lockstep import Lockstep

    class Review: ...

    lockstep = Lockstep.detect()
    lockstep.bind(Review, _spender()())

    with pytest.raises(UndeclaredBudget) as exc:
        lockstep.context(run_id="r")
    assert "Spender" in str(exc.value), "the message names what spends, not just that something does"
    assert "lockstep.budget" in str(exc.value), "and shows the fix"


def test_a_deterministic_lifecycle_needs_no_budget() -> None:
    """A repository binding only Test and Validate spends nothing.

    Demanding a ceiling there would teach people to write `Budget(usd=999)` to quiet the
    framework, which is worse than no check because it looks like a decision.
    """
    from in_lockstep.lockstep import Lockstep

    lockstep = Lockstep.detect()
    lockstep.bind(Iface := type("Iface", (), {}), _deterministic()())
    assert lockstep.context(run_id="r") is not None
    assert Iface is not None


def _deterministic():
    from in_lockstep.core.verbs import Capability, Verb

    class Cheap:
        verb = Verb.TEST
        capabilities = frozenset({Capability.READS_REPO, Capability.EXECUTES_CODE})

        async def invoke(self, ctx, inp):  # pragma: no cover - never reached
            raise AssertionError

    return Cheap


def test_a_ceiling_declared_as_middleware_counts() -> None:
    """`lockstep.budget` is one way to say it; CostBudget is another, and the scaffold uses that.

    Reading only the first would refuse a run that is perfectly well bounded, and a refusal people
    learn to route around is worse than none.
    """
    from in_lockstep.lockstep import Lockstep
    from in_lockstep.middleware import CostBudget

    class Review: ...

    lockstep = Lockstep.detect()
    lockstep.bind(Review, _spender()())
    lockstep.middleware += [CostBudget(usd=2.00)]
    assert lockstep.context(run_id="r") is not None


@pytest.mark.parametrize("ceiling", [{"usd": 1.0}, {"tokens": 1000}, {"wall_seconds": 60}, {"turns": 4}])
def test_any_declared_ceiling_satisfies_it(ceiling: dict) -> None:
    """Four dimensions, any one of which is a decision about how much is too much."""
    from in_lockstep.core.spend import Budget
    from in_lockstep.lockstep import Lockstep

    class Review: ...

    lockstep = Lockstep.detect()
    lockstep.bind(Review, _spender()())
    lockstep.budget = Budget(**ceiling)
    assert lockstep.context(run_id="r") is not None


def test_an_empty_budget_object_is_not_a_declaration() -> None:
    """`Budget()` is the default. A default is not a decision."""
    from in_lockstep.core.spend import Budget, UndeclaredBudget
    from in_lockstep.lockstep import Lockstep

    class Review: ...

    lockstep = Lockstep.detect()
    lockstep.bind(Review, _spender()())
    lockstep.budget = Budget()

    with pytest.raises(UndeclaredBudget):
        lockstep.context(run_id="r")


def test_a_ceiling_of_zero_is_a_declaration_and_is_enforced() -> None:
    """GATE-BUDGET-1 accepts `Budget(usd=0.0)`. "Absent is not zero" runs both ways.

    Zero is how a run that cannot spend says so: `--offline`, `--dry-run` and `apply` state it
    rather than asking the gate for an exemption. The statement is only honest if it is enforced,
    so the second half checks that `Spend` refuses the first projected cent under it while an
    unbilled projection, which is what a replay produces, goes through.
    """
    from in_lockstep.core.outcome import Cost
    from in_lockstep.core.spend import Budget, Spend
    from in_lockstep.lockstep import Lockstep

    class Review: ...

    lockstep = Lockstep.detect()
    lockstep.bind(Review, _spender()())
    lockstep.budget = Budget(usd=0.0)
    assert lockstep.context(run_id="r") is not None

    spend = Spend(budget=Budget(usd=0.0))
    assert spend.would_exceed(Cost(usd=0.0)) is None, "a replay's projection is unbilled"
    assert spend.would_exceed(Cost(usd=0.01)) is not None, "a real one is refused before the call"


# -- GATE-LEDGER-5 -----------------------------------------------------------------------------
#
# The metric namespace broke at the pivot: `lockstep.*` was the compiler's, `in_lockstep.*` is the
# framework's, and a dashboard built on the old prefix must not silently keep receiving data under
# it. The positive half — that the names we expect are emitted — was asserted from Phase 1. The
# negative clause was not, and it is the half that catches a regression.

METRIC_PREFIXES = ("in_lockstep.", "gen_ai.")
RETIRED_PREFIX = "lockstep."


def _metric_names_in_source() -> list[tuple[str, str]]:
    """Every literal metric name in the package, from the AST rather than from a run.

    A runtime check only sees metrics some test happened to trigger. A metric added behind a
    condition nothing exercises is exactly the one that would carry a stale prefix unnoticed.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "in_lockstep"
    found = []
    for path in src.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "metric"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                found.append((str(path.relative_to(src)), node.args[0].value))
    return found


def test_gate_ledger_5_every_metric_name_in_the_package_is_namespaced() -> None:
    sites = _metric_names_in_source()
    assert len(sites) >= 4, f"the scan found {len(sites)} metric call sites; it should find every one"
    for where, name in sites:
        assert name.startswith(METRIC_PREFIXES), f"{where} emits {name!r}, outside the namespace"


def test_gate_ledger_5_no_metric_carries_the_retired_prefix() -> None:
    """The negative clause. `in_lockstep.` CONTAINS `lockstep.`, so this is about the prefix.

    Spelled as a `startswith` rather than a substring check, and asserted here so that nobody
    later "tightens" it into the shape that rejects every legitimate metric in the package.
    """
    assert "in_lockstep.action.outcome".startswith(RETIRED_PREFIX) is False
    assert RETIRED_PREFIX in "in_lockstep.action.outcome", "the trap this test exists to document"

    for where, name in _metric_names_in_source():
        assert not name.startswith(RETIRED_PREFIX), f"{where} emits the compiler's namespace: {name!r}"


def test_gate_ledger_5_holds_for_what_a_run_actually_emits() -> None:
    """Source and runtime both, because a name can also be built rather than written."""
    recorder = Recorder()
    ctx, _ = ctx_with(
        (Thing, Ok(cost=Cost(usd=0.25, input_tokens=10, output_tokens=5, priced_tokens=15))),
        middleware=[otel(recorder)],
    )
    asyncio.run(ctx.do(Thing("x")))

    assert recorder.metrics, "nothing was emitted, so this asserted nothing"
    for metric in recorder.metrics:
        assert metric.name.startswith(METRIC_PREFIXES), metric.name
        assert not metric.name.startswith(RETIRED_PREFIX), metric.name


def test_re_importing_a_module_is_not_a_duplicate_workflow() -> None:
    """Object identity was the test, which made a module conflict with itself on re-import.

    Any host that loads `lockstep.py` twice in one process hits it — a long-lived worker, a test
    harness, two CLI invocations sharing an interpreter. That turns "configuration is code" into
    "configuration is code you may import exactly once".
    """
    from in_lockstep.core.workflow import DuplicateWorkflow, restore, snapshot, workflow

    state = snapshot()
    try:

        def make():  # noqa: ANN202 - a stand-in for module execution
            @workflow(id="demo/reimport")
            async def run(ctx):  # noqa: ANN001, ANN202
                return None

            return run

        first, second = make(), make()
        assert first is not second, "the fixture must produce distinct function objects"

        with pytest.raises(DuplicateWorkflow, match="already registered"):

            @workflow(id="demo/reimport")
            async def different(ctx):  # noqa: ANN001, ANN202
                return None
    finally:
        restore(state)


# -- the run's verdict, derived from its steps (GATE-LEDGER-9) --------------------------------


def _context_with(*statuses, decided=True, reason=None):
    from in_lockstep.core.container import Container
    from in_lockstep.core.context import RepoInfo, RunContext, StepOutcome
    from in_lockstep.core.outcome import Outcome

    ctx = RunContext(run_id="r", repo=RepoInfo(root="."), container=Container())
    for i, status in enumerate(statuses):
        ctx.steps.append(
            StepOutcome(
                step=f"s{i}",
                verb="test",
                outcome=Outcome(
                    status=status, reason=reason if status is not Status.SUCCEEDED else None, decided=decided
                ),
            )
        )
    return ctx


def test_a_runs_verdict_is_blocked_over_errored_over_failed_over_succeeded() -> None:
    """GATE-LEDGER-9: the deciding step's status, in the order a reader would want to know."""
    from in_lockstep.core.outcome import Status as S

    assert _context_with(S.FAILED, S.BLOCKED, S.ERRORED).verdict()[0] is S.BLOCKED
    assert _context_with(S.FAILED, S.ERRORED).verdict()[0] is S.ERRORED
    assert _context_with(S.SUCCEEDED, S.FAILED).verdict()[0] is S.FAILED
    assert _context_with(S.SUCCEEDED, S.SUCCEEDED).verdict()[0] is S.SUCCEEDED
    assert _context_with().verdict() == (S.SUCCEEDED, None, True), "no steps: nothing failed"


def test_a_runs_verdict_carries_the_deciding_steps_reason_and_decides_only_if_every_step_did() -> None:
    from in_lockstep.core.outcome import Status as S

    status, reason, decided = _context_with(S.SUCCEEDED, S.FAILED, reason="test.red").verdict()
    assert (status, reason) == (S.FAILED, "test.red")
    assert decided
    assert _context_with(S.SUCCEEDED, decided=False).verdict()[2] is False


def test_every_completed_step_is_kept_on_the_context() -> None:
    """The steps are the evidence the record is derived from; a step that ran and left no trace
    is a verdict to take on trust."""
    import asyncio

    from in_lockstep.core.verbs import Capability, Verb
    from in_lockstep.lockstep import Lockstep

    class Ping: ...

    class Pong:
        verb = Verb.TEST
        capabilities = frozenset({Capability.READS_REPO})

        async def invoke(self, ctx, inp):
            return Outcome(status=Status.FAILED, reason="pong.red")

    lockstep = Lockstep.detect()
    lockstep.bind(Ping, Pong())
    ctx = lockstep.context(run_id="r")
    asyncio.run(ctx.do(Ping()))
    asyncio.run(ctx.do(Ping()))
    assert [s.step for s in ctx.steps] == ["test", "test.1"]
    assert ctx.verdict()[:2] == (Status.FAILED, "pong.red")
    record = ctx.steps[0].as_record()
    assert record["verb"] == "test" and record["status"] == "failed" and record["reason"] == "pong.red"


def test_a_step_an_adapter_runs_inside_its_own_does_not_decide_the_run() -> None:
    """A strategy's mid-loop test probe or a reproducer expected to fail is the adapter's business;
    its own outcome is its statement about it. Counting the inner step failed a run whose
    implementing step had succeeded, exited 1, and recorded it, for a workflow that returned a
    dict (GATE-LEDGER-9)."""
    import asyncio

    from in_lockstep.core.verbs import Capability, Verb
    from in_lockstep.lockstep import Lockstep

    class Outer: ...

    class Inner: ...

    class RedProbe:
        verb = Verb.TEST
        capabilities = frozenset({Capability.READS_REPO})

        async def invoke(self, ctx, inp):
            return Outcome(status=Status.FAILED, reason="probe.red")

    class Implementer:
        verb = Verb.IMPLEMENT
        capabilities = frozenset({Capability.READS_REPO})

        async def invoke(self, ctx, inp):
            await ctx.do(Inner())
            return Outcome(status=Status.SUCCEEDED)

    lockstep = Lockstep.detect()
    lockstep.bind(Inner, RedProbe())
    lockstep.bind(Outer, Implementer())
    ctx = lockstep.context(run_id="r")
    asyncio.run(ctx.do(Outer()))
    assert [s.verb for s in ctx.steps] == ["implement"], "the inner probe is not a step of the run"
    assert ctx.verdict()[0] is Status.SUCCEEDED


# -- Improvable: a trend is attributed, never inferred ---------------------------------


def test_an_improvable_answers_only_the_finding_ids_it_names() -> None:
    """Exact membership, not a prefix. `review.` would claim `review.security` and every other
    review finding at once, and an attribution that wide points a future prompt change at a body
    that had nothing to do with the trend."""
    body = Improvable(
        body="src/in_lockstep/prompts/review/security.md",
        verb="review",
        label="review/security",
        answers=("review.security",),
    )
    assert body.answers_for("review.security")
    assert not body.answers_for("review.security.extra")
    assert not body.answers_for("review")
    assert not body.answers_for("cost.budget_exceeded")


def test_an_improvable_that_names_no_finding_answers_nothing() -> None:
    """The default. A repository that declares a body without saying what it answers has told us
    where the text is and nothing about the evidence, so every trend stays unattributed."""
    body = Improvable(body="prompts/x.md", verb="review", label="x")
    assert body.answers == ()
    assert not body.answers_for("review.security")
    assert not body.answers_for("")
