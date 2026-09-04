"""Phase-4 gates: SCM, tickets, ledger, CI detection."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from in_lockstep.core.types import ChangeAuthor, ChangeSet, FileChange
from in_lockstep.lockstep import Lockstep
from in_lockstep.platform.ci import CiEnvironment, detect
from in_lockstep.platform.ledger import (
    InRepoLedger,
    LedgerError,
    LedgerScope,
    Unsupported,
    compare,
    read_ledger,
    summarize,
)
from in_lockstep.platform.scm import DirectPushRefused, GitHubScm, GitLocal, branch_for
from in_lockstep.platform.scm.base import GuardRefused
from in_lockstep.platform.tickets import (
    GitHubIssues,
    TicketDraft,
    TicketSource,
    TicketState,
    TicketType,
    criteria_from,
)
from in_lockstep.platform.tickets.base import Ticket

ROOT = Path(__file__).resolve().parents[2]


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    root.mkdir()

    def run(*args: str) -> None:
        subprocess.run(args, cwd=root, capture_output=True, check=True)

    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.test")
    run("git", "config", "user.name", "t")
    (root / "README.md").write_text("hello\n")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "initial")
    return root


# -- write discipline -----------------------------------------------------------------


def test_writes_go_to_a_run_scoped_branch(tmp_path: Path) -> None:
    """Which also serialises concurrent runs — the reason there is no lock service anywhere."""
    assert branch_for("fix-ci", "abc123").startswith("in-lockstep/")
    assert "abc123" in branch_for("fix-ci", "abc123")


def test_the_ticket_is_a_branch_segment_of_its_own() -> None:
    """`in-lockstep/<workflow>/<ticket>/<run-id>`: scannable in a branch list and globbable per
    ticket, with the run id still carrying uniqueness — the ticket joins the name, never replaces
    the collision guarantee."""
    assert branch_for("implement", "run-1", ticket="#59") == "in-lockstep/implement/59/run-1"
    assert branch_for("fix", "run-2", ticket="PROJ-123") == "in-lockstep/fix/PROJ-123/run-2"
    # No ticket, no segment — an `apply` of a local changeset keeps the old shape.
    assert branch_for("change", "local") == "in-lockstep/change/local"
    # Sanitized: a leading '#' is shell noise, and ref-hostile characters become dashes.
    assert branch_for("implement", "r", ticket="a b?c") == "in-lockstep/implement/a-b-c/r"


def test_pushing_outside_the_namespace_is_refused(tmp_path: Path) -> None:
    """Refused by the framework, not merely by a token scope — the token is ambient."""
    scm = GitLocal(_repo(tmp_path))
    scm.assert_run_scoped("in-lockstep/review/1")
    for protected in ("main", "master", "release/1.0", "feature/x"):
        with pytest.raises(DirectPushRefused, match="refusing to write"):
            scm.assert_run_scoped(protected)


def test_open_change_lands_on_its_own_branch_with_trailers(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    scm = GitLocal(root)
    cs = ChangeSet(changes=(FileChange(path="src/new.py", contents="x = 1\n"),), summary="add a thing")
    cr = asyncio.run(
        scm.open_change(cs, title="add a thing", workflow="implement", run_id="r1", ticket="P-1")
    )
    assert cr.branch == "in-lockstep/implement/P-1/r1", "verb, then ticket, then the run id"
    assert (root / "src" / "new.py").read_text() == "x = 1\n"
    log = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=root, capture_output=True, text=True).stdout
    # Trailers are the most portable traceability layer: greppable forever.
    assert "Ticket: P-1" in log
    assert "In-Lockstep-Run: r1" in log


def test_open_change_can_target_a_base_branch(tmp_path: Path) -> None:
    """Every change request used to grow from HEAD and target the default branch — a shape no
    backport can accept. `base` was committed to the protocol before third parties implement,
    because retrofitting a parameter onto a Protocol others implement is a breaking change."""
    root = _repo(tmp_path)
    scm = GitLocal(root)
    release = scm.head()
    subprocess.run(["git", "branch", "release-1.0"], cwd=root, capture_output=True, check=True)
    (root / "later.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "later"], cwd=root, capture_output=True, check=True)

    cs = ChangeSet(changes=(FileChange(path="fix.py", contents="y = 2\n"),))
    asyncio.run(scm.open_change(cs, title="backport", workflow="backport", run_id="r9", base="release-1.0"))
    parent = scm.git("rev-parse", "HEAD^").strip()
    assert parent == release, "the branch must grow from base, not from HEAD"
    assert not (root / "later.txt").exists(), "work landed after the branch point must not be present"


def test_open_change_branches_from_a_remote_only_base(tmp_path: Path) -> None:
    """The CI shape: the release line exists only as origin/<base> (a detached-HEAD checkout with
    no local branches). `git checkout -b b release-1.0` fails there while origin/release-1.0
    works — so the git start-point and the gh --base value cannot be the same string."""
    (tmp_path / "origin").mkdir()
    origin = _repo(tmp_path / "origin")
    subprocess.run(["git", "branch", "release-1.0"], cwd=origin, capture_output=True, check=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.test"], cwd=clone, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=clone, capture_output=True, check=True)
    # A clone has origin/release-1.0 but no local release-1.0 branch — the CI state.
    scm = GitLocal(clone)
    assert scm.start_point("release-1.0") == "origin/release-1.0"
    cs = ChangeSet(changes=(FileChange(path="fix.py", contents="y = 2\n"),))
    cr = asyncio.run(
        scm.open_change(cs, title="backport", workflow="backport", run_id="r9", base="release-1.0")
    )
    assert cr.branch == "in-lockstep/backport/r9", "no ticket, no segment"
    assert (clone / "fix.py").read_text() == "y = 2\n"


def test_commit_trailers_can_be_read_back(tmp_path: Path) -> None:
    """The framework has written In-Lockstep-Run and Ticket trailers from the start; until
    `commits_between` existed, nothing could get them back without shelling out by hand."""
    root = _repo(tmp_path)
    scm = GitLocal(root)
    base = scm.head()
    (root / "a.txt").write_text("a\n")
    scm.commit("did a thing", trailers={"In-Lockstep-Run": "r42", "Ticket": "#7"})

    commits = scm.commits_between(base)
    assert [c.subject for c in commits] == ["did a thing"]
    assert commits[0].trailers == {"In-Lockstep-Run": "r42", "Ticket": "#7"}


def test_cherry_pick_records_where_the_commit_came_from(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    scm = GitLocal(root)
    original = scm.current_branch()
    base = scm.head()
    (root / "fix.txt").write_text("fix\n")
    fix = scm.commit("a fix", trailers={"Ticket": "#7"})

    scm.git("checkout", "-b", "release", base, check=True)
    picked = scm.cherry_pick(fix)
    assert picked != fix
    log = scm.git("log", "-1", "--format=%B")
    assert "cherry picked from commit" in log, "-x is what makes a backport traceable to its source"
    assert "Ticket: #7" in log, "trailers must survive the pick"
    assert scm.merge_base(original, "HEAD") == base


def test_conventional_subject_prefixes_by_workflow_and_keeps_a_declared_type() -> None:
    from in_lockstep.platform.scm.base import conventional_subject, is_conventional

    assert is_conventional("fix: a thing")
    assert is_conventional("feat(scm): a thing")
    assert is_conventional("refactor!: a thing")
    assert not is_conventional("just a summary")

    # A bare summary gets the workflow's type; a fix workflow maps to `fix`, implement to `feat`.
    assert conventional_subject("add a greeting", workflow="implement") == "feat: add a greeting"
    assert conventional_subject("stop the crash", workflow="fix") == "fix: stop the crash"
    assert conventional_subject("cherry-pick it", workflow="backport") == "fix: cherry-pick it"
    # An unknown workflow is a chore, not a guessed feature.
    assert conventional_subject("something", workflow="") == "chore: something"
    # A summary that already declares a type is taken at its word, not double-prefixed.
    assert conventional_subject("fix: already done", workflow="implement") == "fix: already done"


def test_open_change_commit_subject_is_conventional(tmp_path: Path) -> None:
    """A commit a workflow creates must be a Conventional Commit, whatever prose the model wrote."""
    root = _repo(tmp_path)
    scm = GitLocal(root)
    cs = ChangeSet(changes=(FileChange(path="g.py", contents="x = 1\n"),), summary="add a greeting")
    asyncio.run(scm.open_change(cs, title="add a greeting", workflow="implement", run_id="r1"))
    subject = subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    assert subject == "feat: add a greeting"


def test_the_shipped_adapters_pass_their_own_conformance_kit(tmp_path: Path) -> None:
    """The kit third parties are told to run must hold what we ship, or it is advice we do not
    take ourselves."""
    from in_lockstep.platform.conformance import assert_scm, assert_ticket_source
    from in_lockstep.platform.scm import GitHubScm
    from in_lockstep.platform.tickets import GitHubIssues

    root = _repo(tmp_path)
    assert_ticket_source(GitHubIssues())
    assert_scm(GitHubScm(root))
    assert_scm(GitLocal(root))


def _recording(calls: list, stdout: str = ""):
    def fake(*args: str):
        calls.append(args)
        return (0, stdout, "")

    return fake


def test_github_create_reads_the_ticket_back_from_the_url() -> None:
    """What returns is what the tracker holds, not a reconstruction of the draft."""
    issues = GitHubIssues()
    calls: list = []
    issues._gh_raw = _recording(calls, "https://github.com/o/r/issues/12\n")  # type: ignore[method-assign]
    issues._gh_json = lambda *args: {  # type: ignore[method-assign]
        "number": 12,
        "title": "t",
        "body": "",
        "state": "OPEN",
        "labels": [],
        "assignees": [],
        "comments": [],
        "url": "https://github.com/o/r/issues/12",
    }
    ticket = asyncio.run(issues.create(TicketDraft(title="t", labels=("bug",))))
    assert ticket.key == "#12"
    assert "--label" in calls[0] and "bug" in calls[0]


def test_github_search_maps_rows_to_tickets() -> None:
    issues = GitHubIssues()
    issues._gh_json = lambda *args: [  # type: ignore[method-assign]
        {"number": 3, "title": "a crash", "state": "OPEN", "labels": [{"name": "bug"}], "url": "u"}
    ]
    found = asyncio.run(issues.search("crash", limit=5))
    assert [t.key for t in found] == ["#3"]
    assert found[0].type is TicketType.BUG
    assert found[0].state is TicketState.OPEN


def test_github_add_labels_batches_one_edit_and_skips_an_empty_call() -> None:
    issues = GitHubIssues()
    calls: list = []
    issues._gh_raw = _recording(calls)  # type: ignore[method-assign]
    asyncio.run(issues.add_labels(Ticket(key="#4", title="t"), "triaged", "p2"))
    assert calls[-1] == ("issue", "edit", "4", "--add-label", "triaged", "--add-label", "p2")
    asyncio.run(issues.add_labels(Ticket(key="#4", title="t")))
    assert len(calls) == 1, "labeling nothing must not shell out"


def test_github_transition_maps_coarse_and_refuses_what_it_cannot_mean() -> None:
    """GitHub issues have two states; IN_PROGRESS is a refusal, not a silent close."""
    from in_lockstep.core.ports import Unsupported as PortsUnsupported

    issues = GitHubIssues()
    calls: list = []
    issues._gh_raw = _recording(calls)  # type: ignore[method-assign]
    asyncio.run(issues.transition(Ticket(key="#4", title="t"), TicketState.DONE))
    assert calls[-1] == ("issue", "close", "4")
    asyncio.run(issues.transition(Ticket(key="#4", title="t"), TicketState.OPEN))
    assert calls[-1] == ("issue", "reopen", "4")
    with pytest.raises(PortsUnsupported, match="only open and closed"):
        asyncio.run(issues.transition(Ticket(key="#4", title="t"), TicketState.IN_PROGRESS))


def test_github_transition_refuses_a_raw_state_rather_than_ignoring_it() -> None:
    """A caller naming a tracker-specific state must not have it silently dropped and the issue
    closed instead — `raw` means something on a Jira adapter, nothing on GitHub."""
    from in_lockstep.core.ports import Unsupported as PortsUnsupported

    issues = GitHubIssues()
    issues._gh_raw = _recording([])  # type: ignore[method-assign]
    with pytest.raises(PortsUnsupported, match="no state named"):
        asyncio.run(issues.transition(Ticket(key="#4", title="t"), TicketState.DONE, raw="In Review"))


def test_github_open_change_can_open_a_draft(tmp_path: Path) -> None:
    """A draft PR is how an AI change lands without entering a human's review queue until its tests
    pass. `--draft` has to reach `gh pr create`, and the request has to report it."""
    from in_lockstep.platform.scm import GitHubScm

    root = _repo(tmp_path)
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=root, check=True)

    scm = GitHubScm(root)
    calls: list = []
    scm._gh = _recording(calls, "https://github.com/o/r/pull/5\n")  # type: ignore[method-assign]
    cs = ChangeSet(changes=(FileChange(path="a.py", contents="x = 1\n"),), summary="s")

    change = asyncio.run(scm.open_change(cs, title="t", workflow="implement", run_id="r1", draft=True))
    create = next(c for c in calls if c[:2] == ("pr", "create"))
    assert "--draft" in create
    assert change.draft is True

    # Without draft=, no --draft flag and the request says so. A distinct change, so the commit is
    # not empty on the fresh branch.
    calls.clear()
    cs2 = ChangeSet(changes=(FileChange(path="b.py", contents="y = 2\n"),), summary="s2")
    change2 = asyncio.run(scm.open_change(cs2, title="t2", workflow="implement", run_id="r2"))
    create2 = next(c for c in calls if c[:2] == ("pr", "create"))
    assert "--draft" not in create2
    assert change2.draft is False


def test_github_mark_ready_takes_the_pr_out_of_draft() -> None:
    from in_lockstep.platform.scm import GitHubScm
    from in_lockstep.platform.scm.base import ChangeRequest

    scm = GitHubScm(".")
    calls: list = []
    scm._gh = _recording(calls)  # type: ignore[method-assign]

    asyncio.run(scm.mark_ready(ChangeRequest(id="u", url="u", branch="b", title="t", number=5)))
    assert ("pr", "ready", "5") in calls

    # A request with no number (a local branch, or an open_change that returned only a branch) is
    # left alone rather than guessed at.
    calls.clear()
    asyncio.run(scm.mark_ready(ChangeRequest(id="b", url="", branch="b", title="t")))
    assert calls == []


def test_open_change_refuses_a_base_that_looks_like_an_option(tmp_path: Path) -> None:
    """A backport's base can come from a ticket target; a `-`-leading ref that git/gh would read as
    a flag is refused rather than passed through."""
    scm = GitLocal(_repo(tmp_path))
    cs = ChangeSet(changes=(FileChange(path="a.py", contents="x = 1\n"),))
    with pytest.raises(RuntimeError, match="looks like an option"):
        asyncio.run(scm.open_change(cs, title="t", workflow="backport", run_id="r1", base="--upload-pack=x"))


class _RecordingScm:
    """Records how a change was opened, so open_reviewable's draft/ready decision is testable."""

    def __init__(self) -> None:
        self.opened_draft: bool | None = None
        self.marked_ready = False

    async def open_change(self, cs: object, *, draft: bool = False, **kwargs: object):  # noqa: ANN202
        from in_lockstep.platform.scm.base import ChangeRequest

        self.opened_draft = draft
        return ChangeRequest(id="u", url="u", branch="b", title="t", number=1, draft=draft)

    async def mark_ready(self, change: object) -> None:
        self.marked_ready = True


