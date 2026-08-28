"""The chain of actors.

Composed by plain function calls — no decorator stacking, no metaclass indirection — so `pdb`
lands where you expect and a traceback reads as a stack of ordinary frames. `--no-middleware`
exists for bisecting behaviour.

Because `next` is explicit, a middleware can act before, after, around, or instead. That is the
whole IoC hook requirement without a separate before/after registration API to keep in sync.

What `--no-middleware` may NOT disable is the privileged tier: redaction, egress policy, residency,
and the kill switch. Those are not middleware. A debugging flag that can switch off the thing
keeping credentials out of a git-committed record is not a debugging flag.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, runtime_checkable

from .outcome import Outcome
from .verbs import Capability, Verb

Next = Callable[[], Awaitable[Outcome[object]]]


class ActionCall:
    """A declared invocation. Declared, not started.

    Fan-out needs branches it can describe before running them, and retrofitting that onto an
    eager-only entry point would re-plumb the whole chain. So `ctx.do` is defined as
    `ctx.call` followed by `ctx.run_call`, and the declared form exists from the start even though
    nothing fans out at 1.0.
    """

    __slots__ = ("verb", "iface", "input", "using", "step", "strategy", "middleware")

    def __init__(
        self,
        verb: Verb | None,
        iface: type[object],
        input: object,
        *,
        using: str | None = None,
        step: str | None = None,
        strategy: str | None = None,
        middleware: Sequence[Middleware] | None = None,
    ) -> None:
        self.verb = verb
        self.iface = iface
        self.input = input
        self.using = using
        self.step = step
        self.strategy = strategy
        self.middleware = tuple(middleware or ())

    def __repr__(self) -> str:
        name = self.verb.value if self.verb else self.iface.__name__
        return f"ActionCall({name}, step={self.step!r})"


@runtime_checkable
class Middleware(Protocol):
    async def __call__(self, ctx: object, call: ActionCall, next: Next) -> Outcome[object]: ...


def compose(middleware: Sequence[Middleware], terminal: Next, ctx: object, call: ActionCall) -> Next:
    """Fold the chain into a single awaitable, innermost last.

    Built by closure over plain calls so each layer is one frame in a traceback.
    """
    chain = terminal
    for layer in reversed(list(middleware)):

        def step(layer: Middleware = layer, nxt: Next = chain) -> Awaitable[Outcome[object]]:
            return layer(ctx, call, nxt)

        chain = step
    return chain


class RefusesBudgetedActions:
    """Mixin for middleware that must not re-invoke an action which spends money.

    Retrying at the action boundary re-runs a whole agentic loop and re-pays every turn already
    spent. Retry belongs at the transport, where one HTTP attempt is one HTTP attempt.
    """

    @staticmethod
    def spends_budget(call: ActionCall, capabilities: frozenset[Capability]) -> bool:
        return Capability.SPENDS_BUDGET in capabilities
