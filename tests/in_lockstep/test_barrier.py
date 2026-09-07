"""GATE-OUT-6, second clause: eight concurrent barrier ticks launch the continuation exactly once.

Over eight `GitLedger(shared=True)` clones of one bare origin -- the real store, the real swap --
with a negative control the way `GATE-ASYNC-4` keeps one: the same ticks over a store whose swap
is forced synchronous must run one after another, which the test detects, or it proves only that
a loop exists.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from in_lockstep.core.human import HumanBoundary, HumanBranch, Resumption, barrier_key, barrier_record, dumps
from in_lockstep.platform.barrier import BarrierError, Tick, read_barrier, tick
from in_lockstep.platform.ledger import GitLedger


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "a").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True, capture_output=True)
    return path


def _clones(tmp_path: Path, count: int) -> list[GitLedger]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    out = []
    for i in range(count):
        clone = _repo(tmp_path / f"c{i}")
        subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=clone, check=True)
        out.append(GitLedger(root=clone, shared=True))
    return out


class _SlowSwap(GitLedger):
    """A swap that takes as long as a network push, so every tick reads before any writes."""

    def _swap(self, key: str, expected: str | None, new: str) -> bool:
        time.sleep(0.25)
        return super()._swap(key, expected, new)


class _SerialSwap(_SlowSwap):
    """The negative control: the same slow swap awaited on the event loop instead of in a thread,
    which serialises every tick behind it."""

    async def compare_and_set(self, key: str, expected: str | None, new: str) -> bool:
        return self._swap(key, expected, new)


def _park(ledger: GitLedger) -> None:
    record = barrier_record(
        "run-1",
        resume="release/after",
        head="abc",
        human={"": HumanBranch(HumanBoundary.pr_review(41, reviewer="tim"))},
    )
    assert asyncio.run(ledger.compare_and_set(barrier_key("run-1"), None, dumps(record)))


async def _eight_ticks(ledgers: list[GitLedger], launches: list[Resumption]) -> list[Tick]:
    async def launch(resumption: Resumption) -> None:
        launches.append(resumption)

    event = Resumption(
        parent_run_id="run-1", branch="", event_id="review-9001", actor="tim", verdict="approved"
    )
    return list(await asyncio.gather(*(tick(led, event=event, launch=launch) for led in ledgers)))


def test_gate_out_6_eight_concurrent_ticks_launch_the_continuation_once(tmp_path: Path) -> None:
    """GATE-OUT-6. The same event, delivered to eight ticks at once -- a webhook redelivered, a
    review submitted twice, two engineers resuming together: one tick's swap lands and launches,
    the other seven lose the swap, re-read, and find the event already applied. One launch, seven
    re-reads, and the continuation sees the join."""
    ledgers: list[GitLedger] = [_SlowSwap(root=led.root, shared=True) for led in _clones(tmp_path, 8)]
    _park(ledgers[0])
    launches: list[Resumption] = []
    started = time.monotonic()
    ticks = asyncio.run(_eight_ticks(ledgers, launches))
    elapsed = time.monotonic() - started

    assert len(launches) == 1, [t.launched for t in ticks]
    assert sum(t.launched for t in ticks) == 1
    assert sum(t.duplicate for t in ticks) == 7
    assert sum(t.rereads for t in ticks) == 7, [t.rereads for t in ticks]
    assert elapsed < 8 * 0.25, f"eight ticks took {elapsed:.2f}s; they ran one after another"
    (resumption,) = launches
    assert resumption.actor == "tim" and resumption.verdict == "approved" and resumption.affirmative
    assert (
        resumption.join[""]["status"] == "succeeded" and resumption.join[""]["boundary"]["reviewer"] == "tim"
    )
    held = asyncio.run(read_barrier(ledgers[3], "run-1"))
    assert held is not None and held["complete"] is True and len(held["events"]) == 1


def test_gate_out_6_the_negative_control_serialises_and_the_test_can_tell(tmp_path: Path) -> None:
    """The control `GATE-ASYNC-4` keeps, for the same reason: with the swap awaited on the loop
    instead of run in a thread, the eight ticks run one after another -- the wall time is their sum, and the
    losers lose on the porcelain `=` -- the ref already held their value -- which the exit code
    alone reported as a win, launching eight continuations. If this stopped failing the assertion
    above, the assertion would be proving that a loop exists."""
    ledgers: list[GitLedger] = [_SerialSwap(root=led.root, shared=True) for led in _clones(tmp_path, 8)]
    _park(ledgers[0])
    launches: list[Resumption] = []
    started = time.monotonic()
    ticks = asyncio.run(_eight_ticks(ledgers, launches))
    elapsed = time.monotonic() - started
    assert len(launches) == 1, "serialised or not, the continuation launches once"
    assert sum(t.launched for t in ticks) == 1
    assert elapsed >= 8 * 0.25, (
        f"the control ran concurrently ({elapsed:.2f}s); it no longer controls anything"
    )


def test_a_tick_on_a_run_that_never_parked_refuses_by_name(tmp_path: Path) -> None:
    (ledger,) = _clones(tmp_path, 1)

    async def never(_r: Any) -> None:
        raise AssertionError("nothing to launch")

    event = Resumption(parent_run_id="ghost", branch="", event_id="e", actor="x", verdict="approved")
    with pytest.raises(BarrierError, match="ghost"):
        asyncio.run(tick(ledger, event=event, launch=never))


def test_a_join_with_two_human_branches_launches_on_the_second_event_only(tmp_path: Path) -> None:
    """Two people must act; the first event applies and waits, the second completes and launches,
    and the launch carries both verdicts."""
    a, b = _clones(tmp_path, 2)
    record = barrier_record(
        "run-2",
        resume="release/after",
        head="abc",
        machine={"tests": {"status": "succeeded", "reason": None, "decided": True}},
        human={
            "tim": HumanBranch(HumanBoundary.pr_review(41)),
            "ann": HumanBranch(HumanBoundary.pr_review(41)),
        },
    )
    assert asyncio.run(a.compare_and_set(barrier_key("run-2"), None, dumps(record)))
    launches: list[Resumption] = []

    async def launch(r: Resumption) -> None:
        launches.append(r)

    first = asyncio.run(tick(a, event=Resumption("run-2", "tim", "e1", "tim", "approved"), launch=launch))
    assert not first.launched and not first.complete and launches == []
    second = asyncio.run(
        tick(b, event=Resumption("run-2", "ann", "e2", "ann", "changes_requested"), launch=launch)
    )
    assert second.launched and second.complete
    (r,) = launches
    assert r.join["tim"]["status"] == "succeeded" and r.join["ann"]["status"] == "failed"
    assert r.join["tests"] == {"status": "succeeded", "reason": None, "decided": True, "kind": "machine"}
