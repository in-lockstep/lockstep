"""A run that failed still answers on the ticket.

`implement/propose` comments on every outcome it sees — a change opened, no change staged, tests
failed. It only ever saw the outcomes that reached it. A strategy refusing in its first phase does
not: the work job exits non-zero, `needs:` skips propose, and the person who typed `/implement` is
left watching a thread that never replies.

That happened for real on #139. A test-first run refused with `tdd.not_red`, $21 was spent, and the
issue got nothing — no comment, no follow-up, no sign the run had even happened. The alternative to
an answer is not "no answer"; it is somebody assuming it worked, because the last thing the tool
said was that it had started.

These run against this repository's own `.lockstep/lockstep.py` rather than the scaffold string,
because that file is what the failing run actually executed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from in_lockstep.core.workflow import get, restore, snapshot
from in_lockstep.loader import load, lockstep_from

ROOT = Path(__file__).resolve().parents[2]


class _Tracker:
    """A tracker that records what was said to it, and nothing else."""

    def __init__(self) -> None:
        self.said: list[str] = []

    async def get(self, key: str) -> Any:
        from in_lockstep.platform.tickets import Ticket

        return Ticket(key=key, title="a ticket")

    async def comment(self, ticket: Any, body: str) -> None:
        self.said.append(body)


class _Host:
    """Enough `Scm` for `ticket_for` to leave the key alone."""

    shared_numbering = False


class _OpeningHost(_Host):
    """`_Host` plus the two calls `open_reviewable` makes, so a propose workflow runs to the end."""

    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []
        self.ready: list[Any] = []

    async def open_change(self, changeset: Any, **kwargs: Any) -> Any:
        from in_lockstep.platform.scm.base import ChangeRequest

        self.opened.append(kwargs)
        return ChangeRequest(
            id="1", url="https://example.test/pull/1", branch="in-lockstep/fix/139/test", title="t", number=1
        )

    async def mark_ready(self, change: Any) -> None:
        self.ready.append(change)


def _ledger_with(tmp_path: Path, *records: dict[str, Any]) -> Any:
    """A file-backed store holding exactly these records."""
    from in_lockstep.platform.ledger.store import InRepoLedger

    root = tmp_path / "ledger"
    root.mkdir()
    for index, record in enumerate(records):
        record.setdefault("epoch", "in-process")
        (root / f"r{index}.json").write_text(json.dumps(record))
    return InRepoLedger(root=root)


@pytest.fixture
def workflows():
    """This repository's own lifecycle, loaded and then unregistered again."""
    state = snapshot()
    module, _ref = load(str(ROOT))
    yield lockstep_from(module)
    restore(state)


def _run(entry: Any, **kwargs: Any) -> Any:
    class _Ctx:
        run_id = "test"

    return asyncio.run(entry.fn(_Ctx(), **kwargs))


@pytest.mark.parametrize("verb", ["implement", "fix"])
def test_a_failed_run_says_so_on_the_ticket(verb: str, workflows, tmp_path, monkeypatch) -> None:
    """The whole point. A reason a person can act on, not silence."""
    entry = get(f"{verb}/report")
    assert entry is not None, f"{verb}/report is not registered"

    ledger = _ledger_with(
        tmp_path,
        {
            "run_id": f"{verb}-from-ticket-1",
            "kind": verb,
            "args": {"ticket": "#139"},
            "status": "failed",
            "reason": "tdd.not_red",
            "cost_usd": 21.6135,
            "ts": "2026-09-01T19:54:34+00:00",
            "findings": {
                "count": 1,
                "items": [{"id": "tdd.not_red", "message": "the staged test did not fail"}],
            },
        },
    )
    monkeypatch.setattr("in_lockstep.platform.ledger.store_for", lambda *a, **k: ledger)

    tracker = _Tracker()
    _run(entry, ticket="#139", tickets=tracker, scm=_Host())

    (said,) = tracker.said
    assert "tdd.not_red" in said, "the reason is the one thing the person needs"
    assert "$21.61" in said, "what it cost is the other"
    assert "no pull request was opened" in said


@pytest.mark.parametrize("verb", ["implement", "fix"])
def test_it_still_answers_when_the_run_recorded_nothing(verb: str, workflows, tmp_path, monkeypatch) -> None:
    """A run that died before writing a record is the case most likely to go quiet, so it is the
    case worth checking. Saying "the job log is the only account of it" beats saying nothing."""
    monkeypatch.setattr("in_lockstep.platform.ledger.store_for", lambda *a, **k: _ledger_with(tmp_path))

    tracker = _Tracker()
    _run(get(f"{verb}/report"), ticket="#139", tickets=tracker, scm=_Host())

    (said,) = tracker.said
    assert "failed before it recorded anything" in said


@pytest.mark.parametrize("verb", ["implement", "fix"])
def test_a_successful_run_is_not_reported_as_a_failure(verb: str, workflows, tmp_path, monkeypatch) -> None:
    """The report job runs on `failure()`, but the record it reads is found by matching the ticket
    — so a ticket whose only runs succeeded must not have one of them described as the failure."""
    ledger = _ledger_with(
        tmp_path,
        {"run_id": "ok", "args": {"ticket": "#139"}, "status": "succeeded", "reason": None},
    )
    monkeypatch.setattr("in_lockstep.platform.ledger.store_for", lambda *a, **k: ledger)

    tracker = _Tracker()
    _run(get(f"{verb}/report"), ticket="#139", tickets=tracker, scm=_Host())

    (said,) = tracker.said
    assert "failed before it recorded anything" in said, "a succeeded run is not a failure to report"


