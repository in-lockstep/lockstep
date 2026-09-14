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


class _Local:
    """The `GitLocal` a host adapter carries. Only the two calls the update path reaches for."""

    def __init__(self, commits: tuple[Commit, ...], sha: str, *, readable: bool = True) -> None:
        self.commits = commits
        self.sha = sha
        self.readable = readable

    def fetch_branch(self, branch: str, *, remote: str = "origin") -> str:
        if not self.readable:
            raise RuntimeError(f"git fetch {remote} {branch} failed: couldn't find remote ref")
        return self.sha

    def commits_between(self, base: str, head: str = "HEAD") -> tuple[Commit, ...]:
        return self.commits


class _Scm:
    """An `Scm` that records open_change / update_change calls and can report existing CRs."""

    shared_numbering = True

    def __init__(
        self,
        *,
        existing_changes: tuple[ChangeRequest, ...] = (),
        branch_commits: tuple[Commit, ...] = (),
        branch_sha: str = "f0f0f0f0",
        branch_readable: bool = True,
    ) -> None:
        self.existing_changes = existing_changes
        self.branch_commits = branch_commits
        self.branch_sha = branch_sha
        self.branch_readable = branch_readable
        self.opened: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.drafted: list[ChangeRequest] = []
        self.readied: list[ChangeRequest] = []
        self.asked_about: list[tuple[str, int]] = []

    async def ticket_of(self, number: int) -> str | None:
        self.asked_about.append(("", number))
        return None

    async def changes_for(self, ticket: str) -> tuple[ChangeRequest, ...]:
        return self.existing_changes

    #: Shaped like the adapters that run. `commits_between` and `fetch_branch` are `GitLocal`'s,
    #: reached through `.local`; `GitHubScm` proxies exactly one method (`diff`) and has neither
    #: of its own. The first version of this stub carried them at the top level, which is why
    #: `test_a_branch_with_a_persons_commit_is_refused_before_the_push` passed against a helper
    #: that could not fire on either shipped host.
    @property
    def local(self) -> Any:
        return _Local(self.branch_commits, self.branch_sha, readable=self.branch_readable)

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
        self.readied.append(change)

    async def mark_draft(self, change: ChangeRequest) -> None:
        self.drafted.append(change)

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


def test_the_sha_the_check_read_is_the_sha_the_push_is_leased_on(tmp_path: Path) -> None:
    """GATE-REVIEW-8. The person-commit check and the force-push name ONE state. A control that
    inspected the branch while the write acted on whatever the branch had become would be a
    control about a tree nothing overwrote — which is this repository's most expensive recurring
    defect, and the reason `expect` is a required argument rather than a default."""
    from in_lockstep.workflows.implement import implement_propose

    framework_commit = Commit(sha="aaa", subject="feat: first attempt", trailers={"In-Lockstep-Run": "r1"})
    scm = _Scm(existing_changes=(_EXISTING_CR,), branch_commits=(framework_commit,), branch_sha="deadbee")
    asyncio.run(
        implement_propose(
            _Ctx(tmp_path),  # type: ignore[arg-type]
            "#443",
            _Tickets(),  # type: ignore[arg-type]
            scm,  # type: ignore[arg-type]
            artifact=_staged(tmp_path),
        )
    )
    assert scm.updated
    assert scm.updated[0]["expect"] == "deadbee", (
        "the push must be leased on the sha the person-commit check inspected"
    )


def test_a_branch_that_cannot_be_read_is_refused_rather_than_assumed_clean(tmp_path: Path) -> None:
    """GATE-REVIEW-8. *Could not tell* is not *nobody wrote anything here*, and the two were
    spelled the same: `hasattr` on the wrong object, a revision the checkout never fetched, and a
    `git` call that answers "" on failure all produced an empty commit list. An unreadable branch
    refuses, and does not fall back to opening a second pull request — a control with a fallback
    is a control with an off switch."""
    from in_lockstep.workflows.implement import implement_propose

    scm = _Scm(existing_changes=(_EXISTING_CR,), branch_readable=False)
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
    assert outcome.reason == "implement.branch_unreadable"
    assert not scm.updated and not scm.opened
    assert any("cannot tell" in msg for msg in tickets.said)


def test_another_verbs_change_request_is_not_the_one_updated(tmp_path: Path) -> None:
    """GATE-REVIEW-8. `changes_for` matches the TICKET segment of a run branch and ignores the
    workflow one, so a `/fix` run's open pull request for this ticket is in its answer. Reading
    what a reviewer said on it is right; force-pushing an `implement` changeset over it is not."""
    from in_lockstep.workflows.implement import implement_propose

    a_fix = ChangeRequest(
        id="fix",
        url="https://host/pull/41",
        branch="in-lockstep/fix/443/r0",
        title="fix: something else",
        number=41,
    )
    scm = _Scm(existing_changes=(a_fix,))
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
    assert scm.opened, "a fix's change request is not implement's to write over"
    assert not scm.updated


