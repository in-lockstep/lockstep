"""GATE-FORK-1: a fork opens its change on the repository it forked from, or refuses by name.

Which repository a run writes to used to be whatever the host tool inferred from the checkout.
From a fork that inference is right for reading an issue and wrong for naming a head, and it is
one lever for both -- so the two monorepo demonstrations (in-lockstep/query-original and
in-lockstep/query-fork) pin `GH_REPO` to the fork and leave submission to the original as a
one-liner a person runs. `target` makes the repository an argument instead of an inference.

Reading is the other half, and the one a fork needs first: a change proposed to the parent is
implemented from the parent's ticket and answers the parent's reviewers, and none of that is on
the fork. `for_repo` narrows a source or an adapter to a repository; what follows the narrowing
and what deliberately does not is asserted below, because a call left behind would silently read
the wrong repository's #17.

Every assertion about the write is about a fact `gh pr create` cannot express or the REST endpoint requires,
because those are what a reimplementation would get wrong: `head_repo` (a fork sharing its
parent's owner cannot be named as a head at all), `base` (the porcelain defaults it, the endpoint
demands it), a typed `draft`, and rights checked before anything is pushed.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from in_lockstep.core.types import ChangeSet, FileChange
from in_lockstep.platform.scm import GitHubScm, GitLocal, TargetRefused
from in_lockstep.platform.scm.base import ChangeRequest
from in_lockstep.platform.scm.github import owner_repo_from_remote
from in_lockstep.platform.tickets.base import Ticket

FORK = "me/fork"
PARENT = "them/original"


def _forked_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository with one commit and a real bare origin it can actually push to.

    Real, because the alternative is a synthetic remote URL and a push that cannot succeed -- and
    the way to make a test pass against that is to stop checking whether the push worked, which is
    how a pull request gets opened for a branch nobody pushed. The origin is a filesystem path, so
    the fork's own slug comes from `GITHUB_REPOSITORY` the way it does in a fork's CI.
    """
    root = tmp_path / "fork"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=root, check=True)
    monkeypatch.setenv("GITHUB_REPOSITORY", FORK)
    return root


def _host(
    calls: list[tuple[str, ...]], *, may_push: bool = True, default_branch: str = "trunk"
) -> Callable[..., tuple[int, str, str]]:
    """A `gh` that answers by what it was asked, not with one canned string.

    `repos/<slug>` is the preflight -- rights and default branch in one response, which is why
    there is one call and not two. `repos/<slug>/pulls` is the cross-repository open.
    """

    def fake(*args: str) -> tuple[int, str, str]:
        calls.append(args)
        if args[:1] == ("api",) and args[1].endswith("/pulls"):
            body = {"html_url": f"https://github.com/{PARENT}/pull/7", "number": 7}
            return (0, json.dumps(body), "")
        if args[:1] == ("api",):
            body = {"permissions": {"pull": True, "push": may_push}, "default_branch": default_branch}
            return (0, json.dumps(body), "")
        return (0, f"https://github.com/{FORK}/pull/3\n", "")

    return fake


def _recording(calls: list[tuple[str, ...]], stdout: str = "{}") -> Callable[..., tuple[int, str, str]]:
    def fake(*args: str) -> tuple[int, str, str]:
        calls.append(args)
        return (0, stdout, "")

    return fake


def _changeset(path: str = "a.py") -> ChangeSet:
    return ChangeSet(changes=(FileChange(path=path, contents="x = 1\n"),), summary="s")


def _sent(call: tuple[str, ...]) -> dict[str, str]:
    """The `-f name=value` pairs of a `gh api` call, as a dict."""
    sent = [part for flag, part in zip(call, call[1:], strict=False) if flag in ("-f", "-F") and "=" in part]
    return {part.split("=", 1)[0]: part.split("=", 1)[1] for part in sent}


def _pushed(tmp_path: Path) -> list[str]:
    listed = subprocess.run(
        ["git", "ls-remote", "--heads", str(tmp_path / "origin.git")],
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        line.split("\t")[-1].removeprefix("refs/heads/")
        for line in listed.stdout.splitlines()
        if line.strip()
    ]


