"""Backport: deterministic first, a model only on conflict (roadmap item 25).

The claims under test, in the order a release manager cares about them: the right commits are
found (by `Ticket:` trailer, the read half of the trailer discipline), a clean pick costs nothing
and consults nobody, a conflict without a resolver stops with the exact commands a person runs,
and a resolver may merge ONLY the files that conflict — everything else is implementing, which is
a different verb with different gates. The capability declaration follows composition, because
budget and approval gating hang off it.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from in_lockstep.adapters.backport import (
    BackportSpec,
    Conflict,
    GitBackport,
)
from in_lockstep.cli import main
from in_lockstep.core.outcome import Cost, Outcome, Status
from in_lockstep.core.types import FileChange
from in_lockstep.core.verbs import Capability, Verb


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _repo(path: Path) -> Path:
    """A repository with a release line that diverged before the fix landed on main."""
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.test")
    _git(path, "config", "user.name", "t")
    (path / "app.py").write_text("def greet():\n    return 'hello'\n")
    (path / "README.md").write_text("readme\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "chore: initial")
    _git(path, "branch", "release-1.0")
    return path


def _commit_fix(path: Path, *, ticket: str = "#7") -> str:
    """The fix on main, carrying the trailer a workflow commit would."""
    (path / "app.py").write_text("def greet():\n    return 'hello, fixed'\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "fix: greet returns the right thing", "-m", f"Ticket: {ticket}")
    return _git(path, "rev-parse", "HEAD")


class _Ticket:
    def __init__(self, key: str) -> None:
        self.key = key
        self.url = ""


@pytest.fixture()
def repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for var in [v for v in os.environ if v.startswith("GITHUB_")] + ["GITLAB_CI", "ANTHROPIC_API_KEY"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    return _repo(tmp_path)


def _run(adapter: GitBackport, spec: BackportSpec) -> Outcome:
    return asyncio.run(adapter.invoke(None, spec))


# -- the deterministic path -----------------------------------------------------------


def test_discovers_commits_by_ticket_trailer_and_stages_the_pick(repo: Path) -> None:
    """`Ticket:` trailers are how a backport finds its commits — the read half of the trailer
    discipline `commits_between` documents. The result is a ChangeSet against the TARGET line,
    at zero cost, with `resolved` empty: git wrote all of it, a model wrote none."""
    sha = _commit_fix(repo)
    _commit_unrelated(repo)

    outcome = _run(GitBackport(str(repo)), BackportSpec(target="release-1.0", ticket=_Ticket("#7")))
    assert outcome.status is Status.SUCCEEDED, outcome.findings
    report = outcome.value
    assert [p.sha for p in report.picked] == [sha]
    assert report.resolved == ()
    assert outcome.cost.usd == 0.0
    staged = {c.path: c.contents for c in report.changeset.changes}
    assert staged == {"app.py": "def greet():\n    return 'hello, fixed'\n"}
    assert report.changeset.ticket == "#7"
    assert "backport to release-1.0" in report.changeset.summary


def _commit_unrelated(path: Path) -> str:
    (path / "other.py").write_text("x = 1\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "feat: unrelated", "-m", "Ticket: #99")
    return _git(path, "rev-parse", "HEAD")


def test_explicit_commits_need_no_ticket(repo: Path) -> None:
    sha = _commit_fix(repo)
    outcome = _run(GitBackport(str(repo)), BackportSpec(target="release-1.0", commits=(sha,)))
    assert outcome.status is Status.SUCCEEDED
    assert outcome.value.picked[0].sha == sha


def test_a_deletion_travels_as_a_deletion(repo: Path) -> None:
    """A pick that removes a file must stage `contents=None`, not an empty file."""
    (repo / "README.md").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: drop the readme", "-m", "Ticket: #8")
    sha = _git(repo, "rev-parse", "HEAD")

    outcome = _run(GitBackport(str(repo)), BackportSpec(target="release-1.0", commits=(sha,)))
    assert outcome.status is Status.SUCCEEDED
    (change,) = outcome.value.changeset.changes
    assert change.path == "README.md"
    assert change.deleted


def test_a_pick_already_on_the_target_succeeds_with_nothing_staged(repo: Path) -> None:
    """Idempotence: re-running a done backport reports `already_present` rather than stopping at
    git's empty-pick prompt or proposing an empty pull request."""
    sha = _commit_fix(repo)
    first = _run(GitBackport(str(repo)), BackportSpec(target="release-1.0", commits=(sha,)))
    assert first.status is Status.SUCCEEDED

    _git(repo, "checkout", "-q", "release-1.0")
    _git(repo, "cherry-pick", "-x", sha)
    _git(repo, "checkout", "-q", "main")

    again = _run(GitBackport(str(repo)), BackportSpec(target="release-1.0", commits=(sha,)))
    assert again.status is Status.SUCCEEDED
    assert again.value.empty
    assert any(f.id == "backport.already_present" for f in again.findings)


