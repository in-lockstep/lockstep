"""Acme's standards, applied to every repository that installs this package.

`apply` receives a `Standards` facade, not the `Lockstep` itself: it can contribute policy
layers (tighten-only — a plugin can lower a ceiling, never raise one) and bind implementations
at `Tier.PLUGIN` (a repository's own `lockstep.bind` outranks every one of them). It cannot
touch the middleware chain, the budget, or the routes; those belong to the repository.

Everything here is visible: `in-lockstep ls` prints this package under `standards`, each policy
layer below with `<- plugin:acme`, and each binding with its `plugin` tier. A repository that
wants one line different writes that line; a repository that wants none of it removes the
dependency — a reviewable diff either way.
"""

from __future__ import annotations

from typing import Any

from in_lockstep import Policy


def apply(std: Any) -> None:
    # The org-wide floor. `scan_input="block"` is strictest-wins in the stack, so no later
    # layer can soften it to "warn"; `max_turns` merges lowest, so a repository can tighten it
    # to 8 and cannot loosen it to 50.
    std.contribute(
        Policy(
            name="acme-baseline",
            scan_input="block",
            max_turns=16,
            deny_tools=("run_script",),
        )
    )
    # A binding example would go the same way — an org-standard cost table, say:
    #
    #   from in_lockstep.ai.pricing import CostTable, Rate
    #   table = CostTable()
    #   table.add("claude-haiku-4-5", Rate(1.0, 5.0))
    #   std.bind(CostTable, table)
    #
    # bound at Tier.PLUGIN, so any repository's own `lockstep.bind(CostTable, ...)` wins.
