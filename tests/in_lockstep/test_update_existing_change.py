"""A second `/implement` updates the change request it already opened, not a new one.

A reviewer objects on a framework-opened pull request. The next `/implement` on the same ticket
should push to the same branch — same PR URL, same review threads — rather than opening a second
pull request and leaving two for a person to close.

Three properties, each tested:

1. When an open change request exists for the ticket, the run updates it rather than opening a new
   one. The pull request's URL, number and review threads survive.

2. A branch carrying any commit without an `In-Lockstep-Run` trailer is refused **before** the
   push, naming the commits it would have overwritten.

3. A ticket with no open change request behaves exactly as today: a new branch and a new pull
   request.

GATE-REVIEW-8: a second attempt on a ticket that already has an open change request force-pushes
to that branch. The changeset is built over HEAD, not the branch tip.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from in_lockstep.core.outcome import Status
from in_lockstep.core.types import ChangeSet, FileChange
from in_lockstep.platform.scm.base import ChangeRequest, Commit

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _Tickets:
    """A ticket source that records what it was told."""

    def __init__(self) -> None:
        self.said: list[str] = []

    async def get(self, key: str) -> Any:
        from in_lockstep.platform.tickets import Ticket

        return Ticket(key=key, title="a title", description="do the thing")

    async def comment(self, ticket: Any, body: str) -> None:
        self.said.append(body)


class _Scm:
    """An `Scm` that records open_change / update_change calls and can report existing CRs."""

    shared_numbering = True

    def __init__(
        self,
        *,
        existing_changes: tuple[ChangeRequest, ...] = (),
        branch_commits: tuple[Commit, ...] = (),
    ) -> None:
        self.existing_changes = existing_changes
        self.branch_commits = branch_commits
        self.opened: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.asked_about: list[tuple[str, int]] = []

    async def ticket_of(self, number: int) -> str | None:
        self.asked_about.append(("", number))
        return None

    async def changes_for(self, ticket: str) -> tuple[ChangeRequest, ...]:
        return self.existing_changes

    def commits_between(self, base: str, head: str = "HEAD") -> tuple[Commit, ...]:
        return self.branch_commits

    async def open_change(self, cs: Any, **kwargs: Any) -> ChangeRequest:
        self.opened.append(kwargs)
        return ChangeRequest(
            id="new-url",
            url="https://host/pull/99",
            branch="in-lockstep/implement/443/r1",
            title="t",
            number=99,
        )

    async def update_change(self, change: ChangeRequest, cs: Any, **kwargs: Any) -> ChangeRequest:
        self.updated.append({"change": change, **kwargs})
        return change

    async def mark_ready(self, change: ChangeRequest) -> None:
        return None

    async def remarks(self, number: int) -> tuple[Any, ...]:
        return ()


class _Ctx:
    def __init__(self, root: Path) -> None:
        self.run_id = "r2"
        self.max_attempts = 3
        self.repo = type("R", (), {"root": str(root)})()


def _staged(tmp_path: Path) -> str:
    from in_lockstep.platform.artifacts import write_changeset

    artifact = str(tmp_path / "changeset")
    write_changeset(
        artifact,
        ChangeSet(changes=(FileChange(path="a.py", contents="x = 1\n"),), summary="s"),
    )
    return artifact


# The existing CR the framework opened on a previous run.
_EXISTING_CR = ChangeRequest(
    id="existing-url",
    url="https://host/pull/42",
    branch="in-lockstep/implement/443/r1",
    title="feat(scope): existing",
    number=42,
    trailers={"In-Lockstep-Run": "r1", "Ticket": "#443"},
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_a_second_attempt_updates_the_existing_change_request(tmp_path: Path) -> None:
    """GATE-REVIEW-8. When an open CR already exists for this ticket, the run pushes to that
    branch rather than opening a new pull request. The URL and number survive."""
    from in_lockstep.workflows.implement import implement_propose

    # All commits on the existing branch are framework-authored.
    framework_commit = Commit(sha="aaa", subject="feat: first attempt", trailers={"In-Lockstep-Run": "r1"})
    scm = _Scm(existing_changes=(_EXISTING_CR,), branch_commits=(framework_commit,))
    outcome = asyncio.run(
        implement_propose(
            _Ctx(tmp_path),  # type: ignore[arg-type]
            "#443",
            _Tickets(),  # type: ignore[arg-type]
            scm,  # type: ignore[arg-type]
            artifact=_staged(tmp_path),
        )
    )
    assert outcome.status is Status.SUCCEEDED
    # The existing CR was updated, NOT a new one opened.
    assert scm.updated, "update_change should have been called"
    assert not scm.opened, "open_change should NOT have been called"
    assert scm.updated[0]["change"].number == 42, "the original CR is the one being updated"


def test_a_branch_with_a_persons_commit_is_refused_before_the_push(tmp_path: Path) -> None:
    """GATE-REVIEW-8. A branch that carries a commit without `In-Lockstep-Run` is a branch a
    person pushed to. Overwriting that silently is the failure that ends trust; the run refuses
    by name, naming the commits it would have destroyed."""
    from in_lockstep.workflows.implement import implement_propose

    person_commit = Commit(sha="bbb111", subject="fix typo by hand")
    framework_commit = Commit(sha="aaa", subject="feat: first attempt", trailers={"In-Lockstep-Run": "r1"})
    scm = _Scm(
        existing_changes=(_EXISTING_CR,),
        branch_commits=(framework_commit, person_commit),
    )
    tickets = _Tickets()
    outcome = asyncio.run(
        implement_propose(
            _Ctx(tmp_path),  # type: ignore[arg-type]
            "#443",
            tickets,  # type: ignore[arg-type]
            scm,  # type: ignore[arg-type]
            artifact=_staged(tmp_path),
        )
    )
    assert outcome.status is Status.FAILED
    assert outcome.reason == "implement.person_commits_on_branch"
    # Neither opened nor updated — refused before anything was written.
    assert not scm.opened
    assert not scm.updated
    # The refusal names the commit it would have overwritten.
    assert any("bbb111" in msg for msg in tickets.said), "the refusal should name the person's commit"
    # The refusal says what to do about it.
    assert any("close" in msg.lower() or "drop" in msg.lower() for msg in tickets.said), (
        "the refusal should say what to do"
    )


def test_no_existing_cr_opens_a_new_one_as_before(tmp_path: Path) -> None:
    """When no open change request exists for this ticket, the flow is unchanged: a new branch
    and a new pull request are created."""
    from in_lockstep.workflows.implement import implement_propose

    scm = _Scm(existing_changes=())
    outcome = asyncio.run(
        implement_propose(
            _Ctx(tmp_path),  # type: ignore[arg-type]
            "#443",
            _Tickets(),  # type: ignore[arg-type]
            scm,  # type: ignore[arg-type]
            artifact=_staged(tmp_path),
        )
    )
    assert outcome.status is Status.SUCCEEDED
    assert scm.opened, "open_change should have been called"
    assert not scm.updated, "update_change should NOT have been called"


def test_the_changeset_is_built_over_head_not_the_branch_tip(tmp_path: Path) -> None:
    """GATE-REVIEW-8. The ordering property in `prepared` is not weakened: provisioning runs over
    reviewed code (HEAD), not over the branch tip a model wrote. Building over the tip is the
    obvious implementation and it is the one that breaks #422. The update path must still build
    the changeset over HEAD."""
    from in_lockstep.workflows.implement import implement_propose

    framework_commit = Commit(sha="aaa", subject="feat: first attempt", trailers={"In-Lockstep-Run": "r1"})
    scm = _Scm(existing_changes=(_EXISTING_CR,), branch_commits=(framework_commit,))
    asyncio.run(
        implement_propose(
            _Ctx(tmp_path),  # type: ignore[arg-type]
            "#443",
            _Tickets(),  # type: ignore[arg-type]
            scm,  # type: ignore[arg-type]
            artifact=_staged(tmp_path),
        )
    )
    # The update_change call should NOT pass base=<branch-tip>; it should pass no base
    # (which means HEAD) or explicitly pass the default branch base.
    assert scm.updated
    update_kwargs = scm.updated[0]
    # The base, if present at all, should be empty (HEAD) — not the branch's own ref.
    base = update_kwargs.get("base", "")
    assert base != _EXISTING_CR.branch, (
        "the changeset must be built over HEAD, not over the existing branch tip"
    )


def test_two_open_crs_uses_the_one_resolved_through(tmp_path: Path) -> None:
    """When a ticket has two open change requests (e.g. two prior runs), the update goes to the
    first one returned by changes_for (newest first), and says which."""
    from in_lockstep.workflows.implement import implement_propose

    older = ChangeRequest(
        id="old",
        url="https://host/pull/40",
        branch="in-lockstep/implement/443/r0",
        title="t",
        number=40,
    )
    newer = ChangeRequest(
        id="new",
        url="https://host/pull/42",
        branch="in-lockstep/implement/443/r1",
        title="t",
        number=42,
    )
    framework_commit = Commit(sha="aaa", subject="feat: attempt", trailers={"In-Lockstep-Run": "r1"})
    scm = _Scm(existing_changes=(newer, older), branch_commits=(framework_commit,))
    outcome = asyncio.run(
        implement_propose(
            _Ctx(tmp_path),  # type: ignore[arg-type]
            "#443",
            _Tickets(),  # type: ignore[arg-type]
            scm,  # type: ignore[arg-type]
            artifact=_staged(tmp_path),
        )
    )
    assert outcome.status is Status.SUCCEEDED
    assert scm.updated
    assert scm.updated[0]["change"].number == 42, "the newest CR is the one updated"
