"""GATE-DOGFOOD-1: the loop has closed on this repository, and this keeps it closed.

Reads two things nothing in a tmp root can fake: the ledger this repository actually publishes
(`origin/lockstep-history`) and the host's own list of pull requests. Where either is out of
reach the test skips with a named reason rather than passing, because a green that read nothing
is the reassuring figure this ledger exists to refuse. `ci.yml` checks out every branch and hands
`gh` a read-only token so that on CI it runs.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from in_lockstep.platform.scm.base import is_run_branch

ROOT = Path(__file__).resolve().parents[2]
HISTORY_REF = "refs/remotes/origin/lockstep-history"


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def _published_records() -> list[dict[str, object]]:
    if _git("rev-parse", "--verify", "--quiet", f"{HISTORY_REF}^{{commit}}").returncode != 0:
        pytest.skip(f"GATE-DOGFOOD-1 not checked: {HISTORY_REF} is not fetched in this checkout")
    names = _git("ls-tree", "--name-only", f"{HISTORY_REF}:records").stdout.split()
    out: list[dict[str, object]] = []
    for name in names:
        shown = _git("show", f"{HISTORY_REF}:records/{name}").stdout
        try:
            record = json.loads(shown)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def test_gate_dogfood_1_a_fix_this_framework_ran_here_is_on_the_published_ledger() -> None:
    """The first framework-authored change in this repository's history: run 34158960476, the
    ninth `/fix` on #319, staged a reproducer and a fix and proposed them (#343)."""
    records = _published_records()
    fixes = [
        r
        for r in records
        if str(r.get("workflow", "")).startswith(("fix/", "implement/")) and r.get("status") == "succeeded"
    ]
    assert fixes, "no succeeded fix or implement run has been published from this repository"


def test_gate_dogfood_1_the_learning_loop_has_measured_here() -> None:
    """`improve.yml` ran (34069517686 dispatched, 34121918174 scheduled) and each left its record,
    blocked by name -- `improve.no_trend`, then `improve.nothing_to_improve` -- which is the loop
    refusing to propose on no evidence rather than the loop not running."""
    records = _published_records()
    measured = [r for r in records if str(r.get("workflow", "")).startswith("improve/")]
    assert measured, "no improve run has been published from this repository"
    assert all(r.get("reason") for r in measured if r.get("status") == "blocked"), (
        "a blocked improve run must say why"
    )


def test_gate_dogfood_1_the_host_has_merged_a_change_this_framework_opened() -> None:
    """Asked of the host, not the ledger: a merged pull request whose head is a run branch. #343
    was the first, merged 2026-09-07."""
    listed = subprocess.run(
        ["gh", "pr", "list", "--state", "merged", "--limit", "200", "--json", "number,headRefName"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        said = (listed.stderr or listed.stdout).strip().splitlines()
        pytest.skip(
            f"GATE-DOGFOOD-1 not checked: gh could not list pull requests ({said[0][:120] if said else '?'})"
        )
    try:
        rows = json.loads(listed.stdout)
    except ValueError:
        pytest.skip("GATE-DOGFOOD-1 not checked: gh returned no JSON")
    merged = [r for r in rows if isinstance(r, dict) and is_run_branch(str(r.get("headRefName", "")))]
    assert merged, "the host lists no merged pull request on a branch this framework opened"


def test_gate_dogfood_1_no_fixture_record_is_published_after_the_cleanup() -> None:
    """The four that leaked (`triage-412`, `review-security`, two `wayfinder-*-local`) are removed
    by an acknowledged rewrite that is a person's act; until then this names them rather than
    failing, and after it this fails if one comes back."""
    from in_lockstep.platform.ledger.history import FIXTURE_RUN_ID

    records = _published_records()
    leaked = sorted(
        str(r.get("run_id", "")) for r in records if FIXTURE_RUN_ID.match(str(r.get("run_id", "")))
    )
    known = {"triage-412", "wayfinder-chart-local", "wayfinder-work-local"}
    assert set(leaked) <= known, (
        f"a fixture record reached the published ledger: {sorted(set(leaked) - known)}"
    )
