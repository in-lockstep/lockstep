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
from .verbs import Capability, Verb, capabilities_of

Next = Callable[[], Awaitable[Outcome[object]]]


class ActionCall:
    """A declared invocation. Declared, not started.

    Fan-out needs branches it can describe before running them, and retrofitting that onto an
    eager-only entry point would re-plumb the whole chain. So `ctx.do` is defined as
    `ctx.call` followed by `ctx.run_call`, and the declared form exists from the start even though
    nothing fans out at 1.0.

    The request object carries both the payload and the dispatch key: `iface` is its type, and the
    container resolves an adapter for that type. `verb` starts empty and is stamped after
    resolution, from whatever adapter the binding chose.
    """

    __slots__ = ("verb", "iface", "input", "step", "middleware")

    def __init__(
        self,
        request: object,
        *,
        step: str | None = None,
        middleware: Sequence[Middleware] | None = None,
    ) -> None:
        self.verb: Verb | None = None
        self.iface: type[object] = type(request)
        self.input = request
        self.step = step
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


def provides_approval(layer: object) -> bool:
    """Whether a middleware layer is an approval path.

    Declared by the layer rather than recognised by class, for the same reason `Capability` is
    declared: an organisation routing approvals through its own system of record satisfies the
    requirement, and an `isinstance` check would tell it that it had not. `core` also cannot see
    `middleware/` — arrows point down — so a class check here would invert the rule outright.
    """
    return bool(getattr(layer, "provides_approval", False))


def capabilities_for(ctx: object, call: ActionCall) -> frozenset[Capability]:
    """What the action serving this call can do, or an empty set if nothing is bound.

    Every capability-aware middleware needs this and none of them can get it from the `ActionCall`:
    a call names a *request type*, and capabilities belong to whatever is bound to serve it, which
    is the whole point of binding. `Retry` and `ApprovalGate` each open-coded the same four lines,
    and the obvious guess — `capabilities_of(call)` — silently returns an empty set, which fails
    *open* for both of them. A helper that fails closed by construction is worth more than the four
    lines.
    """
    container = getattr(ctx, "container", None)
    if container is None or not container.has(call.iface):
        return frozenset()
    return capabilities_of(container.resolve(call.iface))


class RefusesBudgetedActions:
    """Mixin for middleware that must not re-invoke an action which spends money.

    Retrying at the action boundary re-runs a whole agentic loop and re-pays every turn already
    spent. Retry belongs at the transport, where one HTTP attempt is one HTTP attempt.
    """

    @staticmethod
    def spends_budget(call: ActionCall, capabilities: frozenset[Capability]) -> bool:
        return Capability.SPENDS_BUDGET in capabilities
