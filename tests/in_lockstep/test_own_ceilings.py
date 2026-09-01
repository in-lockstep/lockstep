"""The ceilings this repository declares are the ceilings its runs actually get.

Three objects in `.lockstep/lockstep.py` carry a `max_turns`, and two of them carry something
spelled `max_tokens`. They do not mean the same thing, and the ways they disagree are all quiet:

  * `Workshop` bounds ONE invocation — turns around the loop, output tokens per call.
  * `Policy` is the tighten-only stack. `InvokePolicy.under` takes `min` of its `max_turns` and
    the workshop's, so raising the floor alone changes nothing and reads like it changed
    everything. It has no `max_tokens` at all.
  * `Budget` bounds the WHOLE RUN, its fields are `turns` and `tokens` rather than `max_*`, and
    `Spend` accumulates across every invocation in the run — so a run-scoped ceiling set to the
    per-invocation cap lets a two-phase strategy spend all of it in phase one.

Every one of those failed for real. A change raising the ceilings put `max_tokens` on `Policy` and
`max_turns`/`max_tokens` on `Budget`; the first was a `TypeError` that took the config out
entirely, and the other two would have been accepted silently by a config that named its fields
correctly and still meant nothing by them.

These read this repository's own lifecycle, because a scaffold that agrees with itself proves
nothing about the file that constrains the runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from in_lockstep.ai.invoker import InvokePolicy
from in_lockstep.core.workflow import restore, snapshot
from in_lockstep.loader import load

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def lockstep():
    """This repository's own lifecycle, loaded and then unregistered again."""
    state = snapshot()
    module, _ref = load(str(ROOT))
    yield module.lockstep
    restore(state)


def test_the_turn_ceiling_the_file_declares_is_the_one_a_run_is_given(lockstep) -> None:
    """The trap that makes raising a limit a no-op.

    `InvokePolicy.under` takes the lowest of the workshop's `max_turns` and the policy floor's,
    because a contributed floor may only tighten. So a floor raised to 100 over a workshop left at
    the shipped 30 yields 30 — a diff that reads as a change, a config that says 100, and a run
    that still stops at 30. Nothing fails; the next person just reads the wrong number.
    """
    workshop = lockstep.workshop
    effective = InvokePolicy.under(
        lockstep.policy.resolve(),
        max_turns=workshop.max_turns,
        max_tokens=workshop.max_tokens,
    )
    assert effective.max_turns == workshop.max_turns, (
        f"the policy floor caps runs at {effective.max_turns} turns while the workshop asks for "
        f"{workshop.max_turns}. Raise the floor to match, or lower the workshop to the truth."
    )
    assert effective.max_tokens == workshop.max_tokens


def test_the_run_budget_leaves_room_for_more_than_one_invocation(lockstep) -> None:
    """`Spend` is run-scoped: turns and tokens accumulate across every invocation.

    TDD runs two model phases in one run. A run-scoped ceiling equal to the per-invocation cap is
    therefore spent entirely by the red phase, and the green phase is refused for work it was
    never given room to attempt — reported as a budget refusal, which points at the wrong thing.

    Unset is the normal answer and passes: `usd` is the ceiling meant to bound a whole run, and it
    is the one checked against a projection before each turn.
    """
    budget, workshop = lockstep.budget, lockstep.workshop

    if budget.turns is not None:
        assert budget.turns > workshop.max_turns, (
            f"the run may spend {budget.turns} turns and one invocation may spend "
            f"{workshop.max_turns}, so a second phase gets nothing. Raise it or leave it unset."
        )
    if budget.tokens is not None:
        assert budget.tokens > workshop.max_tokens, (
            f"the run's total token ceiling ({budget.tokens}) is under one turn's output "
            f"({workshop.max_tokens}) — every run would refuse immediately. `Budget.tokens` is "
            f"the run total, not the per-request cap that shares the name on `Workshop`."
        )


def test_the_run_still_declares_a_ceiling_in_dollars(lockstep) -> None:
    """The one that has to be set. `UndeclaredBudget` refuses at startup for a lifecycle that
    binds something which spends and names no ceiling, and this file binds three such verbs — so
    an all-`None` budget here is a refusal at run time rather than a test failure at commit time,
    which is the more expensive place to find out."""
    assert lockstep.budget.declared
    assert lockstep.budget.usd is not None
