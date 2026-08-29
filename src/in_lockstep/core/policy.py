"""The policy stack.

Sealing is not a binding. Bindings resolve repo-above-org by construction, so a container cannot
express "an organisation standard the repository may not weaken" — a repo's `bind` simply wins.
But guardrails were never a slot: they are an append-only registry whose merge is monotone.

So contributions append, there is no removal API, and the merge only ever tightens:

  * `deny-all` egress is an irreversible floor — once closed, a later contribution cannot reopen
    it. Otherwise a repository inheriting two upstreams could have the second silently undo the
    first's egress rule, decided by nothing but declaration order.
  * ceilings take the lowest, not the last — two contributions each setting one are two
    constraints, and satisfying only whichever was read last satisfies neither.
  * tool denies union.
  * the strictest scan setting wins.

What this preserves is *visibility of removal*, not impossibility. A repository can still delete
the line that contributes the standard — but that is a reviewable diff, which is what the compiler
guaranteed too. A middleware chain cannot bound code that never calls `ctx.do`; enforcement that
must survive a hostile repository owner lives in CI required checks and provider billing limits,
and saying so is more honest than implying otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

SCAN_STRENGTH = {"": 0, "warn": 1, "block": 2}


@dataclass(frozen=True)
class Policy:
    """One contribution to the stack."""

    name: str
    source: str = ""
    permissions: str = ""
    network: str = ""
    deny_tools: tuple[str, ...] = ()
    scan_input: str = ""
    max_turns: int | None = None
    max_ai_credits: int | None = None
    per_run_ai_credits: int | None = None
    daily_ai_credits: int | None = None


@dataclass(frozen=True)
class ResolvedPolicy:
    permissions: str = ""
    network: str = ""
    deny_tools: tuple[str, ...] = ()
    scan_input: str = ""
    max_turns: int | None = None
    max_ai_credits: int | None = None
    per_run_ai_credits: int | None = None
    daily_ai_credits: int | None = None


def _lowest(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


class PolicyStack:
    """Append-only. Deliberately exposes no way to remove or weaken a contribution."""

    def __init__(self) -> None:
        self._layers: list[Policy] = []

    def contribute(self, policy: Policy) -> None:
        self._layers.append(policy)

    @property
    def layers(self) -> tuple[Policy, ...]:
        return tuple(self._layers)

    def resolve(self) -> ResolvedPolicy:
        merged = ResolvedPolicy()
        for layer in self._layers:
            if layer.permissions:
                merged = replace(merged, permissions=layer.permissions)
            # deny-all is a floor, not a setting.
            if layer.network and merged.network != "deny-all":
                merged = replace(merged, network=layer.network)
            if layer.deny_tools:
                merged = replace(
                    merged,
                    deny_tools=tuple(dict.fromkeys((*merged.deny_tools, *layer.deny_tools))),
                )
            if SCAN_STRENGTH.get(layer.scan_input, 0) > SCAN_STRENGTH.get(merged.scan_input, 0):
                merged = replace(merged, scan_input=layer.scan_input)
            merged = replace(
                merged,
                max_turns=_lowest(merged.max_turns, layer.max_turns),
                max_ai_credits=_lowest(merged.max_ai_credits, layer.max_ai_credits),
                per_run_ai_credits=_lowest(merged.per_run_ai_credits, layer.per_run_ai_credits),
                daily_ai_credits=_lowest(merged.daily_ai_credits, layer.daily_ai_credits),
            )
        return merged
