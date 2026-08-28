"""Strategies — how an AI adapter approaches the work.

Binding chooses *which adapter* serves a verb; a strategy chooses *how it goes about it*. That
split matters because the interesting variation in AI work is not which class runs, it is whether
the approach is single-shot, explore-then-act, or red-green — and those want to be measured
against each other on the same ground truth rather than argued about.

So `strategy_id` is part of the eval subject key. A strategy nobody measured is a strategy nobody
should route to, and the way to keep that honest is to make measurement the default rather than an
afterthought.

One thing selection may NOT do: a strategy carrying a path grant must not be reachable from
attacker-influenceable input. Ticket labels can steer selection, so a grant keyed on a strategy id
is a grant an injected ticket can acquire.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, runtime_checkable

from ..core.outcome import Outcome
from ..core.verbs import Verb


@runtime_checkable
class Strategy(Protocol):
    id: ClassVar[str]
    verb: ClassVar[Verb]

    async def execute(self, ctx: Any, ai: Any, inp: Any) -> Outcome[Any]: ...


class UnknownStrategy(Exception):
    """A strategy id nothing is registered under."""


class StrategyRefused(Exception):
    """Selection reached a strategy it must not be able to reach."""


@dataclass
class Registration:
    id: str
    verb: Verb
    factory: Callable[[], Any]
    # Strategies holding a path grant may only be selected explicitly, never by a rule over
    # untrusted input.
    privileged: bool = False
    description: str = ""


@dataclass
class StrategyRegistry:
    registrations: dict[str, Registration] = field(default_factory=dict)
    defaults: dict[Verb, str] = field(default_factory=dict)

    def register(
        self,
        strategy_id: str,
        verb: Verb,
        factory: Callable[[], Any],
        *,
        privileged: bool = False,
        description: str = "",
    ) -> None:
        self.registrations[strategy_id] = Registration(
            id=strategy_id,
            verb=verb,
            factory=factory,
            privileged=privileged,
            description=description,
        )

    def default(self, verb: Verb, strategy_id: str) -> None:
        if strategy_id not in self.registrations:
            raise UnknownStrategy(f"no strategy registered as {strategy_id!r}")
        self.defaults[verb] = strategy_id

    def select(
        self,
        verb: Verb,
        *,
        explicit: str | None = None,
        from_untrusted_input: bool = False,
    ) -> Registration:
        """Most specific wins: an explicit choice, then the registered default.

        `from_untrusted_input` marks a selection driven by something like a ticket label. Such a
        selection may not land on a privileged strategy, because that is the path by which an
        injected ticket would acquire write access to the instructions for every future run.
        """
        strategy_id = explicit or self.defaults.get(verb)
        if strategy_id is None:
            raise UnknownStrategy(f"no strategy selected or defaulted for {verb.value}")
        registration = self.registrations.get(strategy_id)
        if registration is None:
            raise UnknownStrategy(
                f"no strategy registered as {strategy_id!r}; have {sorted(self.registrations)}"
            )
        if registration.privileged and from_untrusted_input:
            raise StrategyRefused(
                f"{strategy_id!r} carries a path grant and cannot be selected from "
                "attacker-influenceable input"
            )
        return registration

    def for_verb(self, verb: Verb) -> list[Registration]:
        return sorted((r for r in self.registrations.values() if r.verb is verb), key=lambda r: r.id)