def test_a_red_second_attempt_puts_the_change_request_back_into_draft(tmp_path: Path) -> None:
    """GATE-REVIEW-8. Only the update path can meet a change request that is already ready: a
    green first attempt marked it so. A red second attempt that only ever marks ready leaves it
    in a review queue while the comment it posts calls it a draft — a false statement in the
    place a person reads it."""
    from in_lockstep.workflows.implement import implement_propose

    framework_commit = Commit(sha="aaa", subject="feat: first attempt", trailers={"In-Lockstep-Run": "r1"})
    scm = _Scm(existing_changes=(_EXISTING_CR,), branch_commits=(framework_commit,))
    tickets = _Tickets()
    # No verdict in the artifact: nothing proved these tests pass, which is exactly `ready=False`.
    asyncio.run(
        implement_propose(
            _Ctx(tmp_path),  # type: ignore[arg-type]
            "#443",
            tickets,  # type: ignore[arg-type]
            scm,  # type: ignore[arg-type]
            artifact=_staged(tmp_path),
        )
    )
    assert scm.updated
    assert scm.drafted, "an unverified update must put the change request back into draft"
    assert not scm.readied
    assert any("a draft" in msg for msg in tickets.said)


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


# ---------------------------------------------------------------------------
# Over the real adapter, over real git.
#
# The four tests above drive `implement_propose` against a stub, which is the right level for
# "which branch did it choose". It is the WRONG level for every property below: building over
# HEAD, leasing the push, and reading who wrote what all happen inside `GitHubScm.update_change`
# and `GitLocal`, and a stub cannot be wrong about them in the way the adapters were. The first
# version of this file asserted the #422 property by comparing two constants — `base` was never
# passed, so `kwargs.get("base", "") != branch` was `"" != "in-lockstep/…"` — and passed against
# an implementation deliberately rewritten to build over the branch tip.
#
# So these build a bare remote, a branch with a prior attempt on it, and a DEPTH-1 checkout,
# which is what the propose job's `actions/checkout` leaves behind. The shallow clone is not
# incidental: it is the environment in which a bare `--force-with-lease` is rejected as
# `stale info` and `git log HEAD..<branch>` cannot resolve its revision.
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    """git, always told where it runs. A call that inherits the working directory is a call that
    passes for whoever wrote it and fails in a materialised worktree."""
    import subprocess

    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    assert done.returncode == 0, f"git {' '.join(args)} in {root}: {done.stderr.strip()}"
    return done.stdout


_BRANCH = "in-lockstep/implement/443/r1"
_IDENT = ("-c", "user.email=t@example.invalid", "-c", "user.name=t")