# -- refusals -------------------------------------------------------------------------


def test_nothing_to_pick_is_blocked(repo: Path) -> None:
    _commit_fix(repo, ticket="#7")
    outcome = _run(GitBackport(str(repo)), BackportSpec(target="release-1.0", ticket=_Ticket("#404")))
    assert outcome.status is Status.BLOCKED
    assert outcome.reason == "backport.nothing_to_pick"


def test_no_commits_and_no_ticket_is_blocked(repo: Path) -> None:
    outcome = _run(GitBackport(str(repo)), BackportSpec(target="release-1.0"))
    assert outcome.status is Status.BLOCKED
    assert outcome.reason == "backport.no_commits"


def test_a_ref_that_looks_like_an_option_is_refused(repo: Path) -> None:
    """Option confusion, not injection: a ref never legitimately begins with a dash."""
    outcome = _run(GitBackport(str(repo)), BackportSpec(target="--force", commits=("abc",)))
    assert outcome.status is Status.BLOCKED
    assert outcome.reason == "backport.option_confusion"


def test_a_directory_that_is_not_a_repository_is_a_message_not_a_traceback(tmp_path: Path) -> None:
    outcome = _run(GitBackport(str(tmp_path / "empty")), BackportSpec(target="release-1.0", commits=("abc",)))
    assert outcome.status is Status.BLOCKED
    assert outcome.reason in ("backport.no_worktree", "backport.no_merge_base")


# -- conflicts ------------------------------------------------------------------------


def _diverge(repo: Path) -> str:
    """The fix on main, and a conflicting edit of the same line on the release branch."""
    sha = _commit_fix(repo)
    _git(repo, "checkout", "-q", "release-1.0")
    (repo / "app.py").write_text("def greet():\n    return 'hola'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fix: spanish greeting")
    _git(repo, "checkout", "-q", "main")
    return sha


def test_conflict_without_resolver_stops_with_the_manual_commands(repo: Path) -> None:
    """FAILED, not BLOCKED — no control refused; git met the ordinary hazard. The finding hands a
    person the exact retreat, because a stopped run that says only 'conflict' is a run they have
    to re-derive."""
    sha = _diverge(repo)
    outcome = _run(GitBackport(str(repo)), BackportSpec(target="release-1.0", commits=(sha,)))
    assert outcome.status is Status.FAILED
    assert outcome.reason == "backport.conflict"
    report = outcome.value
    assert report.conflict is not None and report.conflict.paths == ("app.py",)
    blocking = next(f for f in outcome.findings if f.id == "backport.conflict")
    assert "git cherry-pick -x" in blocking.message
    # The conflicted contents travel WITH their markers — what a resolver or a person reads.
    assert "<<<<<<<" in report.conflict.files[0].contents


class _StubResolver:
    """A resolver with a canned answer, so the escalation seam is testable without a model."""

    def __init__(self, files: tuple[FileChange, ...]) -> None:
        self.files = files
        self.seen: list[Conflict] = []

    async def resolve(self, ctx: object, conflict: Conflict) -> Outcome:
        self.seen.append(conflict)
        return Outcome(status=Status.SUCCEEDED, value=self.files, cost=Cost(usd=0.01))


def test_conflict_with_resolver_merges_and_marks_the_model_authored_paths(repo: Path) -> None:
    """The escalation: the resolver's contents complete the pick, the changeset carries them, and
    every model-authored path is flagged — a reviewer reads those files first."""
    sha = _diverge(repo)
    merged = "def greet():\n    return 'hola, fixed'\n"
    resolver = _StubResolver((FileChange(path="app.py", contents=merged),))

    outcome = _run(
        GitBackport(str(repo), resolver=resolver), BackportSpec(target="release-1.0", commits=(sha,))
    )
    assert outcome.status is Status.SUCCEEDED, outcome.findings
    report = outcome.value
    assert report.resolved == ("app.py",)
    assert {c.path: c.contents for c in report.changeset.changes} == {"app.py": merged}
    assert any(f.id == "backport.resolved_by_model" and f.path == "app.py" for f in outcome.findings)
    # The resolver saw the intent it was preserving, not only the markers.
    assert resolver.seen[0].commit == sha
    assert "hello, fixed" in resolver.seen[0].patch
    # The resolver's spend reaches the outcome — a model that wrote lines cost something.
    assert outcome.cost.usd == pytest.approx(0.01)