@pytest.mark.parametrize("verb", ["implement", "fix"])
def test_another_tickets_failure_is_not_borrowed(verb: str, workflows, tmp_path, monkeypatch) -> None:
    """Records are matched on the ticket they carry. Reporting #7's failure onto #139 would be a
    confident, specific, wrong answer — worse than the silence this replaced."""
    ledger = _ledger_with(
        tmp_path,
        {"run_id": "other", "args": {"ticket": "#7"}, "status": "failed", "reason": "budget"},
    )
    monkeypatch.setattr("in_lockstep.platform.ledger.store_for", lambda *a, **k: ledger)

    tracker = _Tracker()
    _run(get(f"{verb}/report"), ticket="#139", tickets=tracker, scm=_Host())

    (said,) = tracker.said
    assert "budget" not in said
    assert "failed before it recorded anything" in said


@pytest.mark.parametrize("verb", ["implement", "fix"])
def test_reporting_a_failure_is_not_itself_a_failure(verb: str, workflows, tmp_path, monkeypatch) -> None:
    """A second red mark on a run whose failure is already recorded would hide the one thing this
    job is for: whether the answer actually reached the ticket."""
    from in_lockstep.core.outcome import Status

    monkeypatch.setattr("in_lockstep.platform.ledger.store_for", lambda *a, **k: _ledger_with(tmp_path))
    outcome = _run(get(f"{verb}/report"), ticket="#139", tickets=_Tracker(), scm=_Host())
    assert outcome.status is Status.SUCCEEDED


# -- a failed run leaves its work behind ---------------------------------------------------------


def test_the_evidence_path_is_not_the_one_propose_reads(workflows) -> None:
    """`tdd.not_green` returns the change deliberately — `test_implement_tdd.py` asserts it, in
    those words: "the change is still carried so a person can see what it tried".

    The workflow threw it away. Run 33582850420 reached that state on #150 — 13 failing tests of
    1644, $13.84 spent, a diagnosable near-miss — and its artifact held a history bundle and
    nothing else, because staging was guarded on SUCCEEDED. The strategy handed the work over and
    the workflow dropped it.

    A failed run now writes to `ATTEMPT`, which is a DIFFERENT path from the two `propose` reads.
    That is what makes "a red change never becomes a pull request" a fact about the filesystem
    rather than a condition somebody can relax later.
    """
    module, _ref = load(str(ROOT))
    assert module.ATTEMPT not in (module.CHANGESET, module.FIX_CHANGESET)


def test_reading_the_proposable_path_does_not_find_an_attempt(workflows, tmp_path, monkeypatch) -> None:
    """The separation, exercised rather than asserted about the source. A change written as
    evidence must be invisible to the reader `propose` uses, or the separate constant is decoration.
    """
    from in_lockstep.core.types import ChangeSet, FileChange
    from in_lockstep.platform.artifacts import MalformedArtifact, read_changeset, write_changeset

    module, _ref = load(str(ROOT))
    monkeypatch.chdir(tmp_path)
    write_changeset(module.ATTEMPT, ChangeSet(changes=(FileChange(path="a.py", contents="x"),)))

    with pytest.raises((MalformedArtifact, FileNotFoundError, OSError)):
        read_changeset(module.CHANGESET)


def test_the_evidence_path_is_uploaded_by_both_workflows(workflows) -> None:
    """Written and then not collected would be the same loss with more steps."""
    module, _ref = load(str(ROOT))
    for name in ("implement.yml", "fix.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text()
        assert f"{module.ATTEMPT}/" in text, f"{name} does not upload the evidence path"


@pytest.mark.parametrize("verb", ["implement", "fix"])
def test_the_propose_workflow_says_on_the_ticket_that_it_opened_the_change(
    verb: str, workflows, tmp_path
) -> None:
    """Issue 196. The success path — the one that runs whenever the work actually worked.

    `fix/propose` opened the change request and then died on an undefined name before it could say
    so, which recorded a succeeding run as errored and left the person who typed `/fix` watching a
    thread that never replied. Exactly the silence `*/report` exists to prevent, on the branch
    nobody thought needed it, because the failure was in the half that succeeds.

    Parametrised over both verbs: the two propose workflows are near-identical and only one of them
    was wrong, which is how it survived — a reader comparing them saw two paragraphs of prose and a
    name that looked plausible in both."""
    from in_lockstep.core.types import ChangeAuthor, ChangeSet, FileChange, TestVerdict
    from in_lockstep.platform.artifacts import write_changeset

    artifact = tmp_path / "changeset.json"
    write_changeset(
        artifact,
        ChangeSet(
            changes=(FileChange(path="src/thing.py", contents="ok\n", author=ChangeAuthor.AGENT),),
            summary=f"{verb} the thing",
            ticket="#139",
        ),
        verdict=TestVerdict(status="succeeded", decided=True, total=3, passed=3),
    )

    tracker, host = _Tracker(), _OpeningHost()
    outcome = _run(get(f"{verb}/propose"), ticket="#139", tickets=tracker, scm=host, artifact=str(artifact))

    assert outcome.status.value == "succeeded", outcome.reason
    (said,) = tracker.said
    assert "https://example.test/pull/1" in said, said
    assert "ready for review" in said, said
    assert host.ready, "a green verdict opens ready, not as a draft"
