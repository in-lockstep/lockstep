"""A fork's propose opens its change on the repository it forked from.

When `target` differs from the checkout's origin, `open_change` must use the REST form
(`gh api repos/<target>/pulls`) with `head_repo` set, because `gh pr create` cannot express
cross-repository pulls when the fork shares its parent's owner. When `target` matches origin
(or is empty), `gh pr create` stays the path.

Ticket: #373
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from in_lockstep.core.types import ChangeSet, FileChange
from in_lockstep.platform.scm.base import ChangeRequest


def _repo(tmp_path: Path) -> Path:
    """A git repository with one commit and a bare origin."""
    root = tmp_path / "r"
    root.mkdir()

    def run(*args: str) -> None:
        subprocess.run(args, cwd=root, capture_output=True, check=True)

    run("git", "init", "-q")
    run("git", "branch", "-M", "main")
    run("git", "config", "user.email", "t@example.test")
    run("git", "config", "user.name", "t")
    (root / "README.md").write_text("hello\n")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "initial")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=root, check=True)
    return root


def _recording(
    calls: list[tuple[str, ...]], stdout: str = ""
) -> Callable[..., tuple[int, str, str]]:
    def fake(*args: str) -> tuple[int, str, str]:
        calls.append(args)
        return (0, stdout, "")

    return fake


# ---------------------------------------------------------------------------
# Same-repo path (target empty or equal to origin): `gh pr create` as before
# ---------------------------------------------------------------------------


def test_no_target_uses_gh_pr_create(tmp_path: Path) -> None:
    """The default (no target) stays on the `gh pr create` path — the existing behaviour."""
    from in_lockstep.platform.scm import GitHubScm

    root = _repo(tmp_path)
    scm = GitHubScm(root)
    calls: list[tuple[str, ...]] = []
    scm._gh = _recording(calls, "https://github.com/o/r/pull/5\n")  # type: ignore[method-assign]
    cs = ChangeSet(changes=(FileChange(path="a.py", contents="x = 1\n"),), summary="s")

    asyncio.run(scm.open_change(cs, title="t", workflow="implement", run_id="r1"))
    # Must go through `pr create`, not the REST form.
    assert any(c[:2] == ("pr", "create") for c in calls)
    assert not any("repos/" in c[1] if len(c) > 1 else False for c in calls if c[0] == "api")


# ---------------------------------------------------------------------------
# Cross-repo path (target differs from origin): REST API with head_repo
# ---------------------------------------------------------------------------


def test_target_different_from_origin_uses_rest_api(tmp_path: Path) -> None:
    """A fork targeting its parent must use the REST form (`gh api repos/<target>/pulls`)
    with `head_repo` set to the fork's owner/name, because `gh pr create --head` cannot
    express a cross-repository pull request when the fork shares the parent's owner."""
    from in_lockstep.platform.scm import GitHubScm

    root = _repo(tmp_path)
    scm = GitHubScm(root)

    # The REST form returns JSON, so we need _gh to return the PR URL for `api` calls.
    pr_json = json.dumps({"number": 7, "html_url": "https://github.com/upstream/repo/pull/7"})
    calls: list[tuple[str, ...]] = []
    scm._gh = _recording(calls, pr_json + "\n")  # type: ignore[method-assign]

    # Simulate that origin is "fork-owner/repo" by making the remote URL say so.
    subprocess.run(
        ["git", "remote", "set-url", "origin", "https://github.com/fork-owner/repo.git"],
        cwd=root, check=True,
    )
    cs = ChangeSet(changes=(FileChange(path="a.py", contents="x = 1\n"),), summary="s")

    change = asyncio.run(
        scm.open_change(
            cs, title="t", workflow="implement", run_id="r1", target="upstream/repo",
        )
    )
    # Must NOT use `pr create`.
    assert not any(c[:2] == ("pr", "create") for c in calls), (
        "cross-repo open must use the REST API, not `gh pr create`"
    )
    # Must call `gh api repos/upstream/repo/pulls` (the REST form).
    api_calls = [c for c in calls if c[0] == "api" and "repos/upstream/repo/pulls" in " ".join(c)]
    assert api_calls, "expected a REST call to repos/<target>/pulls"
    # The REST call must include `-f head_repo=fork-owner/repo` so GitHub knows which fork.
    rest_args = " ".join(api_calls[0])
    assert "head_repo" in rest_args
    assert "fork-owner/repo" in rest_args
    # The returned ChangeRequest carries the number from the REST response.
    assert change.number == 7


def test_target_same_as_origin_uses_gh_pr_create(tmp_path: Path) -> None:
    """When `target` matches origin's owner/repo, `gh pr create` is used — the same path as no
    target, because the fork is opening on itself."""
    from in_lockstep.platform.scm import GitHubScm

    root = _repo(tmp_path)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "https://github.com/acme/repo.git"],
        cwd=root, check=True,
    )
    scm = GitHubScm(root)
    calls: list[tuple[str, ...]] = []
    scm._gh = _recording(calls, "https://github.com/acme/repo/pull/3\n")  # type: ignore[method-assign]
    cs = ChangeSet(changes=(FileChange(path="a.py", contents="x = 1\n"),), summary="s")

    asyncio.run(
        scm.open_change(cs, title="t", workflow="implement", run_id="r1", target="acme/repo")
    )
    assert any(c[:2] == ("pr", "create") for c in calls)


# ---------------------------------------------------------------------------
# Conformance: `target=` is accepted by the Protocol and the adapters
# ---------------------------------------------------------------------------


def test_open_change_accepts_target_parameter() -> None:
    """The `open_change` signature must accept `target=` — committed on the Protocol before
    third parties implement, because retrofitting a keyword is a breaking change."""
    from in_lockstep.platform.conformance import assert_scm
    from in_lockstep.platform.scm import GitHubScm, GitLocal

    # Both shipped adapters must accept `target=` without a Nonconformant error.
    # (We cannot actually call open_change here — we just need the conformance kit to check it.)
    import inspect

    for cls in (GitHubScm, GitLocal):
        sig = inspect.signature(cls.open_change)
        assert "target" in sig.parameters, f"{cls.__name__}.open_change must accept target="
