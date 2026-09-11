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

from in_lockstep.cli import _ensure_review_bound, _review_lenses
from in_lockstep.core.container import Container
from in_lockstep.core.context import RepoInfo, RunContext
from in_lockstep.core.outcome import Finding, Outcome, Status
from in_lockstep.core.verbs import Capability, Verb
from in_lockstep.core.workflow import restore, snapshot
from in_lockstep.loader import load
from in_lockstep.workflows.review import bound_lenses, review_all_lenses

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
