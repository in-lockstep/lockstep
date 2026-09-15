"""What the required check actually reads the diff for. Discharges `GATE-REVIEW-5`.

`review` is a required status check on `main`, so every pull request into this repository is
gated on a model reading the change. It read it through one lens of four. `intent`, `performance`
and `tests` shipped, were composed, sat in the characterization corpus — and had never run on a
real pull request in the repository whose whole argument is that it runs on itself. O10 calls that
asking adopters to trust us on our word; O9 says the four shipped lenses are examples rather than
the set, which is a claim about all four and evidence about one.

It also bounded O5. This check runs with `--record` and harvests, so it is where most of the eval
corpus comes from, and a single-lens check makes a single-lens corpus — leaving the improvement
loop of #163 evidence about a quarter of what the framework ships.

Since Phase 5 the check is `review/all-lenses`, one fan-out whose branches are declared from the
BOUND adapter's own lens map, so the file names no lens and cannot go stale the way a list in
YAML does. What this file holds, then, is that the workflow really does fan out over exactly the
bound set -- both directions, with a stub adapter -- and that `lockstep.yml` runs that workflow
once, recording, on one tape.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml

from in_lockstep.cli import _ensure_review_bound, _parameters, _review_lenses
from in_lockstep.core.container import Container
from in_lockstep.core.context import RepoInfo, RunContext
from in_lockstep.core.outcome import Finding, Outcome, Status
from in_lockstep.core.verbs import Capability, Verb
from in_lockstep.core.workflow import get, restore, snapshot
from in_lockstep.loader import load
from in_lockstep.workflows.review import ALL_LENSES, bound_lenses, register, review_all_lenses

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "lockstep.yml"


@pytest.fixture
def lenses() -> Any:
    """The lenses a review run in THIS repository would actually have: the module loaded, the
    shipped adapter bound the way `run` binds it, and the set read off `compositions()`."""
    state = snapshot()
    module, _ref = load(str(ROOT))
    try:
        _ensure_review_bound(module.lockstep)
        known = _review_lenses(module.lockstep)
        assert known is not None, "this repository's Review adapter must declare its lenses"
        yield set(known)
    finally:
        restore(state)


def _step() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return next(s for s in workflow["jobs"]["reviews"]["steps"] if s.get("name") == "Review")


class _Lensed:
    """A Review adapter that declares its lenses and records which it was asked for."""

    verb: ClassVar[Verb] = Verb.REVIEW
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.READS_REPO})

    def __init__(self, *names: str, red: str = "") -> None:
        self.names = names
        self.red = red
        self.asked: list[str] = []

    def compositions(self) -> dict[str, object]:
        return {f"review/{n}": object() for n in self.names}

    async def invoke(self, ctx: Any, request: Any) -> Outcome[Any]:
        self.asked.append(request.aspect)
        await asyncio.sleep(0.01)
        if request.aspect == self.red:
            return Outcome(
                status=Status.FAILED,
                reason="review.rejected",
                findings=(Finding(id=f"review.{self.red}", message="no"),),
            )
        return Outcome(
            status=Status.SUCCEEDED, findings=(Finding(id=f"review.{request.aspect}", message="a note"),)
        )


def _ctx(adapter: Any | None) -> RunContext:
    from in_lockstep.adapters.ai.review import Review

    container = Container()
    if adapter is not None:
        container.bind(Review, adapter)
    return RunContext(run_id="r", repo=RepoInfo(root="."), container=container)


def test_the_lens_set_is_not_empty(lenses: set[str]) -> None:
    """A positive control: this repository binds at least the four shipped lenses."""
    assert len(lenses) >= 4


def test_the_required_check_exercises_every_lens_that_is_bound(tmp_path: Path) -> None:
    """`GATE-REVIEW-5`. The direction that matters: a lens the adapter declares is a branch the
    workflow runs, whatever its name and however many there are -- five here, one of them
    nothing the framework ships -- and every branch runs even when one of them fails."""
    adapter = _Lensed("security", "intent", "performance", "tests", "licensing", red="intent")
    ctx = _ctx(adapter)
    assert bound_lenses(ctx) == ("intent", "licensing", "performance", "security", "tests")
    outcome = asyncio.run(review_all_lenses(ctx, "main", "HEAD", comments=str(tmp_path / "bodies")))
    assert sorted(adapter.asked) == ["intent", "licensing", "performance", "security", "tests"]
    assert outcome.status is Status.FAILED and outcome.reason == "review.rejected", (
        "the worst verdict is the run's"
    )
    assert sorted(f.id for f in outcome.findings) == sorted(f"review.{n}" for n in adapter.names)
    assert [s.step for s in ctx.steps] == ["intent", "licensing", "performance", "security", "tests"]
    bodies = sorted(p.name for p in (tmp_path / "bodies").glob("*.md"))
    assert bodies == ["intent.md", "licensing.md", "performance.md", "security.md", "tests.md"]
    assert "<!-- in-lockstep:review:licensing -->" in (tmp_path / "bodies" / "licensing.md").read_text()


def test_the_check_does_not_run_a_lens_that_is_not_bound() -> None:
    """The other direction: no adapter, or one that declares nothing, is a refusal by name and
    zero model calls, not a run over a list written somewhere else."""
    outcome = asyncio.run(review_all_lenses(_ctx(None), "main", "HEAD"))
    assert outcome.status is Status.BLOCKED and outcome.reason == "review.no_lenses"
    empty = _Lensed()
    outcome = asyncio.run(review_all_lenses(_ctx(empty), "main", "HEAD"))
    assert outcome.status is Status.BLOCKED and empty.asked == []


def test_this_repositorys_check_runs_the_fan_out_once_recording_on_one_tape(lenses: set[str]) -> None:
    """`lockstep.yml` names the workflow and nothing else (GATE-CI-4): one invocation, no
    `--aspect` list to go stale, `--record` on, one `--cassette` so the single harvest downstream
    sees every lens, and the comment directory the publish job posts from (GATE-REVIEW-6). The
    flip landed one merge after the registration, because configuration loads from the trusted
    ref while the workflow file comes from the merge ref (run 34171482674 was refused by name)."""
    run = " ".join(_step()["run"].split())
    assert run.count("in-lockstep run review/all-lenses") == 1, run
    assert "--aspect" not in run, "the lens list went back into the file"
    assert "||" not in run and "for " not in run and "exit " not in run, "the loop went back into shell"
    assert run.count("--cassette") == 1 and "--record" in run
    assert "--arg comments=review-comments" in run
    # The shipped four are among what this repository binds, so the workflow will run them.
    assert {"security", "intent", "performance", "tests"} <= lenses


# -- which lenses GATE, as distinct from which lenses EXIST (`GATE-REVIEW-9`, #449) ------------


@pytest.fixture
def registry() -> Any:
    """`register` writes to a process-global registry, so every test that calls it restores."""
    state = snapshot()
    try:
        yield
    finally:
        restore(state)


def _registered() -> Any:
    entry = get(ALL_LENSES)
    assert entry is not None, "register() did not claim the id"
    return entry.fn


def test_gate_review_9_only_the_gating_lenses_run_and_the_rest_are_never_asked(
    registry: None, tmp_path: Path
) -> None:
    """`GATE-REVIEW-9`. Five lenses bound, two of them gating: the check runs exactly those two,
    costs two calls rather than five, and writes two comment bodies. The other three are not
    narrowed away from the adapter -- they are simply not this check's business."""
    adapter = _Lensed("security", "intent", "performance", "tests", "licensing")
    ctx = _ctx(adapter)
    # Named in the reverse of the order they run in, deliberately: the branches are ordered by
    # what the ADAPTER declares, not by how the selection was typed, so the steps below assert an
    # ordering that a `for name in asked` implementation would get wrong.
    register(gating=("tests", "security"))
    outcome = asyncio.run(_registered()(ctx, "main", "HEAD", comments=str(tmp_path / "bodies")))
    assert sorted(adapter.asked) == ["security", "tests"], "a lens that does not gate was paid for"
    assert [s.step for s in ctx.steps] == ["security", "tests"]
    assert sorted(p.name for p in (tmp_path / "bodies").glob("*.md")) == ["security.md", "tests.md"]
    assert outcome.status is Status.SUCCEEDED
    # Still every declared lens, unchanged: the selection is the check's, not the adapter's.
    assert bound_lenses(ctx) == ("intent", "licensing", "performance", "security", "tests")