def test_open_reviewable_marks_a_green_change_ready() -> None:
    from in_lockstep.platform.propose import open_reviewable

    scm = _RecordingScm()
    cs = ChangeSet(changes=(FileChange(path="a.py", contents="x\n"),), summary="s")
    asyncio.run(open_reviewable(scm, cs, ready=True, title="t", workflow="implement", run_id="r"))
    assert scm.opened_draft is True, "always opened draft first"
    assert scm.marked_ready is True, "a green change is marked ready"


def test_open_reviewable_leaves_an_unverified_change_a_draft() -> None:
    from in_lockstep.platform.propose import open_reviewable

    scm = _RecordingScm()
    cs = ChangeSet(changes=(FileChange(path="a.py", contents="x\n"),), summary="s")
    asyncio.run(open_reviewable(scm, cs, ready=False, title="t", workflow="implement", run_id="r"))
    assert scm.opened_draft is True
    assert scm.marked_ready is False, "an unverified change stays a draft"


def test_attempt_of_reads_the_highest_attempt_label() -> None:
    from in_lockstep.platform.propose import attempt_of

    assert attempt_of(()) == 0, "a human-filed issue has no attempts"
    assert attempt_of(("bug", "ai-generated")) == 0
    assert attempt_of(("ai-generated", "ai-attempt-2")) == 2
    assert attempt_of(("ai-attempt-1", "ai-attempt-3")) == 3, "a stray duplicate cannot lower it"