def _commit(root: Path, path: str, message: str) -> str:
    (root / path).write_text("x\n")
    _git(root, "add", "-A")
    _git(root, *_IDENT, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD").strip()


def _an_attempt_already_on_the_remote(tmp_path: Path) -> tuple[Path, Path, ChangeRequest]:
    """A bare remote holding `main` and one framework attempt on a run branch, plus the depth-1
    checkout the propose job would be running in. Returns (remote, checkout, the change request)."""
    remote, author, checkout = tmp_path / "remote.git", tmp_path / "author", tmp_path / "checkout"
    _git(tmp_path, "init", "--quiet", "--bare", str(remote))
    _git(tmp_path, "clone", "--quiet", f"file://{remote}", str(author))
    # Pinned: `git init` takes whatever `init.defaultBranch` says, which is `main` on a laptop
    # that set it and `master` on the runner, which did not.
    _git(author, "branch", "-M", "main")
    _commit(author, "base.py", "chore: a base")
    _git(author, "push", "--quiet", "-u", "origin", "main")
    _git(author, "checkout", "--quiet", "-b", _BRANCH)
    _commit(author, "stale.py", "feat: the first attempt\n\nIn-Lockstep-Run: r1\nTicket: #443")
    _git(author, "push", "--quiet", "-u", "origin", _BRANCH)
    # Depth 1, single branch: `actions/checkout` with no `fetch-depth`, which is the propose job.
    _git(tmp_path, "clone", "--quiet", "--depth", "1", "--branch", "main", f"file://{remote}", str(checkout))
    change = ChangeRequest(
        id="u", url="https://host/pull/42", branch=_BRANCH, title="feat: the first attempt", number=42
    )
    return remote, checkout, change


def test_the_changeset_is_built_over_head_not_the_branch_tip(tmp_path: Path) -> None:
    """GATE-REVIEW-8. The ordering property in `prepared` is not weakened: `Provision` runs over
    reviewed code, so the tree that is pushed is HEAD plus this changeset and never the branch
    tip a model wrote (#422). Asserted on the REMOTE, against the prior attempt's own file: if
    the update were built on top of the branch, `stale.py` would still be in the tree."""
    from in_lockstep.platform.scm.github import GitHubScm
    from in_lockstep.workflows.implement import _branch_state

    remote, checkout, change = _an_attempt_already_on_the_remote(tmp_path)
    scm = GitHubScm(checkout)
    state = _branch_state(change, scm)
    assert state is not None, "a branch that exists must be readable from a depth-1 checkout"
    expect, people = state
    assert people == [], "only the framework has written to this branch"

    asyncio.run(
        scm.update_change(
            change,
            ChangeSet(changes=(FileChange(path="second.py", contents="y = 2\n"),), summary="s"),
            expect=expect,
            workflow="implement",
            run_id="r2",
        )
    )
    assert "second.py" in _git(remote, "ls-tree", "--name-only", _BRANCH), "the update did not land"
    assert "stale.py" not in _git(remote, "ls-tree", "--name-only", _BRANCH), (
        "the changeset was built over the branch tip, which is what #422 refuses"
    )
    assert _git(remote, "rev-parse", f"{_BRANCH}^").strip() == _git(remote, "rev-parse", "main").strip(), (
        "the new commit's parent is HEAD, not the prior attempt"
    )


def test_a_branch_that_moved_since_it_was_read_refuses_the_push_by_name(tmp_path: Path) -> None:
    """GATE-REVIEW-8. What the branch name's run id used to guarantee, the lease guarantees now.
    `lockstep-implement.yml` groups concurrency on the number the comment was left on, so a round
    asked for on the issue and one asked for on the pull request are different groups and can
    overlap — safe only while every run had its own branch. The second writer finds its lease
    stale and is refused, by name, with nothing overwritten."""
    import pytest

    from in_lockstep.platform.scm.base import TargetRefused
    from in_lockstep.platform.scm.github import GitHubScm
    from in_lockstep.workflows.implement import _branch_state

    remote, checkout, change = _an_attempt_already_on_the_remote(tmp_path)
    scm = GitHubScm(checkout)
    state = _branch_state(change, scm)
    assert state is not None
    expect, _people = state

    # A second run pushes to the same branch between the read and the write.
    other = tmp_path / "other"
    _git(tmp_path, "clone", "--quiet", "--branch", _BRANCH, f"file://{remote}", str(other))
    theirs = _commit(other, "theirs.py", "feat: another run\n\nIn-Lockstep-Run: r3")
    _git(other, "push", "--quiet", "origin", _BRANCH)

    with pytest.raises(TargetRefused) as refusal:
        asyncio.run(
            scm.update_change(
                change,
                ChangeSet(changes=(FileChange(path="second.py", contents="y = 2\n"),), summary="s"),
                expect=expect,
                workflow="implement",
                run_id="r2",
            )
        )
    assert refusal.value.reason == "scm.branch_moved"
    assert _git(remote, "rev-parse", _BRANCH).strip() == theirs, "the other run's commit survived"


def test_a_persons_commit_is_found_through_the_adapter_the_workflow_is_handed(tmp_path: Path) -> None:
    """GATE-REVIEW-8. The refusal's detection, over the real adapter and with no stub anywhere.

    This is the test the first version of this file could not have: it asked
    `hasattr(scm, "commits_between")`, which is False on both `GitHubScm` and `GitLabScm` —
    `commits_between` is `GitLocal`'s and the adapters proxy exactly one method (`diff`). Only a
    stub carrying it at the top level made the refusal reachable, so the control passed while the
    branches of #442 and #450, each of which carries a person's commit, would have been force-
    pushed over in silence."""
    from in_lockstep.platform.scm.github import GitHubScm
    from in_lockstep.workflows.implement import _branch_state

    remote, checkout, change = _an_attempt_already_on_the_remote(tmp_path)
    author = tmp_path / "author"
    by_hand = _commit(author, "human.py", "fix: a person fixed it by hand")
    _git(author, "push", "--quiet", "origin", _BRANCH)

    state = _branch_state(change, GitHubScm(checkout))
    assert state is not None
    expect, people = state
    assert expect == _git(remote, "rev-parse", _BRANCH).strip()
    assert [c.sha for c in people] == [by_hand], (
        "the commit with no `In-Lockstep-Run` trailer is the one a person wrote"
    )