def test_a_resolution_may_only_touch_conflicted_paths(repo: Path) -> None:
    """A resolver that writes elsewhere is implementing, not resolving; the deterministic half
    refuses it even when the resolver's own check did not."""
    sha = _diverge(repo)
    resolver = _StubResolver(
        (
            FileChange(path="app.py", contents="def greet():\n    return 'x'\n"),
            FileChange(path="sneaky.py", contents="import os\n"),
        )
    )
    outcome = _run(
        GitBackport(str(repo), resolver=resolver), BackportSpec(target="release-1.0", commits=(sha,))
    )
    assert outcome.status is Status.FAILED
    assert outcome.reason == "backport.resolution_out_of_scope"


def test_a_refused_resolution_reports_the_conflict_it_could_not_clear(repo: Path) -> None:
    class _Refusing:
        async def resolve(self, ctx: object, conflict: Conflict) -> Outcome:
            return Outcome(status=Status.FAILED, reason="backport.empty_resolution", cost=Cost(usd=0.005))

    sha = _diverge(repo)
    outcome = _run(
        GitBackport(str(repo), resolver=_Refusing()), BackportSpec(target="release-1.0", commits=(sha,))
    )
    assert outcome.status is Status.FAILED
    assert outcome.reason == "backport.empty_resolution"
    assert outcome.value.conflict is not None
    assert outcome.cost.usd == pytest.approx(0.005), "spend on a failed resolution is still spend"


# -- the capability declaration -------------------------------------------------------


def test_capabilities_follow_composition() -> None:
    """Without a resolver nothing spends and nothing but git writes; with one, a model can author
    file contents — the exact conjunction GATE-APPROVAL-1 fires on. The declaration is what hangs
    budget and approval gating off the right runs and keeps them off the free ones."""
    plain = GitBackport(".")
    assert plain.capabilities == frozenset({Capability.READS_REPO})
    assert plain.verb is Verb.BACKPORT

    class _R:
        async def resolve(self, ctx: object, conflict: Conflict) -> Outcome:  # pragma: no cover
            raise NotImplementedError

    armed = GitBackport(".", resolver=_R())
    assert Capability.SPENDS_BUDGET in armed.capabilities
    assert Capability.WRITES_FILES in armed.capabilities


# -- the resolver adapter -------------------------------------------------------------


class _StubInvocation:
    def __init__(self, content: str) -> None:
        self.content = content
        self.truncated = False
        self.cost = Cost(usd=0.02)
        self.findings = ()
        self.exhausted = False


class _StubInvoker:
    def __init__(self, content: str) -> None:
        self.content = content

    async def run(self, **kwargs: object) -> _StubInvocation:
        return _StubInvocation(self.content)


def _conflict() -> Conflict:
    return Conflict(
        commit="a" * 40,
        subject="fix: greet",
        paths=("app.py",),
        files=(FileChange(path="app.py", contents="<<<<<<<\nx\n=======\ny\n>>>>>>>\n"),),
        patch="diff --git a/app.py b/app.py",
    )


def test_resolver_returns_the_merged_files(repo: Path) -> None:
    from in_lockstep.adapters.ai.backport import AiBackportResolver

    reply = json.dumps(
        {"files": [{"path": "app.py", "contents": "merged\n"}], "summary": "kept both", "notes": ["n1"]}
    )
    resolver = AiBackportResolver(lambda ctx: _StubInvoker(reply))
    outcome = asyncio.run(resolver.resolve(None, _conflict()))
    assert outcome.status is Status.SUCCEEDED
    assert outcome.value == (FileChange(path="app.py", contents="merged\n"),)
    assert any(f.id == "backport.resolution_note" for f in outcome.findings)


def test_resolver_refuses_a_file_that_did_not_conflict(repo: Path) -> None:
    from in_lockstep.adapters.ai.backport import AiBackportResolver

    reply = json.dumps({"files": [{"path": "other.py", "contents": "x\n"}], "summary": "s"})
    resolver = AiBackportResolver(lambda ctx: _StubInvoker(reply))
    outcome = asyncio.run(resolver.resolve(None, _conflict()))
    assert outcome.status is Status.FAILED
    assert outcome.reason == "backport.resolution_out_of_scope"