def test_gate_review_9_a_lens_that_does_not_gate_is_still_reachable_from_the_thread(
    registry: None,
) -> None:
    """`GATE-REVIEW-9`, and the property the obvious implementation breaks.

    The wrong fix for #449 is to narrow `AiReview(lenses=...)`, because `chatops.aspect_from`
    resolves `/review <lens>` against that same map -- so dropping a lens to keep it off the check
    also puts it out of reach of the comment asking for it. Declaring the gating set must leave
    what the adapter declares exactly as it was, which is what this asserts from the chat-ops side.
    """
    from types import SimpleNamespace

    from in_lockstep.adapters.ai.review import Review
    from in_lockstep.platform.chatops import aspect_from

    adapter = _Lensed("security", "intent", "performance", "tests", "licensing")
    register(gating=("security", "tests"))

    # AFTER a gating run, over the same adapter object -- not over a fresh one. The narrowing this
    # guards against is as easy to write at run time as at binding time, and a check made before
    # the run would not see it.
    asyncio.run(_registered()(_ctx(adapter), "main", "HEAD"))
    assert sorted(adapter.asked) == ["security", "tests"]

    container = Container()
    container.bind(Review, adapter)
    known = _review_lenses(SimpleNamespace(container=container))
    assert known == ("intent", "licensing", "performance", "security", "tests"), (
        "the check narrowed what the adapter declares, which is the thread's set too"
    )
    assert aspect_from("/review performance", known=known) == "performance", (
        "a bound lens that does not gate must still be nameable on a pull request"
    )


