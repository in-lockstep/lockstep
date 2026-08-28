"""RunContext — the single seam through which all capability flows.

Which is also the single place to fake everything in tests. Context is passed explicitly; an
ambient contextvar exists for library authors, but user code threads `ctx`.

`ctx.do` is defined as `ctx.call` followed by `ctx.run_call`, not the other way round. Branches
have to be describable before they run, and an entry point that only ever executes eagerly would
have to be re-plumbed later to allow it.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

from .container import Container
from .middleware import ActionCall, Middleware, Next, compose
from .outcome import Outcome, Status
from .ports import StepStore
from .spend import Spend
from .verbs import Verb, capabilities_of, verb_of

T = TypeVar("T")

DISABLE_ENV = "IN_LOCKSTEP_DISABLE"

_current: ContextVar[RunContext | None] = ContextVar("in_lockstep_run_context", default=None)


def current_context() -> RunContext | None:
    """Ambient access, for library authors. User code is encouraged to thread `ctx` explicitly."""
    return _current.get()


@dataclass(frozen=True)
class RepoInfo:
    root: str
    head: str = ""
    branch: str = ""
    dirty: bool = False


@dataclass(frozen=True)
class StepId:
    """Identity of one step, for checkpointing and cassette lookup.

    `scope_path` is empty at 1.0 and is the reason this is a structure rather than a string: when
    branches arrive, each needs its own key space, and a flat one would invalidate every cassette
    recorded before it.
    """

    scope_path: str
    call_site: str
    input_hash: str

    def __str__(self) -> str:
        prefix = f"{self.scope_path}/" if self.scope_path else ""
        return f"{prefix}{self.call_site}#{self.input_hash[:12]}"


def _hash_input(value: object) -> str:
    try:
        import json

        payload = json.dumps(value, sort_keys=True, default=repr)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        payload = repr(value)
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class RunContext:
    run_id: str
    repo: RepoInfo
    container: Container
    spend: Spend = field(default_factory=Spend)
    middleware: list[Middleware] = field(default_factory=list)
    tracer: Any = None
    scope_path: str = ""
    parent_run_id: str | None = None
    # Set to a StateStore to make steps resumable. Opt-out-able: without one the model is "just a
    # Python function", which is the simplicity the whole design trades on.
    state: StepStore | None = None
    recovering: bool = False
    _step_counts: dict[str, int] = field(default_factory=dict, repr=False)
    last_step: StepId | None = None
    last_capabilities: frozenset[Any] = frozenset()

    # -- declaring and running work ------------------------------------------------

    def call(
        self,
        iface: type[Any],
        inp: object,
        *,
        using: str | None = None,
        step: str | None = None,
        strategy: str | None = None,
        middleware: Sequence[Middleware] | None = None,
    ) -> ActionCall:
        """Declare an invocation without starting it."""
        return ActionCall(
            verb=None,
            iface=iface,
            input=inp,
            using=using,
            step=step,
            strategy=strategy,
            middleware=middleware,
        )

    async def run_call(self, call: ActionCall) -> Outcome[Any]:
        """Resolve the bound adapter, wrap it in the chain, and record the step."""
        # The kill switch is checked before anything else, including middleware. It is not part
        # of the chain, so `--no-middleware` cannot reach past it and neither can a bug in a
        # layer that would otherwise run first.
        if os.environ.get(DISABLE_ENV):
            return Outcome(
                status=Status.BLOCKED,
                reason="killswitch",
            )

        action: Any = self.container.resolve(call.iface, call.using)
        call.verb = verb_of(action)
        step_id = self._step_id(call)
        capabilities = capabilities_of(action)

        # A completed step is replayed rather than re-run. The checkpoint records the OUTCOME,
        # not merely that a file appeared: a step that wrote half its output before the runner
        # died must re-run, and presence alone cannot tell those apart.
        if self.recovering and self.state is not None:
            existing = self.state.load_step(self.run_id, str(step_id))
            if isinstance(existing, Outcome):
                self.last_step = step_id
                return existing

        started = time.monotonic()

        async def terminal() -> Outcome[Any]:
            result: Outcome[Any] = await action.invoke(self, call.input)
            # Charged here, innermost, rather than after the chain unwinds — otherwise a
            # middleware reconciling actual spend against its ceiling looks at the accumulator
            # before this call was ever added to it, and every overrun reads as within budget.
            if result.cost.wall_seconds == 0.0:
                result = result.with_cost(replace(result.cost, wall_seconds=time.monotonic() - started))
            self.spend.charge(result.cost)
            return result

        chain: Next = compose([*self.middleware, *call.middleware], terminal, self, call)
        outcome = await chain()

        if self.state is not None and outcome.terminal:
            self.state.save_step(self.run_id, str(step_id), outcome)

        self.last_step = step_id
        self.last_capabilities = capabilities
        return outcome

    async def do(
        self,
        iface: type[Any],
        inp: object,
        *,
        using: str | None = None,
        step: str | None = None,
        strategy: str | None = None,
        middleware: Sequence[Middleware] | None = None,
    ) -> Outcome[Any]:
        """Declare and run. The composition of `call` and `run_call`, and nothing more."""
        return await self.run_call(
            self.call(iface, inp, using=using, step=step, strategy=strategy, middleware=middleware)
        )

    # -- step identity -------------------------------------------------------------

    def _step_id(self, call: ActionCall) -> StepId:
        if call.step:
            call_site = call.step
        else:
            verb = call.verb.value if call.verb else call.iface.__name__.lower()
            seen = self._step_counts.get(verb, 0)
            self._step_counts[verb] = seen + 1
            call_site = verb if seen == 0 else f"{verb}.{seen}"
        return StepId(
            scope_path=self.scope_path,
            call_site=call_site,
            input_hash=_hash_input(call.input),
        )

    def bind_current(self) -> None:
        _current.set(self)


def killswitch_engaged() -> bool:
    return bool(os.environ.get(DISABLE_ENV))


__all__ = [
    "DISABLE_ENV",
    "RepoInfo",
    "RunContext",
    "StepId",
    "Verb",
    "current_context",
    "killswitch_engaged",
]