class _RecordingTickets:
    """Records create/comment so the escalation decision is testable without a tracker."""

    def __init__(self) -> None:
        self.created: list = []
        self.comments: list = []

    async def get(self, key: str):  # noqa: ANN202
        from in_lockstep.platform.tickets import Ticket

        return Ticket(key=key, title="t")

    async def comment(self, ticket: object, body: str) -> None:
        self.comments.append(body)

    async def create(self, draft: object):  # noqa: ANN202
        from in_lockstep.platform.tickets import Ticket

        self.created.append(draft)
        return Ticket(key=f"#{100 + len(self.created)}", title="t")


def test_escalate_opens_the_next_ai_generated_issue_below_the_cap() -> None:
    from in_lockstep.platform.propose import escalate
    from in_lockstep.platform.tickets import Ticket, TicketType

    tickets = _RecordingTickets()
    source = Ticket(key="#7", title="bug", labels=("ai-generated", "ai-attempt-1"))
    new = asyncio.run(escalate(tickets, source, "tests failed: 3 of 5", max_attempts=3))
    assert new is not None
    draft = tickets.created[0]
    assert draft.type is TicketType.BUG
    assert "ai-generated" in draft.labels
    assert "ai-attempt-2" in draft.labels, "the attempt count increments"


def test_escalate_stops_and_comments_at_the_cap() -> None:
    from in_lockstep.platform.propose import escalate
    from in_lockstep.platform.tickets import Ticket

    tickets = _RecordingTickets()
    source = Ticket(key="#7", title="bug", labels=("ai-generated", "ai-attempt-3"))
    new = asyncio.run(escalate(tickets, source, "tests failed", max_attempts=3))
    assert new is None, "no further issue is opened at the cap"
    assert not tickets.created
    assert tickets.comments and "human is needed" in tickets.comments[0]