def test_gate_review_9_a_gating_lens_the_adapter_does_not_declare_refuses_before_the_fan_out(
    registry: None,
) -> None:
    """`GATE-REVIEW-9`. A typo gates on fewer lenses than somebody meant, and a check that goes
    green for the wrong reason is the one failure a required check must not have. Refused by name,
    naming both the unknown lens and the set that exists, with zero model calls -- and BLOCKED
    exits 3, so the check is red rather than green-over-nothing."""
    adapter = _Lensed("security", "intent", "performance", "tests")
    register(gating=("security", "test"))
    outcome = asyncio.run(_registered()(_ctx(adapter), "main", "HEAD"))
    assert outcome.status is Status.BLOCKED and outcome.reason == "review.gating_unknown"
    message = outcome.findings[0].message
    # Exact, not a substring search: "test" is a substring of "tests", so a loose assertion
    # here would pass against a refusal that named the declared set and never the typo.
    assert message.startswith("`register(gating=...)` names test, which"), message
    assert "intent, performance, security, tests" in message, "the refusal lists what does exist"
    assert adapter.asked == [], "a misconfigured gating set must not reach a model"


def test_gate_review_9_gating_on_nothing_is_refused_rather_than_read_as_gating_on_everything(
    registry: None,
) -> None:
    """`GATE-REVIEW-9`. `gating=()` and no `gating=` at all are opposite intentions, and the
    silent reading of the first is a required check that can never fail. Absent means every bound
    lens; empty means somebody wrote something that cannot mean anything, so it is refused."""
    adapter = _Lensed("security", "tests")
    register(gating=())
    outcome = asyncio.run(_registered()(_ctx(adapter), "main", "HEAD"))
    assert outcome.status is Status.BLOCKED and outcome.reason == "review.gating_empty"
    assert "security, tests" in outcome.findings[0].message
    assert adapter.asked == []


def test_gate_review_9_the_registered_workflow_takes_no_gating_argument(registry: None) -> None:
    """`GATE-REVIEW-9`, structurally. `in-lockstep run` builds its `--arg` surface by
    introspecting the registered callable, so a `gating` parameter visible there would be a lens
    list reachable from the trampoline YAML -- exactly the question `GATE-REVIEW-5` closed. This
    is why `register` re-declares the signature instead of `functools.wraps`, which sets
    `__wrapped__` and would make `inspect.signature` report the wrapped parameters."""
    import inspect

    register(gating=("security",))
    fn = _registered()
    assert list(inspect.signature(fn).parameters) == ["ctx", "base", "head", "comments", "diff"]
    assert not hasattr(fn, "__wrapped__")
    assert "gating" not in _parameters(fn), "the selection became a --arg"


def test_gate_review_9_this_repository_gates_on_every_lens_it_binds(registry: None) -> None:
    """`GATE-REVIEW-9`, the half that is about this repository rather than about adopters.

    An adopter chooses; here all of them gate, by decision. O10 exists so the shipped lenses are
    not things we ask adopters to trust on our word, and `intent`, `performance` and `tests` had
    already spent a release shipped-but-never-run once (#241). Asserted by running the workflow
    THIS module registered over a stub declaring five lenses: a selection of any kind would show
    up as fewer than five branches."""
    # Provenance first: what runs below has to be the closure THIS module installed, not one a
    # neighbouring test left in the registry. `register` builds a fresh `Registered` per call, so
    # an unchanged entry means `.lockstep/lockstep.py` never registered the check at all -- at
    # which point every assertion after this would be about somebody else's registration.
    before = get(ALL_LENSES)
    module, _ref = load(str(ROOT))
    entry = get(ALL_LENSES)
    assert entry is not None and entry is not before, (
        "this repository's module did not register review/all-lenses; the rest of this test would "
        "be asserting over a leftover registration"
    )

    adapter = _Lensed("security", "intent", "performance", "tests", "licensing")
    ctx = _ctx(adapter)
    # `Workflow` is annotated `Callable[..., Awaitable[Any]]`, which `asyncio.run` will not
    # take; the registry stores what it stores and the widening belongs here, not there.
    registered_fn: Any = entry.fn
    asyncio.run(registered_fn(ctx, "main", "HEAD"))
    assert sorted(adapter.asked) == ["intent", "licensing", "performance", "security", "tests"], (
        "this repository's registration narrowed the required check"
    )
    # Five over a stub says the registration narrows nothing; this says what it is not narrowing
    # HERE, so the row's claim is about this repository's real lens set rather than about a stub.
    _ensure_review_bound(module.lockstep)
    known = _review_lenses(module.lockstep)
    assert known is not None and {"intent", "performance", "security", "tests"} <= set(known)
