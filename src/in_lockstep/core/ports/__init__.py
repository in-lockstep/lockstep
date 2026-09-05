"""Protocols only — the outward edge of `core`.

The layering rule is that arrows point down. `RunContext` is the single seam through which all
capability flows, which means it names an SCM, a ticket source, a ledger and a notifier — and if
those names resolved to implementations, `core` would import `platform`, `human` and `notify`, and
the god object at the centre of the framework would invert the rule the architecture rests on.

So `core` imports this package and nothing else outward. Implementations live in their own
packages and register through the container. An import-linter contract holds it: a layering rule
with no enforcement does not survive seven phases.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class LedgerScope:
    """Whether a store is visible to more than one machine.

    A local filesystem CAS is implementable and semantically vacuous across CI runners, each of
    which has its own working directory. Park claims and fan-out barriers need a shared one; pure
    in-run fan-out needs no ledger at all. Scoping the store keeps both true without making
    `compare_and_set` a method that silently lies.
    """

    LOCAL = "local"
    SHARED = "shared"


class Unsupported(Exception):
    """Raised by a protocol default a given store does not implement."""


@runtime_checkable
class LedgerStore(Protocol):
    """Append-only run records, plus the compare-and-set park claims and barriers depend on.

    `compare_and_set` is declared from day one even though nothing calls it at 1.0: retrofitting a
    method onto a Protocol that third parties implement is a breaking change, and the shapes are
    what must be committed early.
    """

    scope: str

    async def append(self, run_id: str, record: dict[str, object]) -> None: ...

    async def read(self, run_id: str) -> dict[str, object] | None: ...

    async def compare_and_set(
        self, key: str, expected: str | None, new: str
    ) -> bool:  # pragma: no cover - default is a refusal
        raise Unsupported(
            "this LedgerStore does not provide compare-and-set; park and human fan-out branches "
            "require a store with LedgerScope.SHARED"
        )


@runtime_checkable
class StepStore(Protocol):
    """Where completed step outcomes are checkpointed.

    Declared here rather than imported from the platform package, because `RunContext` uses it and
    `core` may not depend on an implementation — that edge is the one inversion the layering rule
    exists to prevent, and a lazy import inside a function is still that edge.

    `save` takes an outcome-shaped mapping rather than an `Outcome`, so the store never needs to
    reach back into core either.
    """

    def save_step(self, run_id: str, step_id: str, outcome: object) -> None: ...

    def load_step(self, run_id: str, step_id: str) -> object | None: ...


@runtime_checkable
class InferenceLog(Protocol):
    """Where a run's model calls are kept. One log for the whole run.

    Declared here for the reason `StepStore` is: `RunContext` carries it and `core` may not depend
    on an implementation. Deliberately thin — `core` does not need to know how a call is written
    down, only that the run has somewhere to write it and can say how much it kept. The writing is
    `ai.replay`'s, where the request and answer types live.

    One object per run rather than one per invoker: a workflow's steps each build their own
    invoker, and two of them saving the same tape would lose whichever wrote first.
    """

    def calls(self) -> int: ...


@runtime_checkable
class SecretResolver(Protocol):
    """Resolves a named secret at the edge. Values never enter a context package."""

    def resolve(self, name: str) -> str: ...

    def known_values(self) -> frozenset[str]: ...


@runtime_checkable
class Tracer(Protocol):
    def span(self, name: str, **attributes: object) -> object: ...
