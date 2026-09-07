"""The barrier tick: a human event applied to a parked run's record, exactly once across machines.

Design §13.3 and §15.3. The record lives in the shared ledger's state space under
`barrier/<run_id>`; a tick reads it, applies the event (`core.human.apply_event` decides what that
means), and compare-and-sets the new text against the text it read. A lost swap is re-read and
retried, with a jittered pause so eight ticks do not re-collide in step; a duplicate event is
told so and applies nothing. The write that makes every branch terminal is the one that launches
the continuation, and because the store's swap is atomic that write happens once however many
ticks raced for it (`GATE-OUT-6`).

The launch is a callable handed in, not a workflow run here: this module knows the record and
the store, and the CLI knows how a fresh run with a `parent_run_id` is started. A test hands in
a counter.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..core.human import Resumption, apply_event, barrier_key, dumps, join_view


class BarrierError(RuntimeError):
    """A tick that could not be applied: no record, a store that cannot swap, or a swap lost more
    times than a race between real machines could plausibly lose it."""


@dataclass(frozen=True)
class Tick:
    """What one tick came to. `launched` is true for exactly one tick per barrier; `rereads`
    counts the swaps this tick lost before its own landed or it learned it was a duplicate."""

    launched: bool
    duplicate: bool
    complete: bool
    rereads: int
    record: dict[str, Any]


Launch = Callable[[Resumption], Awaitable[Any]]


async def read_barrier(ledger: Any, run_id: str) -> dict[str, Any] | None:
    text = await ledger.state(barrier_key(run_id))
    if text is None:
        return None
    parsed = json.loads(text)
    return parsed if isinstance(parsed, dict) else None


async def tick(
    ledger: Any,
    *,
    event: Resumption,
    launch: Launch,
    attempts: int = 32,
    pause: float = 0.02,
) -> Tick:
    """Apply `event` to the barrier of `event.parent_run_id`, and launch the continuation if this
    is the write that completed it."""
    key = barrier_key(event.parent_run_id)
    rereads = 0
    for _attempt in range(attempts):
        text = await ledger.state(key)
        if text is None:
            raise BarrierError(f"no barrier record for run {event.parent_run_id!r}; is it parked?")
        record = json.loads(text)
        new, duplicate, completed_now = apply_event(record, branch=event.branch, event=event)
        if duplicate:
            return Tick(
                launched=False,
                duplicate=True,
                complete=bool(record.get("complete")),
                rereads=rereads,
                record=record,
            )
        if await ledger.compare_and_set(key, text, dumps(new)):
            if completed_now:
                resumption = Resumption(
                    parent_run_id=event.parent_run_id,
                    branch=event.branch,
                    event_id=event.event_id,
                    actor=event.actor,
                    verdict=event.verdict,
                    text=event.text,
                    payload=dict(new.get("payload") or {}),
                    join=join_view(new),
                    stale=event.stale,
                )
                await launch(resumption)
            return Tick(
                launched=completed_now,
                duplicate=False,
                complete=bool(new["complete"]),
                rereads=rereads,
                record=new,
            )
        # Lost the swap to another tick: re-read and try again. Jittered so the losers of one
        # race do not all retry in the same instant and lose the next one to each other too.
        rereads += 1
        await asyncio.sleep(pause * (0.5 + random.random()))  # noqa: S311 - jitter, not security
    raise BarrierError(
        f"gave up applying event {event.event_id!r} to run {event.parent_run_id!r} after "
        f"{attempts} lost swaps; the store is contended beyond what a barrier expects"
    )


__all__ = ["BarrierError", "Tick", "read_barrier", "tick"]
