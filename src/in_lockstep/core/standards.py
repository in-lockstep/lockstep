"""Organisation standards, distributed as entry points.

Goal 9 is hierarchical inheritance — org, then team, then repository — and at two hundred
repositories a hand-imported convention is drift by another name: every repo that forgets the
import silently runs without the standards, and nothing says so. An entry point inverts that.
The organisation publishes a package; installing it is what applies it; and a repository that
wants it gone has to remove it from its dependencies, which is a reviewable diff rather than a
missing line.

The container's docstring promised this order from the start — explicit binds, then plugins,
then shipped defaults — while no `entry_points` call existed anywhere in the package. This module
is that call.

Three properties are load-bearing:

**A plugin is handed `Standards`, not `Lockstep`.** The facade forces every bind to
`Tier.PLUGIN` — a standards package cannot masquerade as the repository's own configuration —
and stamps every policy contribution with the plugin's source, so `ls` can answer where a layer
came from. The `PolicyStack` it contributes into is already tighten-only, so a plugin can lower
a ceiling and cannot raise one.

**The repository still wins.** Plugins load inside `Lockstep.detect()`, which is the first line
of a `lockstep.py`; every explicit line in that file runs after them, and `Tier.EXPLICIT` beats
`Tier.PLUGIN` regardless of order. Overriding one line of the organisation's package is one
`lockstep.bind(...)`.

**Failure is loud.** A standards package that fails to load means the standards are NOT applied,
and continuing without them is exactly the silently-dropped control this framework exists to
refuse. There is deliberately no environment variable to skip loading: the documented posture is
visibility of removal, not impossibility, and an env var is removal nothing reviews.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, TypeVar

from .container import Scope, Tier
from .policy import Policy

T = TypeVar("T")

GROUP = "in_lockstep.standards"


class StandardsError(RuntimeError):
    """A standards package failed to load, so its standards are not in force."""


class Standards:
    """What a standards package is handed: contribute and bind, nothing else.

    Not the `Lockstep` itself, on purpose. The middleware chain, the budget, the guard and the
    routes belong to the repository; a plugin that needs a ceiling contributes one through the
    policy stack, where it merges tighten-only and is printed with its source.
    """

    def __init__(self, lockstep: Any, *, source: str) -> None:
        self._lockstep = lockstep
        self._source = source

    def contribute(self, policy: Policy) -> None:
        if not policy.source:
            policy = replace(policy, source=self._source)
        self._lockstep.contribute(policy)

    def bind(
        self,
        iface: type[T],
        impl: T | type[T],
        *,
        name: str | None = None,
        scope: Scope = Scope.SINGLETON,
    ) -> None:
        # No `tier` parameter, which is the point: everything a plugin binds is `Tier.PLUGIN`.
        self._lockstep.bind(iface, impl, name=name, scope=scope, tier=Tier.PLUGIN)


def load_standards(lockstep: Any, entries: Any = None) -> list[str]:
    """Discover and apply every `in_lockstep.standards` entry point, in name order.

    Name order rather than discovery order, because discovery order is whatever the resolver's
    dict happened to yield and two machines disagreeing about which standard applied last is a
    support ticket nobody can reproduce. Returns the labels it loaded, which `ls` prints; the
    same list lands on `lockstep.standards`.
    """
    if entries is None:
        from importlib.metadata import entry_points

        entries = list(entry_points(group=GROUP))
    loaded: list[str] = []
    for entry in sorted(entries, key=lambda e: str(e.name)):
        label = str(entry.name)
        dist = getattr(entry, "dist", None)
        if dist is not None:
            label = f"{entry.name}  <- {dist.name} {dist.version}"
        try:
            hook = entry.load()
            hook(Standards(lockstep, source=f"plugin:{entry.name}"))
        except Exception as e:
            raise StandardsError(
                f"standards plugin {entry.name!r} failed to load: {e}. Its standards are NOT in "
                "force, and running without standards somebody installed is worse than stopping "
                "— fix or uninstall the package."
            ) from e
        loaded.append(label)
    lockstep.standards = loaded
    return loaded