def test_local_open_change_is_never_draft_and_mark_ready_is_a_noop(tmp_path: Path) -> None:
    scm = GitLocal(_repo(tmp_path))
    cs = ChangeSet(changes=(FileChange(path="a.py", contents="x = 1\n"),))
    change = asyncio.run(scm.open_change(cs, title="t", workflow="implement", run_id="r1", draft=True))
    assert change.draft is False, "local git has no draft state"
    # No PR to ready, and no error.
    asyncio.run(scm.mark_ready(change))


def test_the_conformance_kit_names_every_miss_at_once() -> None:
    """One run, every problem — an implementer should not fix-and-rerun six times."""
    from in_lockstep.platform.conformance import Nonconformant, assert_scm, assert_ticket_source

    class WrongTickets:
        def get(self, key: str) -> None:  # sync where the protocol says async
            return None

        async def comment(self, ticket: object, body: str) -> None:
            return None

    with pytest.raises(Nonconformant) as tickets_err:
        assert_ticket_source(WrongTickets())
    message = str(tickets_err.value)
    assert "get() must be `async def`" in message
    assert "missing method create()" in message

    class WrongScm:
        async def diff(self, base: str, head: str) -> None:  # async where callers do not await
            return None

        async def open_change(self, cs: object, *, title: str) -> None:  # no base=
            return None

    with pytest.raises(Nonconformant) as scm_err:
        assert_scm(WrongScm())
    message = str(scm_err.value)
    assert "diff() must be synchronous" in message
    assert "open_change() does not accept base=" in message
    assert "open_change() does not accept draft=" in message
    assert "missing method mark_ready()" in message


def test_a_minimal_ticket_source_refuses_what_it_does_not_implement() -> None:
    """The optional protocol methods default to `Unsupported`, not to AttributeError: a workflow
    can catch the refusal and degrade honestly, and an adapter never invents a signature."""
    from in_lockstep.core.ports import Unsupported as PortsUnsupported
    from in_lockstep.platform.conformance import assert_ticket_source

    class Minimal(TicketSource):
        async def get(self, key: str) -> Ticket:
            return Ticket(key=key, title="t")

        async def comment(self, ticket: Ticket, body: str) -> None:
            return None

    with pytest.raises(PortsUnsupported):
        asyncio.run(Minimal().create(TicketDraft(title="x")))
    with pytest.raises(PortsUnsupported):
        asyncio.run(Minimal().search("anything"))
    assert_ticket_source(Minimal())


