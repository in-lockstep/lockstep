"""Phase-5 gates: a run killed mid-flight resumes rather than starting over."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

import pytest

from in_lockstep.core.container import Container
from in_lockstep.core.context import RepoInfo, RunContext
from in_lockstep.core.outcome import Cost, Outcome, Status
from in_lockstep.core.verbs import Capability, Verb
from in_lockstep.platform.state import Checkpoint, StateStore


class Counting:
    verb: ClassVar[Verb] = Verb.TEST
    capabilities: ClassVar[frozenset[Capability]] = frozenset()

    def __init__(self, *, fail_on: int | None = None) -> None:
        self.calls = 0
        self.fail_on = fail_on

    async def invoke(self, ctx, inp):
        self.calls += 1
        if self.fail_on is not None and self.calls == self.fail_on:
            raise RuntimeError("the runner was killed here")
        return Outcome(status=Status.SUCCEEDED, value=f"{inp}-{self.calls}", cost=Cost(usd=0.01))


class Thing:
    pass


def ctx(tmp_path: Path, adapter, *, recovering: bool = False, run_id: str = "r1") -> RunContext:
    container = Container()
    container.bind(Thing, adapter)
    return RunContext(
        run_id=run_id,
        repo=RepoInfo(root="."),
        container=container,
        state=StateStore(tmp_path),
        recovering=recovering,
    )


def test_a_completed_step_is_replayed_not_rerun(tmp_path: Path) -> None:
    adapter = Counting()
    first = ctx(tmp_path, adapter)
    original = asyncio.run(first.do(Thing, "payload", step="one"))
    assert adapter.calls == 1

    resumed = ctx(tmp_path, adapter, recovering=True)
    replayed = asyncio.run(resumed.do(Thing, "payload", step="one"))
    assert adapter.calls == 1, "the step must not run twice"
    assert replayed.value == original.value
    assert any(f.id == "recover.replayed" for f in replayed.findings)


def test_recovery_continues_past_the_step_that_died(tmp_path: Path) -> None:
    """The scenario: a CI timeout in the middle of a multi-step run."""
    adapter = Counting(fail_on=2)
    first = ctx(tmp_path, adapter)
    asyncio.run(first.do(Thing, "a", step="one"))
    with pytest.raises(RuntimeError, match="killed"):
        asyncio.run(first.do(Thing, "b", step="two"))

    healthy = Counting()
    resumed = ctx(tmp_path, healthy, recovering=True)
    replayed = asyncio.run(resumed.do(Thing, "a", step="one"))
    fresh = asyncio.run(resumed.do(Thing, "b", step="two"))

    assert healthy.calls == 1, "step one replayed, step two ran"
    assert "recover.replayed" in {f.id for f in replayed.findings}
    assert "recover.replayed" not in {f.id for f in fresh.findings}


def test_without_a_store_it_is_just_a_python_function(tmp_path: Path) -> None:
    """Checkpointing is opt-out-able; that is the simplicity the design trades on."""
    adapter = Counting()
    container = Container()
    container.bind(Thing, adapter)
    plain = RunContext(run_id="r", repo=RepoInfo(root="."), container=container)
    asyncio.run(plain.do(Thing, "x"))
    asyncio.run(plain.do(Thing, "x"))
    assert adapter.calls == 2
    assert not (tmp_path / "r").exists()


def test_presence_is_not_success(tmp_path: Path) -> None:
    """The old resume primitive was 'an output file exists', which cannot tell these apart."""
    store = StateStore(tmp_path)
    store.save("r", Checkpoint(step_id="s", status="failed", reason="tests red"))
    loaded = store.load("r", "s")
    assert loaded is not None
    outcome = loaded.as_outcome()
    assert outcome.status is Status.FAILED, "a failed step replays as failed, not as done"
    assert outcome.reason == "tests red"


def test_a_torn_checkpoint_is_rerun_not_trusted(tmp_path: Path) -> None:
    """Trusting a half-written checkpoint makes recovery produce a wrong answer, not a slow one."""
    store = StateStore(tmp_path)
    directory = store.run_dir("r")
    directory.mkdir(parents=True)
    (directory / "s.json").write_text('{"step_id": "s", "status": "succ')
    assert store.load("r", "s") is None


def test_checkpoints_are_written_atomically(tmp_path: Path) -> None:
    """The failure being recovered from is a process dying at an arbitrary moment."""
    store = StateStore(tmp_path)
    store.save("r", Checkpoint(step_id="s", status="succeeded"))
    leftovers = list(store.run_dir("r").glob("*.tmp"))
    assert leftovers == [], "no temporary files survive a completed write"
    assert store.load("r", "s") is not None


def test_only_terminal_outcomes_are_checkpointed(tmp_path: Path) -> None:
    """A parked run is waiting, not done; replaying it as done would skip the human."""
    parked = Outcome(status=Status.PARKED, reason="pr_review")
    assert not parked.terminal


def test_an_unserializable_value_is_recorded_as_absent(tmp_path: Path) -> None:
    """A repr that looks like a value is worse than a missing one."""

    class Opaque:
        pass

    store = StateStore(tmp_path)
    store.save("r", Checkpoint.of("s", Outcome(status=Status.SUCCEEDED, value=Opaque())))
    loaded = store.load("r", "s")
    assert loaded is not None and loaded.value is None


def test_completed_steps_are_listable(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.save("r", Checkpoint(step_id="one", status="succeeded"))
    store.save("r", Checkpoint(step_id="two", status="failed"))
    assert store.completed("r") == ["one", "two"]
