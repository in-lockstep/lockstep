"""GitLab phase 2: the second host, exercised against a mock transport rather than a live server.

The point of a second `Scm` implementation is that it proves the protocol host-neutral: the same
run-branch discipline, trailers, Conventional-Commit subjects and draft-until-green flow as
GitHub, with only the host API different. So these tests hold `GitLabScm` to the same invariants
`test_platform.py` holds `GitHubScm` to — and check the GitLab-specific spellings (draft as a
title prefix, iid as the number, notes as the comment thread) map onto the framework's shapes.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from in_lockstep.core.ports import Unsupported
from in_lockstep.core.types import ChangeSet, FileChange
from in_lockstep.platform.conformance import assert_scm, assert_ticket_source
from in_lockstep.platform.scm import GitLabScm
from in_lockstep.platform.scm.gitlab import project_from_remote, server_from_remote
from in_lockstep.platform.tickets import GitLabIssues, TicketDraft, TicketState, TicketType
from in_lockstep.platform.tickets.base import Ticket

BASE = "https://gitlab.example.test/api/v4"


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


def _with_origin(tmp_path: Path) -> Path:
    """A repo whose origin is a local bare clone, so `git push` in a test goes somewhere real."""
    root = _repo(tmp_path)
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], capture_output=True, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=root, capture_output=True, check=True)
    return root


def _scm(root: Path, handler, **kwargs) -> GitLabScm:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    return GitLabScm(root, project="group/proj", client=client, **kwargs)


# -- remote parsing -------------------------------------------------------------------


def test_the_project_path_is_read_from_either_remote_spelling() -> None:
    assert project_from_remote("git@gitlab.example.com:group/project.git") == "group/project"
    assert project_from_remote("https://gitlab.example.com/group/sub/project.git") == "group/sub/project"
    assert project_from_remote("https://gitlab.example.com/group/project") == "group/project"
    assert project_from_remote("") == ""
    assert project_from_remote("not-a-remote") == "", "unfamiliar shapes are empty, never guessed"


def test_the_server_is_read_from_either_remote_spelling() -> None:
    assert server_from_remote("https://gitlab.example.com/g/p.git") == "https://gitlab.example.com"
    assert server_from_remote("git@gitlab.example.com:g/p.git") == "https://gitlab.example.com"
    assert server_from_remote("") == ""


def test_the_server_never_carries_a_credential_or_an_ssh_scheme() -> None:
    """A CI checkout's origin is `https://gitlab-ci-token:<token>@host/...`: the userinfo must
    not become part of an API base URL, or the token rides every request as basic auth — the
    exact tokens-never-in-URLs rule the adapter states. And an ssh remote maps to https with its
    port dropped, because `ssh://host:2222` is where git talks, not where the REST API lives."""
    assert (
        server_from_remote("https://gitlab-ci-token:sekret@gitlab.example.com/g/p.git")
        == "https://gitlab.example.com"
    )
    assert server_from_remote("ssh://git@gitlab.example.com/g/p.git") == "https://gitlab.example.com"
    assert server_from_remote("ssh://git@gitlab.example.com:2222/g/p.git") == "https://gitlab.example.com"
    assert (
        server_from_remote("https://gitlab.example.com:8443/g/p.git") == "https://gitlab.example.com:8443"
    ), "an http(s) port is an API port and survives"


# -- the Scm protocol, on GitLab ------------------------------------------------------


def test_gitlab_scm_satisfies_the_conformance_kit(tmp_path: Path) -> None:
    assert_scm(GitLabScm(_repo(tmp_path)))


def test_open_change_pushes_the_run_branch_and_opens_a_draft_mr(tmp_path: Path) -> None:
    """The whole discipline in one pass: run-scoped branch, conventional subject, trailers,
    a real push, and a merge request whose draft state is the title prefix GitLab uses."""
    root = _with_origin(tmp_path)
    posted: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":  # no base= means the project is asked for its default branch
            return httpx.Response(200, json={"default_branch": "main"})
        # raw_path, because httpx decodes `path` — and the encoded slash IS the assertion: the
        # API addresses a project by its path with the slashes escaped.
        assert req.url.raw_path.endswith(b"/projects/group%2Fproj/merge_requests")
        posted.update(json.loads(req.content))
        return httpx.Response(
            201, json={"iid": 7, "web_url": "https://gitlab.example.test/group/proj/-/merge_requests/7"}
        )

    scm = _scm(root, handler)
    cs = ChangeSet(changes=(FileChange(path="src/new.py", contents="x = 1\n"),))
    change = asyncio.run(
        scm.open_change(cs, title="add a thing", workflow="implement", run_id="r1", ticket="#5", draft=True)
    )

    assert change.branch == "in-lockstep/implement/5/r1"
    assert change.number == 7 and change.draft is True
    assert change.url.endswith("/merge_requests/7")
    assert change.title == "feat: add a thing", "ChangeRequest.title never carries the Draft: prefix"

    assert posted["source_branch"] == "in-lockstep/implement/5/r1"
    assert posted["title"] == "Draft: feat: add a thing", "draft is a title prefix on GitLab"
    assert "In-Lockstep-Run" in posted["description"], "the machine-readable block rides the MR body"

    # The branch actually reached the origin — the API cannot open an MR for an unpushed branch.
    on_origin = subprocess.run(
        ["git", "branch", "-a"], cwd=tmp_path / "origin.git", capture_output=True, text=True
    ).stdout
    assert "in-lockstep/implement/5/r1" in on_origin

    log = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=root, capture_output=True, text=True).stdout
    assert "feat: add a thing" in log and "Ticket: #5" in log and "In-Lockstep-Run: r1" in log


def test_open_change_targets_base_or_asks_for_the_default_branch(tmp_path: Path) -> None:
    """GitLab requires a target_branch where GitHub defaults one: an explicit `base` is passed
    through bare, and an empty one asks the project which branch is its default."""
    root = _with_origin(tmp_path)
    subprocess.run(["git", "branch", "release-1.0"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "push", "-q", "origin", "release-1.0"], cwd=root, capture_output=True)
    seen: list[tuple[str, str, dict | None]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        seen.append((req.method, req.url.path, body))
        if req.method == "GET":
            return httpx.Response(200, json={"default_branch": "main"})
        return httpx.Response(201, json={"iid": 1, "web_url": ""})

    scm = _scm(root, handler)
    cs = ChangeSet(changes=(FileChange(path="fix.py", contents="y\n"),))
    asyncio.run(scm.open_change(cs, title="backport", workflow="backport", run_id="r2", base="release-1.0"))
    assert seen[-1][2] is not None and seen[-1][2]["target_branch"] == "release-1.0"
    assert all(method != "GET" for method, _, _ in seen), "an explicit base asks nothing"

    seen.clear()
    subprocess.run(["git", "checkout", "-q", "-"], cwd=root, capture_output=True)
    asyncio.run(scm.open_change(cs, title="again", workflow="fix", run_id="r3"))
    assert seen[0][0] == "GET" and seen[0][1].endswith("/projects/group/proj")
    assert seen[-1][2] is not None and seen[-1][2]["target_branch"] == "main"


def test_mark_ready_strips_the_draft_prefix_by_rewriting_the_title(tmp_path: Path) -> None:
    from in_lockstep.platform.scm.base import ChangeRequest

    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"], seen["path"] = req.method, req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={})

    scm = _scm(_repo(tmp_path), handler)
    change = ChangeRequest(id="x", url="", branch="b", title="fix: it", number=7, draft=True)
    asyncio.run(scm.mark_ready(change))
    assert seen["method"] == "PUT" and seen["path"].endswith("/merge_requests/7")
    assert seen["body"] == {"title": "fix: it"}, "the title written back has no Draft: prefix"


def test_mark_ready_without_a_number_is_left_alone(tmp_path: Path) -> None:
    """A request with no iid is never guessed at — same rule as GitHub."""
    from in_lockstep.platform.scm.base import ChangeRequest

    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be reached
        raise AssertionError("no request should be made")

    scm = _scm(_repo(tmp_path), handler)
    asyncio.run(scm.mark_ready(ChangeRequest(id="x", url="", branch="b", title="t")))


def test_upsert_comment_edits_its_own_note_across_pages(tmp_path: Path) -> None:
    """The sticky comment is found even when it is not on the first page of notes — the exact
    thread-burying `gh api --paginate` prevents on GitHub."""
    calls: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.method == "GET" and req.url.params.get("page") == "1":
            return httpx.Response(200, json=[{"id": 1, "body": "someone else"}], headers={"x-next-page": "2"})
        if req.method == "GET":
            return httpx.Response(200, json=[{"id": 2, "body": "old body\n\n<!-- marker -->"}])
        return httpx.Response(200, json={})

    scm = _scm(_repo(tmp_path), handler)
    asyncio.run(scm.upsert_comment(9, "new body", "<!-- marker -->"))
    assert calls[-1][0] == "PUT" and calls[-1][1].endswith("/merge_requests/9/notes/2")


def test_upsert_comment_posts_fresh_when_no_note_carries_the_marker(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.method)
        if req.method == "GET":
            return httpx.Response(200, json=[])
        assert "<!-- m -->" in json.loads(req.content)["body"], "the marker rides the new note"
        return httpx.Response(201, json={})

    scm = _scm(_repo(tmp_path), handler)
    asyncio.run(scm.upsert_comment(9, "body", "<!-- m -->"))
    assert calls == ["GET", "POST"]


def test_api_errors_carry_gitlabs_body_and_never_the_token(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "insufficient_scope"})

    scm = _scm(_repo(tmp_path), handler, token="glpat-secret")
    with pytest.raises(RuntimeError, match="403") as err:
        scm._default_branch()
    assert "insufficient_scope" in str(err.value), "GitLab's own body names what failed"
    assert "glpat-secret" not in str(err.value)


# -- the TicketSource protocol, on GitLab ---------------------------------------------


def _issues(handler, **kwargs) -> GitLabIssues:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    return GitLabIssues(base_url=BASE, project="group/proj", client=client, **kwargs)


def _issue(iid: int = 42, *, state: str = "opened", labels: list[str] | None = None) -> dict:
    return {
        "iid": iid,
        "title": "Checkout 500s",
        "description": "It is broken.\n\n- [ ] returns 200\n- [ ] logs id",
        "state": state,
        "labels": labels if labels is not None else ["bug", "prod"],
        "assignees": [{"username": "dana"}],
        "milestone": {"title": "2.1"},
        "web_url": "https://gitlab.example.test/group/proj/-/issues/42",
    }


def test_gitlab_issues_satisfies_the_conformance_kit() -> None:
    assert_ticket_source(GitLabIssues(base_url=BASE, project="group/proj"))


def test_the_comment_cap_counts_people_not_machine_narration() -> None:
    """A busy issue's first notes page can be mostly system narration ("changed the label",
    "mentioned in commit"). The cap bounds the CONVERSATION, so the adapter paginates and filters
    before it caps — capping the raw page silently dropped the humans this exists to keep."""

    def handler(req: httpx.Request) -> httpx.Response:
        if not req.url.path.endswith("/notes"):
            return httpx.Response(200, json=_issue())
        if req.url.params.get("page") == "1":
            narration = [{"body": f"changed label {i}", "system": True} for i in range(100)]
            return httpx.Response(200, json=narration, headers={"x-next-page": "2"})
        return httpx.Response(200, json=[{"body": "a human wrote this", "system": False}])

    ticket = asyncio.run(_issues(handler).get("#42"))
    assert ticket.comments == ("a human wrote this",)


def test_get_maps_an_issue_and_its_notes_onto_the_framework_ticket() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/notes"):
            return httpx.Response(
                200,
                json=[
                    {"body": "changed the label", "system": True},
                    {"body": "confirmed on prod", "system": False},
                ],
            )
        return httpx.Response(200, json=_issue())

    ticket = asyncio.run(_issues(handler).get("#42"))
    assert ticket.key == "#42" and ticket.title == "Checkout 500s"
    assert ticket.state is TicketState.OPEN and ticket.raw_state == "opened"
    assert ticket.type is TicketType.BUG, "the type is a label, as on GitHub"
    assert ticket.acceptance_criteria == ("returns 200", "logs id")
    assert ticket.assignees == ("dana",)
    assert ticket.milestone == "2.1"
    assert ticket.comments == ("confirmed on prod",), "system notes are machine narration, not comments"
    assert ticket.url.endswith("/issues/42")


def test_create_posts_the_payload_and_reads_the_issue_back() -> None:
    posted: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/issues"):
            posted.update(json.loads(req.content))
            return httpx.Response(201, json={"iid": 43})
        if req.url.path.endswith("/notes"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=_issue(43))

    ticket = asyncio.run(
        _issues(handler).create(TicketDraft(title="new bug", description="d", labels=("ai-generated",)))
    )
    assert ticket.key == "#43"
    assert posted == {"title": "new bug", "description": "d", "labels": "ai-generated"}


def test_search_passes_the_query_and_maps_each_row() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json=[_issue(1), _issue(2)])

    found = asyncio.run(_issues(handler).search("checkout", limit=5))
    assert [t.key for t in found] == ["#1", "#2"]
    assert seen["params"] == {"search": "checkout", "per_page": "5"}


def test_add_labels_uses_the_add_labels_update() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"], seen["path"], seen["body"] = req.method, req.url.path, json.loads(req.content)
        return httpx.Response(200, json={})

    asyncio.run(_issues(handler).add_labels(Ticket(key="#4", title="t"), "triaged", "p2"))
    assert seen["method"] == "PUT" and seen["path"].endswith("/issues/4")
    assert seen["body"] == {"add_labels": "triaged,p2"}, "added, never replacing the existing set"


def test_transition_maps_coarse_and_refuses_what_it_cannot_mean() -> None:
    events: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        events.append(json.loads(req.content)["state_event"])
        return httpx.Response(200, json={})

    src = _issues(handler)
    ticket = Ticket(key="#4", title="t")
    asyncio.run(src.transition(ticket, TicketState.CLOSED))
    asyncio.run(src.transition(ticket, TicketState.DONE))
    asyncio.run(src.transition(ticket, TicketState.OPEN))
    assert events == ["close", "close", "reopen"]
    with pytest.raises(Unsupported, match="opened or closed"):
        asyncio.run(src.transition(ticket, TicketState.OPEN, raw="In Review"))
    with pytest.raises(Unsupported, match="blocked"):
        asyncio.run(src.transition(ticket, TicketState.BLOCKED))


# -- host detection and the hosted factory --------------------------------------------


def test_detect_host_prefers_the_running_ci_environment(tmp_path: Path, monkeypatch) -> None:
    from in_lockstep.platform.hosted import detect_host

    for var in ("GITHUB_ACTIONS", "GITLAB_CI"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GITLAB_CI", "true")
    assert detect_host(tmp_path) == "gitlab"


def test_detect_host_reads_the_tree_then_the_remote(tmp_path: Path, monkeypatch) -> None:
    from in_lockstep.platform.hosted import detect_host

    for var in ("GITHUB_ACTIONS", "GITLAB_CI"):
        monkeypatch.delenv(var, raising=False)

    root = _repo(tmp_path)
    assert detect_host(root) == "", "nothing places a bare repo — an honest absence"
    subprocess.run(
        ["git", "remote", "add", "origin", "git@gitlab.example.com:g/p.git"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    assert detect_host(root) == "gitlab", "the origin remote names the host"
    (root / ".github" / "workflows").mkdir(parents=True)
    assert detect_host(root) == "github", "one host's CI files alone are decisive"
    # A migrated repository: vestigial .github/workflows beside a .gitlab-ci.yml is ambiguous,
    # and the remote — where the change requests actually go — breaks the tie.
    (root / ".gitlab-ci.yml").write_text("stages: [review]\n")
    assert detect_host(root) == "gitlab", "both hosts' files present: the remote decides"


def test_detect_host_reads_a_gitlab_ci_file_without_env_or_remote(tmp_path: Path, monkeypatch) -> None:
    """The tree signal on its own: a fresh clone with no remote and no CI env must still be
    placed by the `.gitlab-ci.yml` it carries."""
    from in_lockstep.platform.hosted import detect_host

    for var in ("GITHUB_ACTIONS", "GITLAB_CI"):
        monkeypatch.delenv(var, raising=False)
    root = _repo(tmp_path)
    (root / ".gitlab-ci.yml").write_text("stages: [review]\n")
    assert detect_host(root) == "gitlab"


def test_hosted_tickets_prefers_the_ci_environment_over_a_credentialed_remote(
    tmp_path: Path, monkeypatch
) -> None:
    """A GitLab runner's origin is `https://gitlab-ci-token:<token>@host/...`. The ambient
    CI_SERVER_URL/CI_PROJECT_PATH are the documented defaults and carry no credential, so they
    win — a token must never end up inside the API base URL."""
    from in_lockstep.platform.hosted import hosted_tickets

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setenv("CI_SERVER_URL", "https://gitlab.example.com")
    monkeypatch.setenv("CI_PROJECT_PATH", "g/p")

    root = _repo(tmp_path)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://gitlab-ci-token:sekret@gitlab.example.com/g/p.git"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    tickets = hosted_tickets(root)
    assert isinstance(tickets, GitLabIssues)
    assert tickets.base_url == "https://gitlab.example.com" and "sekret" not in tickets.base_url
    assert tickets.project == "g/p"


def test_gitlab_ci_detection_carries_the_merge_request_iid(monkeypatch) -> None:
    """`review --comment` finds its thread through `ci.detect().pr_number`; without the iid the
    sticky-comment path this feature exists for is unreachable on the very pipelines it targets."""
    from in_lockstep.platform import ci

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setenv("CI_PIPELINE_SOURCE", "merge_request_event")
    monkeypatch.setenv("CI_MERGE_REQUEST_IID", "42")
    env = ci.detect()
    assert env is not None and env.host == "gitlab"
    assert env.pr_number == 42
    assert env.reviewing

    monkeypatch.delenv("CI_MERGE_REQUEST_IID")
    env = ci.detect()
    assert env is not None and env.pr_number is None, "absent outside a merge-request pipeline"


def test_hosted_factories_return_the_detected_hosts_adapters(tmp_path: Path, monkeypatch) -> None:
    from in_lockstep.platform.hosted import hosted_scm, hosted_tickets
    from in_lockstep.platform.scm import GitHubScm

    for var in ("GITHUB_ACTIONS", "GITLAB_CI", "CI_SERVER_URL", "CI_PROJECT_PATH"):
        monkeypatch.delenv(var, raising=False)

    root = _repo(tmp_path)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://gitlab.example.com/g/p.git"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    scm = hosted_scm(root)
    assert isinstance(scm, GitLabScm)
    tickets = hosted_tickets(root)
    assert isinstance(tickets, GitLabIssues)
    assert tickets.base_url == "https://gitlab.example.com" and tickets.project == "g/p"

    assert isinstance(hosted_scm(tmp_path), GitHubScm), "nothing detected falls back to the shipped default"


# -- the scaffolded trampoline --------------------------------------------------------


def test_init_on_a_gitlab_repository_writes_a_gitlab_trampoline(tmp_path: Path, monkeypatch) -> None:
    """The trampoline the host can actually run: `.gitlab-ci.yml`, version-pinned, with the
    review job active and the gate/work/propose split present (commented) in the same file."""
    import yaml
    from click.testing import CliRunner

    from in_lockstep import __version__
    from in_lockstep.cli import main

    for var in ("GITHUB_ACTIONS",):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["init"])
    assert result.exit_code == 0, result.output
    path = tmp_path / ".gitlab-ci.yml"
    assert path.exists() and not (tmp_path / ".github").exists()

    text = path.read_text()
    assert "IN_LOCKSTEP_VERSION" not in text, "the placeholder must be substituted"
    assert f"in-lockstep[anthropic]=={__version__}" in text, "pinned to the version that wrote it"

    parsed = yaml.safe_load(text)
    assert parsed["stages"] == ["gate", "review", "work", "propose"]
    review = parsed["review"]
    assert review["rules"] == [{"if": '$CI_PIPELINE_SOURCE == "merge_request_event"'}]
    assert review["variables"]["GIT_DEPTH"] == "0", "the diff needs full history"
    # The credential split ships in the same file, commented until its environments exist.
    for job in ("#gate:", "#work:", "#propose:"):
        assert job in text
    assert "docs/trampoline.md" in text, "the YAML points at the contract it implements"


def test_the_gitlab_work_job_provisions_before_doctor_on_an_image_that_carries_uv(
    tmp_path: Path, monkeypatch
) -> None:
    """Issue 185 on GitLab. The commented work job runs `in-lockstep provision` before `doctor`,
    not `|| true`, on uv's image so the constant line finds a provisioner for the most common
    Python layout. The active review job stays on python:3.11-slim and never provisions; neither
    do gate and propose (GATE-PROVISION-1)."""
    from click.testing import CliRunner

    from in_lockstep.cli import main

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.chdir(tmp_path)
    assert CliRunner().invoke(main, ["init"]).exit_code == 0
    text = (tmp_path / ".gitlab-ci.yml").read_text()

    work = text.split("#work:")[1].split("#propose:")[0]
    assert "#    - in-lockstep provision\n#    - in-lockstep doctor || true\n" in work
    assert "#  image: ghcr.io/astral-sh/uv:python3.11-bookworm-slim" in work
    review = text.split("\nreview:\n")[1].split("\n#gate:")[0]
    assert "image: python:3.11-slim" in review and "provision" not in review
    gate = text.split("#gate:")[1].split("#work:")[0]
    propose = text.split("#propose:")[1]
    assert "provision" not in gate and "provision" not in propose


def test_init_on_gitlab_writes_no_github_workflow_files(tmp_path: Path, monkeypatch) -> None:
    """`--implement`/`--fix` on a GitLab repository must not scaffold GitHub YAML the host would
    silently ignore: the Python halves are appended, and the output points at the gate/work/
    propose block already in .gitlab-ci.yml instead."""
    from click.testing import CliRunner

    from in_lockstep.cli import main

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["init", "--implement", "--fix"])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / ".github").exists(), "GitHub YAML on a GitLab host is dead weight"
    assert "gitlab" in result.output and "docs/trampoline.md" in result.output
    module = (tmp_path / ".lockstep" / "lockstep.py").read_text()
    assert "implement/from-ticket" in module and "fix/from-ticket" in module


def test_init_leaves_an_existing_gitlab_ci_file_alone(tmp_path: Path, monkeypatch) -> None:
    """A trampoline is never regenerated — and on GitLab the file may predate the framework."""
    from click.testing import CliRunner

    from in_lockstep.cli import main

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".gitlab-ci.yml").write_text("stages: [build]\n")

    result = CliRunner().invoke(main, ["init"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".gitlab-ci.yml").read_text() == "stages: [build]\n"
    assert "left alone" in result.output


# -- reading the review conversation ---------------------------------------------------
#
# Parity, and the reason it is tested rather than assumed: the GitHub adapter reaches three
# endpoints and this one reaches a single `notes` collection that mixes everything together. The
# same four facts have to come out of both, or "a reviewer's comment reaches the next run" is true
# on one host and a documentation error on the other.


def test_gitlab_changes_for_matches_the_source_branch(tmp_path: Path) -> None:
    """On the branch `branch_for` wrote, never on the description — a merge request a stranger
    opened saying "closes #218" is not a review of our change."""
    from in_lockstep.platform.scm.base import branch_for

    root = _repo(tmp_path)

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params.get("state") == "opened", "closing one is how a person says ignore it"
        return httpx.Response(
            200,
            json=[
                {
                    "iid": 219,
                    "web_url": "u/219",
                    "title": "Draft: ours",
                    "source_branch": branch_for("fix", "r1", ticket="#218"),
                },
                {"iid": 300, "web_url": "u/300", "title": "closes #218", "source_branch": "dana/hand-rolled"},
            ],
        )

    changes = asyncio.run(_scm(root, handler).changes_for("#218"))
    assert [c.number for c in changes] == [219]
    assert changes[0].draft is True
    assert changes[0].title == "ours", "ChangeRequest.title never carries the Draft: prefix"


def test_gitlab_remarks_locate_a_diff_note_and_drop_the_system_ones(tmp_path: Path) -> None:
    """ "changed the description" is an event, not something a reviewer said. A prompt full of them
    is a prompt with less room for the sentence that mattered."""
    root = _repo(tmp_path)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"author": {"username": "dana"}, "body": "thanks", "system": False},
                {"author": {"username": "bot"}, "body": "changed the description", "system": True},
                {
                    "author": {"username": "sam"},
                    "body": "iterate the entries",
                    "system": False,
                    "position": {"new_path": "actions/save/action.yml", "new_line": 29},
                },
            ],
        )

    remarks = asyncio.run(_scm(root, handler).remarks(219))
    assert [r.kind for r in remarks] == ["comment", "line"]
    assert remarks[1].as_text(where="!219").startswith("@sam reviewed actions/save/action.yml:29 on !219:")


def test_gitlab_remarks_strip_the_frameworks_own_marker(tmp_path: Path) -> None:
    """Its own sticky review note is gathered — it is what the human was replying to — but the
    marker that lets `upsert_comment` find it again is not a thing to teach a model to write."""
    root = _repo(tmp_path)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"author": {"username": "bot"}, "body": "findings\n\n<!-- in-lockstep:review:security -->"}
            ],
        )

    (remark,) = asyncio.run(_scm(root, handler).remarks(1))
    assert remark.body == "findings"


def test_gitlab_ticket_of_reads_the_record_it_wrote(tmp_path: Path) -> None:
    """Parity with the GitHub adapter, and the one place the two hosts genuinely differ is
    declared rather than papered over: `shared_numbering` is False here, so `ticket_for` will not
    resolve an iid at all — an issue and a merge request can both be number 7."""
    from in_lockstep.platform.scm.base import change_body

    root = _repo(tmp_path)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "description": change_body("x", {"Ticket": "#218"}),
                "source_branch": "in-lockstep/fix/218/r1",
            },
        )

    assert asyncio.run(_scm(root, handler).ticket_of(7)) == "#218"
    assert GitLabScm.shared_numbering is False


def test_gitlab_counts_open_proposals_past_the_hosts_page_cap(tmp_path: Path) -> None:
    """GitLab caps `per_page` at 100 whatever is asked for, so one request is a count with a
    silent ceiling on it. `changes_for` asks for 60 and never noticed; a ceiling cannot afford
    not to."""
    pages: list[dict[str, Any]] = []
    seen: list[Any] = []

    def _request(method: str, path: str, *, json: Any = None, params: Any = None) -> Any:
        seen.append(params)
        page = int((params or {}).get("page") or 1)
        return pages[page - 1] if page <= len(pages) else []

    scm = _scm(_repo(tmp_path), lambda r: httpx.Response(200, json=[]))
    scm._request = _request  # type: ignore[method-assign]
    pages = [
        [
            {"iid": n, "web_url": f"u{n}", "source_branch": "in-lockstep/improve/run", "title": "t"}
            for n in range(100)
        ],
        [{"iid": 100, "web_url": "u100", "source_branch": "in-lockstep/improve/run", "title": "t"}],
    ]
    assert len(scm.open_changes_by_workflow("improve")) == 101
    assert [p["page"] for p in seen] == [1, 2], seen
    assert all(p["per_page"] == 100 for p in seen)


def test_gitlab_reports_a_draft_merge_request_by_its_title_prefix_as_well(tmp_path: Path) -> None:
    """GitLab spells draft as a title prefix as well as a field, and a proposal opened as a draft
    still occupies the ceiling."""
    scm = _scm(_repo(tmp_path), lambda r: httpx.Response(200, json=[]))
    scm._request = lambda *a, **k: [  # type: ignore[method-assign]
        {"iid": 7, "web_url": "u", "source_branch": "in-lockstep/improve/r1", "title": "Draft: a change"}
    ]
    (only,) = scm.open_changes_by_workflow("improve")
    assert only.draft is True
    assert only.title == "a change"