def test_a_change_touching_a_protected_path_is_refused(tmp_path: Path) -> None:
    scm = GitLocal(_repo(tmp_path))
    cs = ChangeSet(changes=(FileChange(path="lockstep.py", contents="evil"),))
    with pytest.raises(GuardRefused):
        scm.apply(cs)


def test_framework_authored_changes_pass_the_guard(tmp_path: Path) -> None:
    scm = GitLocal(_repo(tmp_path))
    cs = ChangeSet(
        changes=(FileChange(path=".in-lockstep/ledger/r.json", contents="{}", author=ChangeAuthor.FRAMEWORK),)
    )
    assert scm.apply(cs) == [".in-lockstep/ledger/r.json"]


def test_diff_reports_the_paths_it_touched(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    scm = GitLocal(root)
    (root / "b.py").write_text("y = 2\n")
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "b"], cwd=root, capture_output=True)
    assert "b.py" in scm.diff("HEAD~1", "HEAD").paths


# -- ledger ---------------------------------------------------------------------------


def test_gate_ledger_1_records_carry_schema_and_epoch(tmp_path: Path) -> None:
    """The gate is that both are stamped, not that the schema is any particular number.

    It asserted `== 2`, which made a deliberate bump look like a regression. The number's history
    belongs beside the constant, where the comment says what each version changed; here what
    matters is that a reader can always tell which shape it is holding.
    """
    from in_lockstep.platform.ledger.store import SCHEMA

    ledger = InRepoLedger(root=tmp_path / "ledger")
    asyncio.run(ledger.append("r1", {"kind": "review", "tokens": 100}))
    record = json.loads((tmp_path / "ledger" / "r1.json").read_text())
    assert record["schema"] == SCHEMA
    assert isinstance(record["schema"], int)
    assert record["epoch"] == "in-process"


def test_a_record_without_an_epoch_reads_back_as_the_legacy_one(tmp_path: Path) -> None:
    directory = tmp_path / "ledger"
    directory.mkdir()
    (directory / "old.json").write_text(json.dumps({"kind": "review", "credits": 90}))
    assert read_ledger(directory, epoch="in-process") == []
    legacy = read_ledger(directory, epoch="ghaw")
    assert legacy and legacy[0]["epoch"] == "ghaw"


def test_gate_ledger_2_comparing_across_epochs_raises(tmp_path: Path) -> None:
    """The unit of measurement changed; a delta across it is a schema artefact."""
    before = [{"epoch": "ghaw", "kind": "review", "credits": 90}]
    after = [{"epoch": "in-process", "kind": "review", "tokens": 1000, "cost_usd": 0.5}]
    with pytest.raises(LedgerError, match="across epochs"):
        compare(before, after)


def test_summarize_counts_a_record_with_no_verdict_apart_and_leaves_it_out_of_the_rate() -> None:
    """`report --format json` and `--by-kind` read this. Eleven schema-4 `"completed"` records gave
    `failure_rate: 0.0`; a record with no verdict is not evidence of success (GATE-LEDGER-9)."""
    stats = summarize(
        [
            {"epoch": "in-process", "kind": "workflow", "status": "completed"},
            {"epoch": "in-process", "kind": "workflow", "status": "failed"},
        ]
    )
    stat = stats["workflow"]
    assert stat.runs == 2 and stat.unclassified == 1 and stat.judged == 1
    assert stat.failure_rate == 1.0
    assert summarize([{"epoch": "in-process", "kind": "w", "status": "completed"}])["w"].failure_rate is None


def test_gate_ledger_3_an_absent_measurement_is_none_not_zero() -> None:
    """The fabrication: coercing an absent key to 0.0 produced a clean -100% nothing earned."""
    stats = summarize([{"epoch": "in-process", "kind": "review", "status": "succeeded"}])
    assert stats["review"].runs == 1
    assert stats["review"].tokens is None, "unmeasured must not read as measured-as-zero"
    assert stats["review"].cost_usd is None
    assert stats["review"].mean_cost is None


def test_a_measured_zero_is_distinguishable_from_an_absent_one() -> None:
    stats = summarize([{"epoch": "in-process", "kind": "r", "tokens": 0, "cost_usd": 0.0}])
    assert stats["r"].tokens == 0, "measured as none is not the same as unmeasured"


def test_too_few_runs_reports_no_trend_rather_than_a_number() -> None:
    before = [{"epoch": "in-process", "kind": "r", "status": "succeeded"}] * 2
    after = [{"epoch": "in-process", "kind": "r", "status": "succeeded"}] * 2
    assert compare(before, after)[0]["verdict"] == "too few runs"


def test_gate_out_4_the_in_repo_store_declares_local_scope_and_refuses_cas(tmp_path: Path) -> None:
    """Declared from day one; refused honestly rather than implemented emptily."""
    ledger = InRepoLedger(root=tmp_path)
    assert ledger.scope == LedgerScope.LOCAL
    with pytest.raises(Unsupported, match="across machines"):
        asyncio.run(ledger.compare_and_set("k", None, "v"))


# -- tickets ---------------------------------------------------------------------------