def test_resolver_reports_an_empty_answer_honestly(repo: Path) -> None:
    from in_lockstep.adapters.ai.backport import AiBackportResolver

    reply = json.dumps({"files": [], "summary": "could not"})
    resolver = AiBackportResolver(lambda ctx: _StubInvoker(reply))
    outcome = asyncio.run(resolver.resolve(None, _conflict()))
    assert outcome.status is Status.FAILED
    assert outcome.reason == "backport.empty_resolution"
    assert outcome.cost.usd == pytest.approx(0.02), "a useless answer still cost money"


# -- the command ----------------------------------------------------------------------


def test_cli_backport_is_free_and_stages_an_artifact(repo: Path) -> None:
    """The headline: a clean backport needs no key, no budget and no approval, and hands the
    privileged half a changeset plus the exact `apply --base` line that opens it against the
    release line."""
    sha = _commit_fix(repo)
    result = CliRunner().invoke(
        main,
        ["backport", "--target", "release-1.0", "--commit", sha, "--out", "cs.json"],
    )
    assert result.exit_code == 0, result.output
    assert "picked" in result.output
    assert "deterministic; no model was consulted" in result.output
    assert "--base release-1.0" in result.output
    assert Path("cs.json").exists() or (repo / "cs.json").exists()

    from in_lockstep.platform.ledger import GitLedger

    records = [r for r in GitLedger(root=repo).records() if r.get("kind") == "backport"]
    assert records, "a backport run leaves its record like every other verb"
    assert records[-1]["target"] == "release-1.0"
    assert records[-1]["picked"] == [sha]
    assert "model" not in records[-1], "no model was called, so no model is recorded"


def test_cli_apply_base_opens_the_change_against_the_release_line(repo: Path) -> None:
    """The privileged half: `apply --base` starts the run-scoped branch from the release line, so
    the backport lands where its changeset is true."""
    sha = _commit_fix(repo)
    staged = CliRunner().invoke(
        main, ["backport", "--target", "release-1.0", "--commit", sha, "--out", "cs.json"]
    )
    assert staged.exit_code == 0, staged.output

    applied = CliRunner().invoke(
        main,
        [
            "apply",
            "--from-artifact",
            "cs.json",
            "--base",
            "release-1.0",
            "--workflow",
            "backport",
            "--run-id",
            "7",
        ],
    )
    assert applied.exit_code == 0, applied.output
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "in-lockstep/backport/7"
    # Grown from the release line, not from main: the branch's parent is the release head.
    parent = _git(repo, "rev-parse", "HEAD~1")
    assert parent == _git(repo, "rev-parse", "release-1.0")
    assert (repo / "app.py").read_text() == "def greet():\n    return 'hello, fixed'\n"
    # Conventional Commits: a workflow-created backport commit is a fix.
    assert _git(repo, "log", "-1", "--pretty=%s").startswith("fix: ")


def test_cli_resolve_without_an_approval_path_is_refused(repo: Path) -> None:
    """`--resolve` makes the adapter a writing spender, and the framework's own gates engage:
    no approval path, no run — stated before a cent is spent or a conflict is met."""
    sha = _diverge(repo)
    result = CliRunner().invoke(
        main,
        [
            "backport",
            "--target",
            "release-1.0",
            "--commit",
            sha,
            "--resolve",
            "--budget",
            "1.00",
            "--dry-run",
        ],
    )
    assert result.exit_code != 0
    assert "ApprovalGate" in result.output


def test_cli_resolve_without_a_budget_is_refused(repo: Path) -> None:
    sha = _diverge(repo)
    result = CliRunner().invoke(
        main,
        ["backport", "--target", "release-1.0", "--commit", sha, "--resolve", "--approve", "--dry-run"],
    )
    assert result.exit_code != 0
    assert "budget" in result.output.lower()


def test_cli_conflict_names_the_retreat_and_exits_nonzero(repo: Path) -> None:
    sha = _diverge(repo)
    result = CliRunner().invoke(main, ["backport", "--target", "release-1.0", "--commit", sha])
    assert result.exit_code == 1, result.output
    assert "backport.conflict" in result.output
    assert "git cherry-pick -x" in result.output


def test_cli_requires_commits_or_a_ticket(repo: Path) -> None:
    result = CliRunner().invoke(main, ["backport", "--target", "release-1.0"])
    assert result.exit_code != 0
    assert "--commit" in result.output
