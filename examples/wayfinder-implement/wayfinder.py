"""An `Implement` strategy that charts before it builds.

The framework ships no `Implement` adapter, so this file is what extending a verb actually looks
like: a marker class for the interface, a spec, and something that satisfies `Action`. Nothing
here is privileged — it is the same shape `PytestTest` has.

What makes it a *strategy* rather than just an adapter is that it changes how the verb proceeds
without changing what a workflow asks for. A workflow says `ctx.do(Implement, spec)`; whether that
means "write the code" or "chart the map and stop" is a binding decision.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from in_lockstep import Capability, Finding, Outcome, Severity, Status, Verb
from in_lockstep.ai.context import ContextPackage
from in_lockstep.platform.tickets.base import Ticket, TicketState

#: Declared, not borrowed. Before `Verb` was opened this had to reuse a shipped member, and a
#: charting run would have reported itself as `implement` in every span and metric even when it
#: deliberately implemented nothing.
CHART = Verb("chart")


class Implement:
    """The verb interface. Workflows ask for this; a binding decides what serves it."""


class Chart:
    """A verb of its own, because charting is not implementing.

    It writes nothing, spends differently, and succeeds by producing decisions. Sharing the
    `implement` label would have put both under one heading in every span, metric and step id, and
    the two runs a wayfinder map produces are exactly the two you want to tell apart.
    """


@dataclass(frozen=True)
class ImplementSpec:
    """One ticket, and the map it sits on.

    `tickets` is the whole map rather than just the target, because the frontier is a property of
    the map: whether a ticket may be claimed depends on what blocks it.
    """

    target: str
    tickets: tuple[Ticket, ...] = ()
    blocked_by: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Charting produces decisions; working produces a change. Wayfinder's two sessions.
    mode: str = "work"

    # The queries below are about the map, not about whoever is reading it — both adapters ask
    # the same questions, and duplicating them across two classes is how two adapters end up
    # disagreeing about what the frontier is.

    def find(self, key: str) -> Ticket | None:
        return next((t for t in self.tickets if t.key == key), None)

    def open_blockers(self, ticket: Ticket) -> tuple[str, ...]:
        """Blockers that are still open. A closed blocker no longer blocks."""
        states = {t.key: t.state for t in self.tickets}
        return tuple(
            key
            for key in self.blocked_by.get(ticket.key, ())
            if states.get(key, TicketState.OPEN) is not TicketState.CLOSED
        )

    def frontier(self) -> tuple[str, ...]:
        """Every ticket nothing open blocks. This is what may be claimed."""
        return tuple(t.key for t in self.tickets if not self.open_blockers(t))

    def fog(self) -> tuple[str, ...]:
        return tuple(t.key for t in self.tickets if _is_fog(t))

    def claimed(self) -> tuple[Ticket, ...]:
        return tuple(t for t in self.tickets if t.state is TicketState.IN_PROGRESS)


@dataclass(frozen=True)
class Map:
    """What a charting session produces. Not a deliverable — that is the point."""

    destination: str = ""
    frontier: tuple[str, ...] = ()
    fog: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()


class WayfinderImplement:
    """Implement, wayfinder-style: one frontier ticket, or a map and nothing else.

    Four of wayfinder's constraints are checkable without a model, so they are checks rather than
    prompt text. A rule stated only in a prompt is a request; the framework's whole argument is
    that the difference matters.
    """

    verb: ClassVar[Verb] = Verb.IMPLEMENT
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.READS_REPO, Capability.SPENDS_BUDGET}
    )

    def __init__(
        self,
        invoker_factory: Callable[[Any], Any] | None = None,
        *,
        max_tickets_per_session: int = 1,
    ) -> None:
        self.invoker_factory = invoker_factory
        # "One ticket per session" is wayfinder's sharpest rule and its least enforceable one as
        # prose. Here it is a number an adapter refuses to exceed.
        self.max_tickets_per_session = max_tickets_per_session

    async def invoke(self, ctx: Any, inp: ImplementSpec) -> Outcome[Map]:
        target = inp.find(inp.target)
        if target is None:
            return _blocked(
                "wayfinder.unknown_ticket",
                f"{inp.target!r} is not on this map. Wayfinder refers to tickets by name, and a "
                f"name that is not on the map is a ticket nobody charted.",
            )

        blockers = inp.open_blockers(target)
        if blockers:
            # The frontier is what is *unblocked*. Refusing here rather than letting a model
            # decide is the difference between a dependency graph and a suggestion.
            return _blocked(
                "wayfinder.not_on_frontier",
                f"{target.key} is blocked by {', '.join(blockers)}. The frontier is the set of "
                f"unblocked tickets; claiming behind it is how a map stops describing the work.",
            )

        claimed = inp.claimed()
        if len(claimed) > self.max_tickets_per_session:
            return _blocked(
                "wayfinder.session_scope",
                f"{len(claimed)} tickets are in progress and the session limit is "
                f"{self.max_tickets_per_session}. Wayfinder resolves one ticket per session so a "
                f"decision is attributable to the run that made it.",
            )

        return await self._work(ctx, inp, target)

    # -- working --------------------------------------------------------------------

    async def _work(self, ctx: Any, inp: ImplementSpec, target: Ticket) -> Outcome[Map]:
        if self.invoker_factory is None:
            return _blocked(
                "wayfinder.no_invoker",
                "working a ticket needs a model; bind an invoker factory, or run with mode='chart' "
                "to plan without one.",
            )

        package = ContextPackage(items=list(target.as_context()))
        invoker = self.invoker_factory(ctx)
        invocation = await invoker.run(
            system=_SYSTEM,
            messages=[],
            context=package,
            policy=None,
        )
        return Outcome(
            status=Status.SUCCEEDED,
            value=Map(destination=target.key, decisions=(invocation.content,)),
            decided=True,
            cost=invocation.cost,
        )


class WayfinderChart:
    """The first session: name the destination, map the frontier, and stop.

    Deterministic on purpose. Charting here reads the map's own structure — what blocks what, what
    has acceptance criteria — and none of that needs a model. Wayfinder's charting session does
    involve conversation; what this adapter contributes is the part that must not be a matter of
    opinion.

    It declares no `SPENDS_BUDGET`, so `ApprovalGate` and the egress trigger both leave it alone,
    and a charting run costs nothing.
    """

    verb: ClassVar[Verb] = CHART
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.READS_REPO})

    async def invoke(self, ctx: Any, inp: ImplementSpec) -> Outcome[Map]:
        # No frontier check, deliberately. The destination of a map is blocked by definition —
        # that is what makes the work foggy and worth charting — so requiring it to be claimable
        # would make the first session of any effort impossible. Running the example caught that.
        return self._chart(inp)

    def _chart(self, inp: ImplementSpec) -> Outcome[Map]:
        """Plan, do not deliver.

        `decided=True` and no `ChangeSet`, which is exactly the combination `Outcome` was built to
        express: a run that succeeded, produced evidence, and wrote nothing. A framework without
        that distinction would have to report charting as either a failure or a no-op.
        """
        frontier = inp.frontier()
        fog = inp.fog()
        chart = Map(
            destination=inp.target,
            frontier=frontier,
            fog=fog,
            decisions=(f"{len(frontier)} ticket(s) on the frontier", f"{len(fog)} still fog"),
        )
        # Reported as findings, not only on `value`. Findings are the framework's channel for
        # what a run noticed: they print, they reach the ledger, and a charting session whose map
        # lived only in a return value would be a run that succeeded and said nothing.
        findings = [
            Finding(
                id="wayfinder.frontier",
                message=(
                    f"claimable now: {', '.join(frontier)}"
                    if frontier
                    else "nothing is claimable; every ticket is blocked"
                ),
                severity=Severity.NOTE,
            ),
            Finding(
                id="wayfinder.destination",
                message=(
                    f"{inp.target} is blocked by {', '.join(inp.open_blockers(target))}"
                    if (target := inp.find(inp.target)) and inp.open_blockers(target)
                    else f"{inp.target} is reachable; the route is clear"
                ),
                severity=Severity.NOTE,
            ),
        ]
        findings += [
            Finding(
                id="wayfinder.fog",
                message=f"{key} is not sharp enough to phrase precisely yet; left as fog.",
                severity=Severity.NOTE,
            )
            for key in fog
        ]
        return Outcome(status=Status.SUCCEEDED, value=chart, decided=True, findings=tuple(findings))


def _is_fog(ticket: Ticket) -> bool:
    """Fog is work that cannot yet be phrased precisely.

    Read off the ticket rather than guessed at: a ticket with no acceptance criteria and no
    description has not been specified, whatever its title promises.
    """
    return not ticket.acceptance_criteria and not ticket.description.strip()


def _blocked(reason: str, message: str) -> Outcome[Map]:
    return Outcome.blocked_by(
        reason,
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )


_SYSTEM = (
    "You are resolving one ticket from a wayfinder map. Produce the decision the ticket asks for "
    "and nothing beyond it. Do not implement work belonging to other tickets, and do not resolve "
    "fog that has not been ticketed."
)

__all__ = [
    "CHART",
    "Chart",
    "Implement",
    "ImplementSpec",
    "Map",
    "WayfinderChart",
    "WayfinderImplement",
]
