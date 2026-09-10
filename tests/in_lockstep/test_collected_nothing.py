"""A suite that collected nothing says so, and never that the staged test passed.

GATE-VERDICT-1. Three outcomes wear one sentence if nobody separates them, and each sends the next
attempt somewhere different:

- the staged test ran and passed        -> rewrite the assertions      (`tdd.not_red`)
- the suite ran but not the staged test -> fix the collection settings (`tdd.test_not_collected`)
- the suite collected nothing at all    -> look at the environment     (`tdd.suite_collected_nothing`)

Run 34498550853 got the first about a suite that had done the third, on tests that were genuinely
red -- 6 failed, 4 passed, when a person ran them. $10.18 to be told the opposite of the truth.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from in_lockstep.adapters.ai.strategy import collected_nothing, nothing_collected_finding
from in_lockstep.core.outcome import Outcome, Severity, Status


def _outcome(report: Any, *, status: Status = Status.SUCCEEDED, decided: bool = False) -> Outcome[Any]:
    return Outcome(status=status, value=report, decided=decided)


def test_a_suite_that_reported_zero_tests_collected_nothing() -> None:
    """The shape the broken path produced: SUCCEEDED, nothing decided, a report of zero."""
    assert collected_nothing(_outcome(SimpleNamespace(total=0))) is True


def test_a_suite_that_ran_tests_did_not_collect_nothing() -> None:
    """The negative control. Without it the predicate could return True always and every test
    above it would still pass."""
    assert collected_nothing(_outcome(SimpleNamespace(total=1581))) is False


def test_a_run_that_produced_no_report_is_not_collected_nothing() -> None:
    """Absent is not zero, in the module whose verdicts this decides. A run with no report may not
    have run at all, which is a different failure with a different remedy -- and `not_a_verdict`
    has already passed the statuses it owns through by the time this is asked."""
    assert collected_nothing(_outcome(None)) is False


def test_the_finding_never_tells_the_model_its_test_passed() -> None:
    """The whole point. A model told its test passed rewrites assertions that were never executed,
    and the assertions were never the problem."""
    finding = nothing_collected_finding("tdd.suite_collected_nothing", "red")

    assert finding.id == "tdd.suite_collected_nothing"
    assert finding.severity is Severity.ERROR and finding.blocking
    message = finding.message.lower()
    assert "collected no tests at all" in message
    assert "do not rewrite the assertions" in message
    for claim in ("did not fail against the current code", "did not make the staged test pass"):
        assert claim not in message, "this says nothing about the test or the implementation"


def test_the_finding_names_the_phase_that_collected_nothing() -> None:
    """Red, green and the reproducer are three different moments and a reader needs to know which."""
    assert "red run" in nothing_collected_finding("tdd.suite_collected_nothing", "red").message
    assert "green run" in nothing_collected_finding("tdd.suite_collected_nothing", "green").message
    assert "reproducer run" in nothing_collected_finding("fix.suite_collected_nothing", "reproducer").message


def test_the_finding_id_is_the_reason_so_a_record_reads_as_one_thing() -> None:
    """The convention the other verdicts here follow: `tdd.not_red` is both. A finding whose id
    differs from the reason beside it reads as two findings to whoever greps the ledger."""
    assert nothing_collected_finding("fix.suite_collected_nothing", "reproducer").id == (
        "fix.suite_collected_nothing"
    )


def test_the_finding_points_at_the_environment_the_collection_settings_and_the_imports() -> None:
    """The three real causes, because "something went wrong" costs a turn to act on. The
    environment is first: it is what actually happened, and it is the one a model cannot see."""
    message = nothing_collected_finding("tdd.suite_collected_nothing", "red").message

    assert "environment" in message
    assert "testpaths" in message
    assert "collection error" in message


def test_the_collection_probe_asks_the_runner_the_red_run_used() -> None:
    """The other half of #435, and the mechanism that produced the wrong label.

    `_uncollected` dispatched `Test` with no runner, so `PytestTest` fell back to the BOUND
    adapter's own sandbox -- `None` for this repository, meaning the host. The red run it was
    explaining had run in a container. The probe then answered truthfully about an environment
    nobody had asked about: the staged tests collect fine on a laptop that has a venv, so it
    reported "collection is not the problem" about a container where the suite had collected
    nothing at all, and the run was labelled `tdd.not_red`.

    A diagnostic that inspects a different thing from the one that failed is the defect it exists
    to find, wearing its own clothes.
    """
    import asyncio

    from in_lockstep.adapters.ai.tdd import _uncollected
    from in_lockstep.core.types import ChangeAuthor, ChangeSet, FileChange

    seen: list[Any] = []

    class _Ctx:
        async def do(self, request: Any) -> Outcome[Any]:
            seen.append(request.runner)
            return _outcome(SimpleNamespace(total=0, passed=0, failed=0))

    staged = ChangeSet(
        changes=(FileChange(path="tests/test_new.py", contents="", author=ChangeAuthor.AGENT),)
    )
    sentinel = object()

    asyncio.run(_uncollected(_Ctx(), "/tree", staged, sentinel))

    assert seen == [sentinel], "the probe must run where the run it is diagnosing ran"


def test_the_probe_reports_the_staged_files_when_only_those_collect_nothing() -> None:
    """The case the probe is actually for, kept working: the suite ran, and the staged files are
    the part of it that collected nothing."""
    import asyncio

    from in_lockstep.adapters.ai.tdd import _uncollected
    from in_lockstep.core.types import ChangeAuthor, ChangeSet, FileChange

    class _Ctx:
        async def do(self, request: Any) -> Outcome[Any]:
            return _outcome(SimpleNamespace(total=0, passed=0, failed=0))

    staged = ChangeSet(
        changes=(
            FileChange(path="tests/test_new.py", contents="", author=ChangeAuthor.AGENT),
            FileChange(path="README.md", contents="", author=ChangeAuthor.AGENT),
        )
    )

    found = asyncio.run(_uncollected(_Ctx(), "/tree", staged, None))

    assert found == ("tests/test_new.py",), "python files only; a README collects nothing by nature"
