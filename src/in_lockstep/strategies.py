"""The shipped strategies.

An aspect is an agent, not a data row — which is why the four review lenses are four registrations
rather than one with a parameter, and what makes each independently budgetable and measurable.

`implement/tdd` is the interesting one, and it is not new: the compiler-era pipeline already ran
requirements → plan → tests → change as four chained agents. What changes is that the chain is now
a single strategy driving several invocations, interleaved with deterministic verbs — it can
actually run the tests it wrote and see them fail before implementing, which four independent
agents could not do.

The registry is deliberately small at 1.0: registering a strategy nobody has fixtures for is how
strategy sprawl starts, and ten unmeasured strategies are worse than one measured.

**One of these is executable and the rest are not, and the difference is visible from here.** A
registration whose factory returns a string is a catalogue entry — a name reserved, a description
of an approach nobody has written. `AiImplement` refuses one by name rather than failing on a
missing attribute, so the distinction is something you are told rather than something you deduce
from a traceback. That this file used to contain nothing but such entries is why the registry
looked like a dispatcher for a phase without being one.
"""

from __future__ import annotations

from collections.abc import Callable

from .adapters.ai.oneshot import OneshotImplement
from .ai.strategy import StrategyRegistry
from .core.verbs import Verb


def default_registry() -> StrategyRegistry:
    registry = StrategyRegistry()

    def lens(aspect: str) -> Callable[[], str]:
        return lambda: aspect

    for aspect in ("security", "intent", "performance", "tests"):
        registry.register(
            f"review/{aspect}",
            Verb.REVIEW,
            factory=lens(aspect),
            description=f"Review through the {aspect} lens.",
        )
    registry.default(Verb.REVIEW, "review/security")

    registry.register(
        "implement/oneshot",
        Verb.IMPLEMENT,
        factory=OneshotImplement,
        description=(
            "One session with read, search, write and run tools. Explores the repository, stages "
            "the change, reports what it could not do."
        ),
    )
    registry.register(
        "implement/tdd",
        Verb.IMPLEMENT,
        factory=lambda: "tdd",
        description="Red then green: write failing tests, confirm red, implement, re-test.",
    )
    registry.register(
        "implement/direct",
        Verb.IMPLEMENT,
        factory=lambda: "direct",
        description="Single shot. Cheapest, and the baseline the others are measured against.",
    )
    # The default is the one that runs. Defaulting to `implement/tdd` was defensible while nothing
    # dispatched strategies at all and indefensible the moment something did: `ctx.do(Implement,
    # ...)` with no explicit choice would have refused, and the registry's own default would have
    # been the reason. A default naming an approach nobody has written is a plan, not a default.
    registry.default(Verb.IMPLEMENT, "implement/oneshot")

    registry.register(
        "fix/diagnose-then-fix",
        Verb.FIX,
        factory=lambda: "diagnose",
        description="Diagnose, write a failing reproducer, fix, confirm the reproducer passes.",
    )
    registry.default(Verb.FIX, "fix/diagnose-then-fix")

    registry.register(
        "triage/rules-first",
        Verb.TRIAGE,
        factory=lambda: "rules",
        description="Rules where they settle it, a model where they do not.",
    )
    registry.default(Verb.TRIAGE, "triage/rules-first")

    # The improvement loop proposes changes to prompts and skills, so it holds a path grant — and
    # is therefore marked privileged, which makes it unreachable from a selection driven by
    # ticket labels or any other attacker-influenceable input.
    registry.register(
        "improve/propose",
        Verb.IMPLEMENT,
        factory=lambda: "improve",
        privileged=True,
        description="Propose prompt and routing changes as a pull request, with evidence.",
    )

    return registry
