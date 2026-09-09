"""GATE-VERDICT-3: a suite that is red elsewhere is not a change failing its own test.

`tdd.not_green` says "the implementation did not make the staged test pass". What the code checked
was whether the WHOLE suite passed, so any failure anywhere produced that sentence — and the model,
and the person reading the ticket, were told something false about the work.

Run 34363672287 was told exactly that. Its own 35 tests passed; four tests in a file it never
touched failed for an environmental reason (fixed in #404). $11.29, 21.5 minutes, and a complete
change discarded with a message about a test that was never red.

The two states are one number apart in a count and opposite in meaning, which is why the verdict
carries the number now.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from in_lockstep.adapters.ai.strategy import elsewhere_only
from in_lockstep.adapters.sandbox import Sandbox
from in_lockstep.core.outcome import Outcome, Status
from in_lockstep.core.types import (
    ChangeSet,
    FileChange,
    TestCase,
    TestReport,
    TestVerdict,
    failures_elsewhere,
)


def _report(*cases: tuple[str, str]) -> TestReport:
    made = tuple(TestCase(id=cid, outcome=outcome) for cid, outcome in cases)
    failed = sum(1 for c in made if c.outcome in ("failed", "error"))
    return TestReport(total=len(made), passed=len(made) - failed, failed=failed, cases=made)


def _changeset(*paths: str) -> ChangeSet:
    return ChangeSet(changes=tuple(FileChange(path=p, contents="x\n") for p in paths))


# -- whose failure is it -----------------------------------------------------------------------------


def test_gate_verdict_3_a_failure_in_a_file_the_change_did_not_touch_is_named_as_such() -> None:
    """GATE-VERDICT-3. The node id carries the file, which is how the two states are told apart."""
    report = _report(
        ("tests/mine/test_a.py::test_one", "passed"),
        ("tests/theirs/test_b.py::test_two", "failed"),
        ("tests/theirs/test_b.py::test_three", "error"),
    )
    assert failures_elsewhere(report, ("tests/mine/test_a.py",)) == (
        "tests/theirs/test_b.py::test_two",
        "tests/theirs/test_b.py::test_three",
    )
    assert failures_elsewhere(report, ("tests/theirs/test_b.py",)) == ()


def test_a_case_that_names_no_file_counts_as_the_changes_own() -> None:
    """An unattributable failure is not evidence that somebody else broke something, and the safe
    direction is the one that keeps the model responsible for it."""
    report = _report(("collection error", "error"))
    assert failures_elsewhere(report, ("src/a.py",)) == ()


def test_gate_verdict_3_a_partial_overlap_is_still_the_models_to_fix() -> None:
    """GATE-VERDICT-3. One of its own tests red means it is fixing whatever else is failing beside
    it; a change is excused only when NONE of the failures are its own."""
    report = _report(
        ("tests/mine/test_a.py::test_one", "failed"),
        ("tests/theirs/test_b.py::test_two", "failed"),
    )
    outcome: Outcome[Any] = Outcome(status=Status.FAILED, value=report, decided=True)
    assert elsewhere_only(outcome, _changeset("tests/mine/test_a.py")) == ()

    only_theirs = _report(("tests/theirs/test_b.py::test_two", "failed"))
    outcome = Outcome(status=Status.FAILED, value=only_theirs, decided=True)
    assert elsewhere_only(outcome, _changeset("tests/mine/test_a.py")) == (
        "tests/theirs/test_b.py::test_two",
    )


def test_a_runner_that_reported_no_cases_attributes_nothing() -> None:
    """Counts without cases cannot say whose failure it was, and guessing "not yours" is how a red
    suite becomes a pull request."""
    outcome: Outcome[Any] = Outcome(status=Status.FAILED, value=TestReport(total=9, failed=2), decided=True)
    assert elsewhere_only(outcome, _changeset("src/a.py")) == ()


# -- what the verdict says ----------------------------------------------------------------------------


def test_gate_verdict_3_the_verdict_counts_the_failures_that_are_not_the_changes() -> None:
    """GATE-VERDICT-3. A count rather than the names: a verdict is counts and a status so it
    serialises on the redacted side of the artifact, and the names travel as findings."""
    report = _report(
        ("tests/mine/test_a.py::ok", "passed"),
        ("tests/theirs/test_b.py::x", "failed"),
    )
    verdict = TestVerdict.of("failed", True, report, changed=("tests/mine/test_a.py",))
    assert verdict.failed == 1 and verdict.elsewhere == 1
    assert verdict.only_elsewhere, "red, and none of it is this change's"

    mine = TestVerdict.of("failed", True, report, changed=("tests/theirs/test_b.py",))
    assert mine.elsewhere == 0 and not mine.only_elsewhere


def test_a_verdict_built_without_the_changed_paths_claims_nothing() -> None:
    """The honest answer for a caller that did not know: `elsewhere` is 0 and `only_elsewhere` is
    False, so nothing downstream reads an absent split as "not this change's fault"."""
    report = _report(("tests/theirs/test_b.py::x", "failed"))
    verdict = TestVerdict.of("failed", True, report)
    assert verdict.elsewhere == 0 and not verdict.only_elsewhere


def test_a_green_verdict_is_never_only_elsewhere() -> None:
    report = _report(("tests/mine/test_a.py::ok", "passed"))
    assert not TestVerdict.of("succeeded", True, report, changed=("tests/mine/test_a.py",)).only_elsewhere


def test_gate_verdict_3_the_split_crosses_the_artifact(tmp_path: Path) -> None:
    """GATE-VERDICT-3. The job that decides whether to escalate holds a write token and no provider
    credential: it cannot re-run the suite, so what the verdict knew has to travel with it."""
    from in_lockstep.platform.artifacts import read_verdict, write_changeset

    artifact = str(tmp_path / "changeset")
    write_changeset(
        artifact,
        _changeset("src/a.py"),
        verdict=TestVerdict(status="failed", decided=True, total=9, passed=8, failed=1, elsewhere=1),
    )
    read_back = read_verdict(artifact)
    assert read_back is not None and read_back.elsewhere == 1 and read_back.only_elsewhere


# -- what a run does about it --------------------------------------------------------------------------


class _Tickets:
    def __init__(self) -> None:
        self.said: list[str] = []
        self.created: list[Any] = []

    async def get(self, key: str) -> Any:
        from in_lockstep.platform.tickets import Ticket

        return Ticket(key=key, title="a title")

    async def comment(self, ticket: Any, body: str) -> None:
        self.said.append(body)

    async def create(self, draft: Any) -> Any:
        from in_lockstep.platform.tickets import Ticket

        self.created.append(draft)
        return Ticket(key="#999", title=draft.title)


class _Scm:
    shared_numbering = True

    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []
        self.ready: list[Any] = []

    async def ticket_of(self, number: int) -> str | None:
        return None

    async def open_change(self, cs: Any, **kwargs: Any) -> Any:
        from in_lockstep.platform.scm.base import ChangeRequest

        self.opened.append(kwargs)
        return ChangeRequest(id="u", url="https://x/1", branch="b", title="t", number=1)

    async def mark_ready(self, change: Any) -> None:
        self.ready.append(change)


class _Ctx:
    def __init__(self, root: Path) -> None:
        self.run_id = "r1"
        self.max_attempts = 3
        self.repo = type("R", (), {"root": str(root)})()


def _propose(tmp_path: Path, verdict: TestVerdict, name: str) -> tuple[_Scm, _Tickets]:
    from in_lockstep.platform.artifacts import write_changeset
    from in_lockstep.workflows.implement import implement_propose

    artifact = str(tmp_path / name)
    write_changeset(artifact, _changeset("src/a.py"), verdict=verdict)
    scm, tickets = _Scm(), _Tickets()
    asyncio.run(
        implement_propose(
            _Ctx(tmp_path),  # type: ignore[arg-type]
            "#1",
            tickets,  # type: ignore[arg-type]
            scm,  # type: ignore[arg-type]
            artifact=artifact,
        )
    )
    return scm, tickets


def test_gate_verdict_3_a_change_the_suite_failed_elsewhere_is_opened_as_a_draft(
    tmp_path: Path,
) -> None:
    """GATE-VERDICT-3, the consequence. Escalating here files a bug report about somebody else's
    failure and spends an attempt on it; discarding the change destroys work that is complete."""
    verdict = TestVerdict(status="failed", decided=True, total=9, passed=8, failed=1, elsewhere=1)
    scm, tickets = _propose(tmp_path, verdict, "elsewhere")

    assert scm.opened, "the change never travelled"
    assert scm.ready == [], "and it is a draft, because the suite is still red"
    assert tickets.created == [], "no ai-generated ticket was filed about somebody else's failure"


def test_gate_verdict_3_a_change_that_failed_its_own_tests_still_escalates(tmp_path: Path) -> None:
    """GATE-VERDICT-3, the other side. The escalation path is what the fixing verb picks up, and a
    change whose own tests are red is exactly what it is for."""
    verdict = TestVerdict(status="failed", decided=True, total=9, passed=8, failed=1, elsewhere=0)
    scm, tickets = _propose(tmp_path, verdict, "mine")

    assert scm.opened == [], "a change that fails its own tests must not open a pull request"
    assert tickets.created, "and the loop's next attempt was filed"


def test_gate_verdict_3_the_workflows_own_verdict_computes_the_split(tmp_path: Path) -> None:
    """GATE-VERDICT-3, at the one call site that knows both halves.

    `verdict_over_staged` is where the report and the change set are both in hand, so it is the
    only place the split can be made — and a control that removed `changed=` there left every
    other test green while the feature quietly disappeared, reporting `elsewhere=0` for everything.
    """
    import subprocess

    from in_lockstep.adapters.worktree import verdict_over_staged
    from in_lockstep.core.types import Test

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=root, check=True)
    (root / "seed.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "i"], cwd=root, check=True
    )

    report = _report(
        ("tests/mine/test_a.py::ok", "passed"),
        ("tests/theirs/test_b.py::x", "failed"),
    )

    class _Tester:
        # A runner that declares a container, because a staged change is not handed to one that
        # would run it on this host (GATE-SANDBOX-2) and the verdict would be `blocked` instead.
        sandbox = Sandbox(image="declared-for-this-test", require_container=True)

        async def invoke(self, ctx: Any, request: Test) -> Outcome[TestReport]:
            return Outcome(status=Status.FAILED, value=report, decided=True)

    class _Container:
        @staticmethod
        def has(verb: Any) -> bool:
            return verb is Test

        @staticmethod
        def resolve(verb: Any) -> Any:
            return _Tester()

    class _Ctx2:
        container = _Container()
        repo = type("R", (), {"root": str(root)})()

        async def do(self, request: Any) -> Any:
            return await _Tester().invoke(self, request)

    verdict = asyncio.run(verdict_over_staged(_Ctx2(), str(root), _changeset("tests/mine/test_a.py")))
    assert verdict is not None
    assert verdict.elsewhere == 1 and verdict.only_elsewhere, (
        "the verdict was built without the paths the change staged, so it can no longer tell "
        "whose failure it was"
    )