# -- the write --------------------------------------------------------------------------------


def test_gate_fork_1_a_target_that_is_not_origin_opens_through_the_rest_form_with_head_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GATE-FORK-1. `gh pr create` cannot open this at all: a fork sharing its parent's owner
    reports `Head sha can't be blank`, and `head_repo` -- the field that says which repository
    holds the branch -- is not one the porcelain exposes."""
    root = _forked_checkout(tmp_path, monkeypatch)
    scm = GitHubScm(root)
    calls: list[tuple[str, ...]] = []
    scm._gh = _host(calls)  # type: ignore[method-assign]

    change = asyncio.run(
        scm.open_change(
            _changeset(), title="t", workflow="implement", run_id="r1", ticket="#373", target=PARENT
        )
    )

    assert not [c for c in calls if c[:2] == ("pr", "create")], "the porcelain cannot express this"
    (opened,) = [c for c in calls if c[:1] == ("api",) and c[1].endswith("/pulls")]
    assert opened[1] == f"repos/{PARENT}/pulls", "the request is opened on the parent"
    sent = _sent(opened)
    assert sent["head"] == change.branch
    assert sent["head_repo"] == FORK, "the branch lives on the fork, and the API has to be told"
    assert change.url == f"https://github.com/{PARENT}/pull/7"
    assert change.number == 7
    assert change.repo == PARENT, "carried, because mark_ready runs later and elsewhere"
    assert change.branch in _pushed(tmp_path), "the branch reached the fork's own origin"


def test_the_rest_form_carries_the_base_the_endpoint_requires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`POST /repos/{owner}/{repo}/pulls` requires `base`; `gh pr create` defaults it from the
    repository. Every propose call passes no base at all, so the target's own default branch is
    what the preflight is asked for -- in the same response that answers the rights question."""
    root = _forked_checkout(tmp_path, monkeypatch)
    scm = GitHubScm(root)
    calls: list[tuple[str, ...]] = []
    scm._gh = _host(calls, default_branch="trunk")  # type: ignore[method-assign]

    asyncio.run(scm.open_change(_changeset(), title="t", workflow="implement", run_id="r1", target=PARENT))
    (opened,) = [c for c in calls if c[1].endswith("/pulls")]
    assert _sent(opened)["base"] == "trunk", "not `main`, which this repository's default is not"
    assert len([c for c in calls if c[:1] == ("api",) and not c[1].endswith("/pulls")]) == 1, (
        "one request answers both the rights question and the default branch"
    )

    # An explicit base -- a backport's release line -- wins over the default, as it does on the
    # same-repository path.
    calls.clear()
    subprocess.run(["git", "branch", "release-1.0"], cwd=root, check=True)
    asyncio.run(
        scm.open_change(
            _changeset("b.py"), title="t", workflow="fix", run_id="r2", base="release-1.0", target=PARENT
        )
    )
    (opened,) = [c for c in calls if c[1].endswith("/pulls")]
    assert _sent(opened)["base"] == "release-1.0"


def test_a_draft_crosses_as_a_typed_boolean_rather_than_the_string_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`-f draft=true` sends the string "true" where the API wants a boolean. `-F` is the typed
    flag, and a change opened not-as-a-draft is one that entered a human's review queue unasked."""
    root = _forked_checkout(tmp_path, monkeypatch)
    scm = GitHubScm(root)
    calls: list[tuple[str, ...]] = []
    scm._gh = _host(calls)  # type: ignore[method-assign]

    change = asyncio.run(
        scm.open_change(_changeset(), title="t", workflow="implement", run_id="r1", draft=True, target=PARENT)
    )
    (opened,) = [c for c in calls if c[1].endswith("/pulls")]
    assert "-F" in opened and opened[opened.index("-F") + 1] == "draft=true"
    assert "-f" not in [flag for flag, part in zip(opened, opened[1:], strict=False) if part == "draft=true"]
    assert change.draft is True

    calls.clear()
    plain = asyncio.run(
        scm.open_change(_changeset("c.py"), title="t", workflow="implement", run_id="r3", target=PARENT)
    )
    (opened,) = [c for c in calls if c[1].endswith("/pulls")]
    assert "draft=true" not in opened
    assert plain.draft is False