def test_acceptance_criteria_come_from_a_heading_when_there_is_one() -> None:
    body = "Intro\n\n## Acceptance Criteria\n\n- first\n- second\n\n## Notes\n\n- ignored\n"
    assert criteria_from(body) == ("first", "second")


def test_criteria_fall_back_to_a_task_list() -> None:
    """Most trackers have no criteria field, and a checklist is what people actually write."""
    assert criteria_from("- [ ] do a thing\n- [x] done already\n") == (
        "do a thing",
        "done already",
    )


def test_ticket_text_is_untrusted_context() -> None:
    """Anyone who can file a ticket can write into a prompt."""
    ticket = Ticket(key="#1", title="t", description="d", comments=("a comment",))
    items = ticket.as_context()
    assert len(items) == 2
    assert all(i.provenance.value == "untrusted_external" for i in items)


def test_unknown_tracker_state_keeps_the_raw_value() -> None:
    ticket = Ticket(key="#1", title="t", state=TicketState.OTHER, raw_state="Awaiting Triage")
    assert ticket.raw_state == "Awaiting Triage"


# -- CI detection ------------------------------------------------------------------------


def test_detection_returns_none_outside_ci(monkeypatch) -> None:
    for var in ("GITHUB_ACTIONS", "GITLAB_CI"):
        monkeypatch.delenv(var, raising=False)
    assert detect() is None


