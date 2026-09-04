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


#: The one workflow allowed to state a ceiling on the command line. `lockstep.yml` runs `review`,
#: which is the workflow an adopter gets before they have written a lockstep.py at all — so a flag
#: there is the ceiling rather than a second copy of one.
BUDGET_ON_THE_COMMAND_LINE = {"lockstep.yml"}


def test_no_workflow_shadows_the_ceiling_the_lifecycle_declares() -> None:
    """`run --budget` REPLACES `lockstep.budget` rather than merging with it.

    That is right at a terminal — a number somebody typed is a number somebody meant — and it is a
    trap in CI, where nobody types anything and the literal simply outranks the module for good.
    Run 33564844360 is the demonstration: `lockstep.budget` had just been raised to $100, the
    workflow still said `--budget 25.00`, and the run was refused at `usd:25.1700>25.0000`. The
    ceiling in force and the ceiling in the diff were different numbers, and nothing said so.

    Worse than the dollars: `--budget X` builds `Budget(usd=X)`, so it also drops `wall_seconds`.
    A flag stating one ceiling silently removed another.
    """

    def states_one(text: str) -> bool:
        # Comment lines are skipped, or this fails on the paragraphs explaining why the flag is
        # absent — a check that cannot survive its own rationale being written down is one that
        # gets deleted rather than obeyed.
        return any("--budget" in line for line in text.splitlines() if not line.lstrip().startswith("#"))

    offenders = sorted(
        path.name
        for path in (ROOT / ".github" / "workflows").glob("*.yml")
        if states_one(path.read_text()) and path.name not in BUDGET_ON_THE_COMMAND_LINE
    )
    assert not offenders, (
        f"{', '.join(offenders)} state `--budget`, which replaces the ceiling `.lockstep/"
        f"lockstep.py` declares instead of merging with it. Raise the module's number and delete "
        f"the flag — two places to edit means one of them is wrong and neither says so."
    )


def test_the_run_still_declares_a_ceiling_in_dollars(lockstep) -> None:
    """The one that has to be set. `UndeclaredBudget` refuses at startup for a lifecycle that
    binds something which spends and names no ceiling, and this file binds three such verbs — so
    an all-`None` budget here is a refusal at run time rather than a test failure at commit time,
    which is the more expensive place to find out."""
    assert lockstep.budget.declared
    assert lockstep.budget.usd is not None


def test_a_lifecycle_that_declares_no_improvable_attributes_nothing() -> None:
    """The default has to be empty rather than helpful. A framework that shipped a guess about
    which body answers which finding would be making a claim about evidence it never saw."""
    from in_lockstep.lockstep import Lockstep

    bare = Lockstep()
    assert bare.improve == ()
    assert bare.max_open_proposals == 1


def test_this_repositorys_own_lifecycle_module_passes_the_ruff_its_selfcheck_runs() -> None:
    """Issue 196. `make lint` targets `src tests`, so the one file that can rebind any adapter,
    remove any middleware and grant any tool is the one file CI does not lint — and an undefined
    name sat on `fix/propose`'s success path for a release because of it.

    Linted here rather than by widening the Makefile target, so the check travels with the suite
    and runs wherever the tests do. `F` is the rule set that matters: this file is executed, not
    imported for its symbols, so an undefined name is a runtime failure in a workflow rather than
    an import error anyone would see first."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--select", "I,E4,E7,E9,F", str(ROOT / ".lockstep")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# -- GATE-EVIDENCE-1: the promoted corpus is not a path an agent of ours can write --------------


def test_gate_evidence_1_this_repositorys_guard_refuses_the_promoted_corpus(lockstep) -> None:  # noqa: ANN001
    """`evidence/README.md` says no agent here can write there. Asserted, because it once did not.

    The prose made the claim while `evidence/` appeared in neither deny tier, so `check_path`
    returned `None` for it. A `fix` run given a ticket that mentioned the eval corpus could have
    staged `evidence/cases/review/<name>.json` -- a whole composed prompt and a whole diff -- and
    the `propose` job, which holds `contents: write`, would have committed it. That is the decay
    `design/gates.md` opens by describing: a control that reads as though it were in force.

    Read off this repository's own lifecycle rather than a constructed `PathPolicy`, because the
    claim is about what THIS repository denies, and a default nobody bound proves nothing.
    """
    refusal = lockstep.guard.check_path("evidence/cases/review/anything.json")
    assert refusal is not None, "an agent of this repository can write to the promoted corpus"
    assert refusal.tier == 1, f"a grant could lift it: {refusal}"


def test_extending_the_deny_list_did_not_drop_what_it_already_protected(lockstep) -> None:
    """The trap `_matches` documents, checked on the repository that walked into its invitation.

    Adding a path means constructing a new tuple, and the tier-1 basename and suffix rules used to
    be reached by an IDENTITY test against the shipped one -- so extending the list silently
    stopped protecting `conftest.py`, `CODEOWNERS`, `*.pem` and `.env*`. That is fixed in
    `_matches`, and this is the test that would notice if it regressed here, where the extension
    actually happens.
    """
    for path in (".lockstep/lockstep.py", "src/conftest.py", "keys/deploy.pem", ".env.local"):
        assert lockstep.guard.check_path(path) is not None, f"{path} lost its protection"