def test_gate_fork_1_a_target_naming_the_checkouts_own_repository_is_the_path_it_always_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GATE-FORK-1, the half that matters to everybody who is not a fork. An empty target, or one
    naming this repository, is `gh pr create` exactly as before -- no preflight request, no REST
    form, no recorded repository -- so a trampoline may pass the flag unconditionally."""
    root = _forked_checkout(tmp_path, monkeypatch)
    scm = GitHubScm(root)
    calls: list[tuple[str, ...]] = []
    scm._gh = _host(calls)  # type: ignore[method-assign]

    for run, target in (("r1", ""), ("r2", FORK)):
        calls.clear()
        change = asyncio.run(
            scm.open_change(
                _changeset(f"{run}.py"), title="t", workflow="implement", run_id=run, target=target
            )
        )
        assert [c for c in calls if c[:2] == ("pr", "create")], f"target={target!r} took the REST form"
        assert not [c for c in calls if c[:1] == ("api",)], "nobody was asked for permission"
        assert change.repo == "", "no repository is recorded, because it is the checkout's own"


# -- the refusals -----------------------------------------------------------------------------


def test_gate_fork_1_a_credential_that_cannot_write_the_target_refuses_before_anything_is_pushed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GATE-FORK-1, the refusal. A fork's workflow token can read the parent and cannot write it,
    which is the ordinary state rather than a bug -- and without this the run does its work, pays
    for its model calls, pushes a branch and only then learns it was never going to open
    anything."""
    root = _forked_checkout(tmp_path, monkeypatch)
    scm = GitHubScm(root)
    calls: list[tuple[str, ...]] = []
    scm._gh = _host(calls, may_push=False)  # type: ignore[method-assign]

    with pytest.raises(TargetRefused) as caught:
        asyncio.run(
            scm.open_change(_changeset(), title="t", workflow="implement", run_id="r1", target=PARENT)
        )
    assert caught.value.reason == "scm.no_rights_on_target"
    assert PARENT in str(caught.value)
    assert _pushed(tmp_path) == [], "a refusal left no branch on the remote"
    assert not [c for c in calls if c[1].endswith("/pulls")], "and asked for no pull request"
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    )
    assert branch.stdout.strip() == "main", "nor left the working tree on a run branch"