def test_github_actions_is_detected(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/app")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "x")
    env = detect()
    assert env is not None
    assert env.host == "github"
    assert env.oidc_available, "federated credentials are preferred; a run must know they exist"
    assert env.reviewing, "which decides where configuration is loaded from"


def test_a_push_build_is_not_reviewing(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    env = detect()
    assert env is not None and not env.reviewing


def test_environment_can_be_constructed_explicitly() -> None:
    """Detection is a default, not magic. This is what tests use."""
    env = CiEnvironment(host="gitlab", repo="acme/app", event="merge_request_event")
    assert env.reviewing


# -- GATE-LEDGER-6 -----------------------------------------------------------------------------
#
# `credits`, `busy_seconds` and `pickup_seconds` are the previous substrate's vocabulary. They are
# absent from the current epoch by construction rather than by check, and "by construction" is a
# claim that decays: someone porting a dashboard reintroduces one, the reader still accepts it
# because a record is a dict, and GATE-LEDGER-2's epoch refusal never fires because the record
# says `in-process`. Then a credits-era number is averaged with a tokens-era one inside a single
# epoch, which is the failure GATE-LEDGER-3 exists for, arriving by a route it does not cover.

LEGACY_KEYS = ("credits", "busy_seconds", "pickup_seconds")


def test_gate_ledger_6_no_current_epoch_record_carries_the_old_vocabulary(tmp_path: Path) -> None:
    import asyncio as aio

    from in_lockstep.platform.ledger.store import InRepoLedger

    ledger = InRepoLedger(root=tmp_path)
    aio.run(
        ledger.append(
            "run-1",
            {"kind": "review", "tokens": 100, "cost_usd": 0.01, "wall_seconds": 1.5},
        )
    )
    record = json.loads(ledger.path_for("run-1").read_text())
    assert record["epoch"] == "in-process"
    for key in LEGACY_KEYS:
        assert key not in record, f"{key} is previous-substrate vocabulary"


def test_gate_ledger_6_no_emitted_metric_carries_it() -> None:
    """Metric names and dimension keys both. A dimension is as much a schema as a name."""
    import asyncio as aio

    from in_lockstep.core.outcome import Outcome, Status
    from in_lockstep.middleware.otel import Recorder, otel

    recorder = Recorder()
    lockstep = Lockstep.detect()
    lockstep.bind(_Iface := type("Iface", (), {}), _Emitting())
    lockstep.middleware += [otel(recorder)]
    aio.run(lockstep.context(run_id="m").do(_Iface()))

    assert recorder.metrics, "nothing was emitted, so this asserted nothing"
    for metric in recorder.metrics:
        for key in LEGACY_KEYS:
            assert key not in metric.name, f"{metric.name} carries {key}"
            assert key not in metric.dimensions, f"{metric.name} has dimension {key}"
        assert metric.name.startswith(("in_lockstep.", "gen_ai.")), metric.name
    assert Outcome is not None and Status is not None


class _Emitting:
    from in_lockstep.core.verbs import Capability as _C
    from in_lockstep.core.verbs import Verb as _V

    verb = _V.TEST
    capabilities = frozenset({_C.READS_REPO})

    async def invoke(self, ctx, inp):
        from in_lockstep.core.outcome import Cost, Outcome, Status

        return Outcome(status=Status.SUCCEEDED, cost=Cost(input_tokens=1, output_tokens=1, usd=0.01))


def test_the_ledger_has_one_writer() -> None:
    """A gate about what a record contains is worth only as much as the number of writers.

    `cli._write_ledger` hand-rolled the record and stamped `schema` and `epoch` as literals beside
    the store that owns those constants. Two writers means this gate can hold for one of them.
    """
    import ast

    source = (ROOT / "src" / "in_lockstep" / "cli.py").read_text()
    # Parsed, not grepped. The docstring on the function that stopped doing this quotes the very
    # literals it stopped writing, and a substring scan would trip on the explanation.
    tree = ast.parse(source)
    stamped = {
        key.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant) and key.value in {"epoch", "schema"}
    }
    assert not stamped, f"cli.py stamps {sorted(stamped)} of its own again"
    assert "InRepoLedger" in source, "cli.py no longer writes through the store"


def test_a_criteria_heading_over_a_task_list_strips_the_checkbox() -> None:
    """The commonest real issue shape, and the one the parser got wrong.

    A heading *and* checkboxes took the heading branch, which matched bullets without accounting
    for a checkbox — so every criterion arrived as "[ ] the actual text". The plain-task-list
    fallback handled it correctly, meaning the better-formatted input was the one that broke.
    """
    from in_lockstep.platform.tickets import criteria_from

    body = (
        "Some prose.\n\n"
        "## Acceptance criteria\n\n"
        "- [ ] the command resolves an Scm\n"
        "- [x] --dry-run still writes nothing\n"
        "- a plain bullet with no box\n"
    )
    assert criteria_from(body) == (
        "the command resolves an Scm",
        "--dry-run still writes nothing",
        "a plain bullet with no box",
    )


# -- who may ask for a run ----------------------------------------------------------------------
#
# A chat-ops trigger is an unauthenticated entry point wearing a familiar interface. These are the
# shapes GitHub actually sends, asserted here rather than trusted to a `grep` inside a YAML `if:`.

from in_lockstep.platform.actors import authorize, parse_codeowners  # noqa: E402

CODEOWNERS = """
# A comment naming @not-an-owner, which is not an entry.
*                        @alice
/src/privileged/         @bob @acme/security
/docs/                   @carol
"""


def test_codeowners_names_individuals_and_teams_separately() -> None:
    owners = parse_codeowners(CODEOWNERS)
    assert owners.handles == {"alice", "bob", "carol"}
    assert owners.teams == {"acme/security"}


def test_a_handle_inside_a_comment_is_not_an_owner() -> None:
    """The whole line is a comment, so nothing on it grants anything."""
    assert "not-an-owner" not in parse_codeowners(CODEOWNERS).handles


def test_an_org_member_may_ask() -> None:
    decision = authorize(actor="dana", association="MEMBER", codeowners=CODEOWNERS)
    assert decision.allowed and "MEMBER" in decision.reason


def test_a_code_owner_who_is_not_an_org_member_may_ask() -> None:
    """The two sources answer different questions, which is why either suffices.

    An outside collaborator can own a directory without being in the organisation at all.
    """
    decision = authorize(actor="@Carol", association="CONTRIBUTOR", codeowners=CODEOWNERS)
    assert decision.allowed and "CODEOWNERS" in decision.reason


def test_a_contributor_who_owns_nothing_may_not() -> None:
    decision = authorize(actor="mallory", association="CONTRIBUTOR", codeowners=CODEOWNERS)
    assert not decision.allowed


def test_a_collaborator_is_not_automatically_an_org_member() -> None:
    """COLLABORATOR is push access without membership — broader than what was asked for.

    A collaborator who should qualify is exactly the person CODEOWNERS names, and naming them is
    a decision somebody makes rather than one this default makes for them.
    """
    assert not authorize(actor="eve", association="COLLABORATOR", codeowners=CODEOWNERS).allowed


def test_a_bot_may_never_ask_however_it_is_associated() -> None:
    """A trigger a bot can fire is a loop, and this one spends money on every lap."""
    decision = authorize(actor="github-actions[bot]", association="OWNER", codeowners=CODEOWNERS)
    assert not decision.allowed and "loop" in decision.reason


def test_an_unresolvable_team_is_reported_rather_than_silently_denied() -> None:
    """Someone refused while sitting in a team that owns the code is half right, and should be told."""
    decision = authorize(actor="frank", association="CONTRIBUTOR", codeowners=CODEOWNERS)
    assert not decision.allowed
    assert "acme/security" in decision.reason
    assert "cannot be resolved here" in decision.reason


def test_an_empty_actor_decides_nothing() -> None:
    assert not authorize(actor="", association="OWNER", codeowners=CODEOWNERS).allowed


def test_this_repositorys_own_codeowners_parses() -> None:
    """A fixture that drifts from the real file tests the fixture."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    owners = parse_codeowners((root / ".github" / "CODEOWNERS").read_text())
    assert owners.handles, "the shipped CODEOWNERS names nobody, or the parser stopped matching"


# -- a title a host will actually accept --------------------------------------------------


def test_a_title_is_one_line_and_within_the_hosts_limit() -> None:
    """GitHub refuses a pull-request title over 256 characters, with a GraphQL error and not a
    truncation — and it refuses at the very end, after the branch is pushed and the model is paid.

    Run 33578430422 died there. The model had implemented #146 and the suite was green (1631
    passed); the title was a thousand characters of the model's own running commentary, taken from
    `changeset.summary`, and the work survived only because it was in the run's artifact.
    """
    from in_lockstep.platform.scm.base import MAX_TITLE_CHARS, title_line

    commentary = (
        "Right — staged writes aren't visible to search. That's expected.\n\n"
        "1. **test_a_ticket_in_four_records** — 4 records, cost sums to 109.73 ✓\n"
        "2. **test_both_spellings** — args.ticket and top-level ticket ✓\n"
    ) + ("x" * 900)
    shaped = title_line(commentary)

    assert "\n" not in shaped, "a commit message may have a body; a title may not"
    assert len(shaped) <= MAX_TITLE_CHARS
    assert shaped == "Right — staged writes aren't visible to search. That's expected."


def test_a_single_line_longer_than_the_limit_is_clipped_and_says_so() -> None:
    """The other half. First-line-only handles a summary with a body; this handles one that is a
    single unbroken paragraph, which is just as plausible from a model."""
    from in_lockstep.platform.scm.base import MAX_TITLE_CHARS, title_line

    shaped = title_line("feat: " + ("a very long explanation " * 40))
    assert len(shaped) <= MAX_TITLE_CHARS
    assert shaped.endswith("…"), "a clipped title says it was clipped"
    assert shaped.startswith("feat: a very long explanation")


def test_a_short_title_is_left_exactly_as_it_is() -> None:
    from in_lockstep.platform.scm.base import title_line

    assert title_line("feat(metrics): count attempts per ticket") == (
        "feat(metrics): count attempts per ticket"
    )


def test_an_empty_summary_still_produces_a_title() -> None:
    """A host refuses an empty title too, and "" is what a model that answered with only tool calls
    leaves behind."""
    from in_lockstep.platform.scm.base import title_line

    assert title_line("") == "changes"
    assert title_line("   \n\n  ") == "changes"


def test_interior_whitespace_is_collapsed_rather_than_carried() -> None:
    """A title is rendered on one line whatever it contains, so runs of space that came from
    wrapped prose would show up as gaps in the middle of it."""
    from in_lockstep.platform.scm.base import title_line

    assert title_line("feat:   count   attempts") == "feat: count attempts"


# -- counting what one workflow has open ----------------------------------------------------


def _pr(branch: str, number: int, *, title: str = "t", draft: bool = False) -> dict[str, Any]:
    return {
        "number": number,
        "url": f"https://github.com/o/r/pull/{number}",
        "title": title,
        "headRefName": branch,
        "isDraft": draft,
    }


def test_open_proposals_are_matched_on_the_branch_the_framework_wrote_not_on_the_title() -> None:
    """A ceiling counted from titles is a ceiling anybody can raise by naming a branch well. The
    only evidence that a change request is ours is the branch layout `branch_for` wrote."""
    scm = GitHubScm(".")
    scm._gh_json = lambda *a: [  # type: ignore[method-assign]
        _pr("in-lockstep/improve/run-1", 1),
        _pr("in-lockstep/implement/from-ticket/218/run-2", 2),
        _pr("feature/improve-things", 3, title="improve the prompts"),
    ]
    open_now = scm.open_changes_by_workflow("improve")
    assert [c.number for c in open_now] == [1]


def test_a_named_workflow_does_not_claim_a_shallower_ones_branches() -> None:
    """The strict direction, and the only one the branch layout can actually give. Asking for
    `implement/from-ticket` must not count a branch that `implement` opened, or one workflow's
    ceiling would be spent by another's work."""
    scm = GitHubScm(".")
    scm._gh_json = lambda *a: [  # type: ignore[method-assign]
        _pr("in-lockstep/implement/from-ticket/218/run-2", 2),
        _pr("in-lockstep/implement/run-3", 3),
    ]
    assert [c.number for c in scm.open_changes_by_workflow("implement/from-ticket")] == [2]


