"""What the required check actually reads the diff for. Discharges `GATE-REVIEW-5`.

`review` is a required status check on `main`, so every pull request into this repository is
gated on a model reading the change. It read it through one lens of four. `intent`, `performance`
and `tests` shipped, were composed, sat in the characterization corpus — and had never run on a
real pull request in the repository whose whole argument is that it runs on itself. O10 calls that
asking adopters to trust us on our word; O9 says the four shipped lenses are examples rather than
the set, which is a claim about all four and evidence about one.

It also bounded O5. This check runs with `--record` and harvests, so it is where most of the eval
corpus comes from, and a single-lens check makes a single-lens corpus — leaving the improvement
loop of #163 evidence about a quarter of what the framework ships.

The comparison below is against the lenses the BOUND adapter declares rather than a list of four
written here. A gate that hardcoded the shipped set would go stale the moment somebody added a
fifth, which is the failure it exists to prevent one layer down.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from in_lockstep.cli import _review_lenses
from in_lockstep.core.workflow import restore, snapshot
from in_lockstep.loader import load

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "lockstep.yml"

# `--aspect security --aspect intent …`: one invocation of `review`, which loops over the lenses
# itself and exits with the worst status. The loop used to be shell in the workflow (#314).
ASPECT_FLAG = re.compile(r"--aspect\s+([\w-]+)")


@pytest.fixture
def lenses():
    """The lenses a review run in THIS repository would actually have.

    Loaded through the module rather than read off `prompts.review.LENSES`, because the question
    the gate asks is what the bound adapter declares. This repository binds no `Review` of its own
    today and the two answers agree — but a gate that only holds while they agree is a gate about
    the shipped default, not about the check.
    """
    state = snapshot()
    module, _ref = load(str(ROOT))
    try:
        known = _review_lenses(module.lockstep)
        assert known is not None, "this repository's Review adapter must declare its lenses"
        yield set(known)
    finally:
        restore(state)


def _step() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return next(s for s in workflow["jobs"]["review"]["steps"] if s.get("name") == "Review")


def _aspects_run() -> list[str]:
    found = ASPECT_FLAG.findall(_step()["run"])
    assert found, f"no --aspect in the review step:\n{_step()['run']}"
    return found


def test_the_lens_set_is_not_empty(lenses):
    """A positive control. Both sides of the comparison below are parsed, and two empty sets agree
    with each other perfectly — the vacuity every ratchet in this repository has had to be given."""
    assert len(lenses) >= 4
    assert len(_aspects_run()) >= 4


def test_the_required_check_exercises_every_lens_that_is_bound(lenses):
    """`GATE-REVIEW-5`. The direction that matters: adding a lens must turn this red.

    A lens the framework declares and the enforced check never runs is a lens shipped on our word.
    The message names the missing ones rather than the counts, because the fix is to add them to
    the loop — or to decide out loud that they are not worth the money, which is a decision this
    file should make somebody write down.
    """
    unexercised = lenses - set(_aspects_run())
    assert not unexercised, (
        f"the required review check never runs {sorted(unexercised)}. Add them to the aspect loop "
        f"in {WORKFLOW.relative_to(ROOT)}, or say in that file why they are not worth the spend."
    )


def test_the_check_does_not_run_a_lens_that_is_not_bound(lenses):
    """The other direction, and not symmetric with it. A name in the loop that no adapter declares
    is a run that refuses — `GATE-REVIEW-3` makes the refusal loud — but it refuses after the job
    has installed the provider extra and minted a credential, and it fails a required check for a
    typo. Cheaper to catch here."""
    unknown = set(_aspects_run()) - lenses
    assert not unknown, f"the review loop names {sorted(unknown)}, which no bound lens declares"


def test_every_lens_runs_even_when_one_of_them_fails():
    """Failing fast would let a transient refusal in the first lens cost the recordings of the
    three behind it, and this check is where most of the eval corpus comes from. The loop and
    the worst-status rule are `review`'s own (GATE-CI-4, `test_cli.py`), so what this file has to
    hold is that the step hands every lens to ONE invocation rather than running four steps,
    which would stop at the first red one."""
    run = " ".join(_step()["run"].split())
    assert run.count("in-lockstep review") == 1, "several invocations stop at the first failure"
    assert "||" not in run and "for " not in run and "exit " not in run, "the loop went back into shell"


def test_the_lenses_share_one_tape_so_the_harvest_sees_them_all():
    """A cassette is keyed on the whole composed prompt, so four lenses accumulate into one file
    rather than overwrite each other — and the single harvest step downstream covers the lot. Four
    tapes would need four harvests, and the one that got forgotten would be silent."""
    run = _step()["run"]
    assert run.count("--cassette") == 1
    assert "--record" in run