def test_an_origin_nothing_can_name_refuses_rather_than_sending_an_empty_head_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty `head_repo` does not fail: GitHub looks for the branch on the TARGET instead, so
    the guess either opens the wrong change or none, with nothing saying which happened."""
    root = _forked_checkout(tmp_path, monkeypatch)
    monkeypatch.delenv("GITHUB_REPOSITORY")  # a laptop; and origin is a filesystem path
    scm = GitHubScm(root)
    calls: list[tuple[str, ...]] = []
    scm._gh = _host(calls)  # type: ignore[method-assign]

    with pytest.raises(TargetRefused) as caught:
        asyncio.run(
            scm.open_change(_changeset(), title="t", workflow="implement", run_id="r1", target=PARENT)
        )
    assert caught.value.reason == "scm.unknown_origin"
    assert _pushed(tmp_path) == []
    assert calls == [], "refused before the credential was even asked"


def test_gate_fork_1_gitlab_declines_a_cross_project_target_by_name(tmp_path: Path) -> None:
    """GATE-FORK-1 on the other host. A merge request across projects needs `target_project_id`
    and a registered fork relationship; nothing here has ever run against a GitLab instance, so it
    declines by name rather than shipping an untested spelling of it."""
    from in_lockstep.platform.scm import GitLabScm

    root = _forked_checkout(tmp_path, pytest.MonkeyPatch())
    scm = GitLabScm(root, project="me/fork")
    with pytest.raises(TargetRefused) as caught:
        asyncio.run(
            scm.open_change(
                _changeset(), title="t", workflow="implement", run_id="r1", target="them/original"
            )
        )
    assert caught.value.reason == "scm.cross_project_unsupported"
    assert _pushed(tmp_path) == [], "declined before the branch was pushed"


def test_local_git_takes_the_keyword_and_ignores_it(tmp_path: Path) -> None:
    """`apply --target` on a laptop with no host bound still makes the branch. Refusing here would
    break the one place a person most wants to see what the change looks like first."""
    root = _forked_checkout(tmp_path, pytest.MonkeyPatch())
    change = asyncio.run(
        GitLocal(root).open_change(_changeset(), title="t", workflow="implement", run_id="r1", target=PARENT)
    )
    assert change.branch == "in-lockstep/implement/r1"
    assert change.repo == "", "a local branch is on no repository but this one"


# -- what the request carries -------------------------------------------------------------------


def test_mark_ready_names_the_repository_the_request_was_opened_on() -> None:
    """`gh pr ready 7` from a fork's checkout takes the pull request out of draft on whichever
    repository the tool infers, which for numbering that overlaps is a different pull request."""
    scm = GitHubScm(".")
    calls: list[tuple[str, ...]] = []
    scm._gh = _host(calls)  # type: ignore[method-assign]

    asyncio.run(scm.mark_ready(ChangeRequest(id="u", url="u", branch="b", title="t", number=7, repo=PARENT)))
    assert calls == [("pr", "ready", "7", "--repo", PARENT)]

    calls.clear()
    asyncio.run(scm.mark_ready(ChangeRequest(id="u", url="u", branch="b", title="t", number=7)))
    assert calls == [("pr", "ready", "7")], "no target, no flag: the call it has always been"


def test_owner_repo_from_remote_reads_both_spellings_git_writes() -> None:
    """Empty rather than guessed, the rule `project_from_remote` already follows: a wrong slug
    here names a real repository that is not this one."""
    assert owner_repo_from_remote("git@github.com:me/fork.git") == "me/fork"
    assert owner_repo_from_remote("https://github.com/me/fork.git") == "me/fork"
    assert owner_repo_from_remote("https://github.com/me/fork") == "me/fork"
    assert owner_repo_from_remote("/tmp/origin.git") == "", "a filesystem path names no repository"
    assert owner_repo_from_remote("") == ""


def test_the_conformance_check_requires_the_keyword_of_an_scm_bound_here() -> None:
    """Committed to the protocol now, before third parties implement it: a keyword added later is
    a breaking change, which is the argument `base` and `draft` are already in there on."""
    from in_lockstep.platform.conformance import Nonconformant, assert_scm

    class NoTarget:
        def diff(self, base: str, head: str = "HEAD") -> Any: ...

        async def open_change(
            self,
            cs: Any,
            *,
            title: str,
            body: str = "",
            ticket: str = "",
            workflow: str = "",
            run_id: str = "",
            base: str = "",
            draft: bool = False,
        ) -> Any: ...

        async def mark_ready(self, change: Any) -> None: ...

    with pytest.raises(Nonconformant, match="target"):
        assert_scm(NoTarget())


# -- the reads --------------------------------------------------------------------------------


def _fake_run(calls: list[list[str]], stdout: str = "{}") -> Callable[..., Any]:
    """A `subprocess.run` that records argv. The ticket source injects `--repo` at its own
    subprocess seam, so a fake one layer above it would test nothing."""

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    return run


def test_gate_fork_1_a_narrowed_ticket_source_reads_and_answers_on_the_repository_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GATE-FORK-1, the read half. A fork implementing the parent's ticket has to read the
    parent's ticket -- and `gh` infers the parent only by luck of forkhood, which `GH_REPO` then
    takes away. Named, it is neither luck nor a variable that moves everything at once."""
    from in_lockstep.platform.tickets import github as tickets_github

    calls: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", _fake_run(calls, json.dumps({"number": 373})))
    source = tickets_github.GitHubIssues().for_repo(PARENT)

    asyncio.run(source.get("#373"))
    asyncio.run(source.comment(Ticket(key="#373", title="t"), "said"))
    for argv in calls:
        assert argv[-2:] == ["--repo", PARENT], f"{argv} was asked on whatever gh inferred"

    # And un-narrowed, the call it has always been: no flag, so a repository that is not a fork
    # keeps `gh`'s own resolution and this whole mechanism is invisible to it.
    calls.clear()
    asyncio.run(tickets_github.GitHubIssues().get("#1"))
    assert "--repo" not in calls[0]