def test_a_family_prefix_counts_the_workflows_nested_under_it() -> None:
    """The other direction over-counts, and that is deliberate rather than sloppy.

    `in-lockstep/implement/218/run-2` (workflow `implement`, ticket 218) and
    `in-lockstep/implement/from-ticket/run-2` (workflow `implement/from-ticket`, no ticket) are the
    same shape — `ticket_from_branch` already records that the layout cannot be read positionally.
    So a shallower name cannot be made to exclude the deeper ones, and given the choice, a ceiling
    over-counts: refusing a run that could have proceeded costs a person one command, and letting
    one through because a branch went unrecognised is the failure the ceiling exists to prevent."""
    scm = GitHubScm(".")
    scm._gh_json = lambda *a: [  # type: ignore[method-assign]
        _pr("in-lockstep/implement/from-ticket/218/run-2", 2),
        _pr("in-lockstep/implement/run-3", 3),
    ]
    assert [c.number for c in scm.open_changes_by_workflow("implement")] == [3, 2]


def test_counting_open_proposals_is_not_capped_at_the_prompt_context_limit() -> None:
    """`changes_for` stops at MAX_CHANGES_READ because its output goes into a prompt. A count
    capped at three is a ceiling that stops counting before it stops anything."""
    scm = GitHubScm(".")
    scm._gh_json = lambda *a: [  # type: ignore[method-assign]
        _pr(f"in-lockstep/improve/run-{n}", n) for n in range(5)
    ]
    assert len(scm.open_changes_by_workflow("improve")) == 5


def test_a_listing_that_may_have_been_truncated_is_refused_rather_than_undercounted() -> None:
    """The host returned exactly as many rows as it was asked for, so there may be more. An
    undercounted ceiling lets a run through, which is the one failure this number exists to
    prevent, so it raises and the caller refuses instead of counting."""
    scm = GitHubScm(".")
    scm._gh_json = lambda *a: [  # type: ignore[method-assign]
        _pr(f"in-lockstep/improve/run-{n}", n) for n in range(4)
    ]
    with pytest.raises(RuntimeError, match="truncated"):
        scm.open_changes_by_workflow("improve", limit=4)