def test_gate_fork_1_a_call_that_addresses_a_number_follows_the_narrowing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GATE-FORK-1. An issue or pull-request number belongs to a repository, so every call that
    names one has to say which -- `ticket_for` resolving the number a comment was left on,
    `with_review` gathering what reviewers said, and the answer posted back."""
    root = _forked_checkout(tmp_path, monkeypatch)
    scm = GitHubScm(root).for_repo(PARENT)
    calls: list[tuple[str, ...]] = []
    scm._gh = _recording(calls)  # type: ignore[method-assign]

    asyncio.run(scm.ticket_of(17))
    asyncio.run(scm.changes_for("#373"))
    asyncio.run(scm.remarks(17))
    asyncio.run(scm.comment(17, "said"))
    assert calls, "the recorder saw nothing"
    for call in calls:
        addressed = "--repo" in call and call[call.index("--repo") + 1] == PARENT
        assert addressed or PARENT in " ".join(call), f"{call} addresses no repository"
        assert "{owner}/{repo}" not in " ".join(call), "the checkout's own substitution survived"


def test_a_call_about_this_checkouts_own_runs_does_not_follow_the_narrowing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the rule, and the reason it is a rule rather than a blanket flag: the CI
    that is executing is the FORK'S, whoever the change is for. Its artifacts, its runs and its
    own open pull requests are not the parent's, and a narrowing that moved them would have the
    improve loop reading a repository it cannot write."""
    root = _forked_checkout(tmp_path, monkeypatch)
    scm = GitHubScm(root).for_repo(PARENT)
    calls: list[tuple[str, ...]] = []
    scm._gh = _recording(calls, "[]")  # type: ignore[method-assign]

    scm.open_changes_by_workflow("implement")
    scm.run_artifacts("lockstep-run")
    assert calls, "the recorder saw nothing"
    for call in calls:
        assert PARENT not in " ".join(call), f"{call} was redirected at the parent"


def test_a_tracker_or_host_that_cannot_point_elsewhere_refuses_rather_than_answering_here(
    tmp_path: Path,
) -> None:
    """`Unsupported`, not `self`. Silently returning itself answers a question about another
    repository with this one's issues, and nothing in the answer says which was read."""
    from in_lockstep.core.ports import Unsupported
    from in_lockstep.platform.scm import GitLabScm
    from in_lockstep.platform.tickets import GitLabIssues
    from in_lockstep.platform.tickets.jira import JiraSource

    root = _forked_checkout(tmp_path, pytest.MonkeyPatch())
    for narrowable in (
        GitLabIssues(base_url="https://gitlab.test", project="g/p"),
        JiraSource(base_url="https://jira.test", project="P"),
        GitLocal(root),
        GitLabScm(root, project="g/p"),
    ):
        with pytest.raises(Unsupported):
            narrowable.for_repo(PARENT)


def test_the_conformance_check_requires_the_narrowing_of_a_ticket_source() -> None:
    """Committed to the shape now, with a refusing default so that inheriting it is enough. An
    implementation that is structural only has to answer the question one way or the other."""
    from in_lockstep.platform.conformance import Nonconformant, assert_ticket_source

    class NoNarrowing:
        async def get(self, key: str) -> Any: ...

        async def comment(self, ticket: Any, body: str) -> None: ...

        async def create(self, draft: Any) -> Any: ...

        async def search(self, query: str, *, limit: int = 20) -> Any: ...

        async def add_labels(self, ticket: Any, *labels: str) -> None: ...

        async def transition(self, ticket: Any, state: Any, *, raw: str = "") -> None: ...

    with pytest.raises(Nonconformant, match="for_repo"):
        assert_ticket_source(NoNarrowing())
