"""GATE-LEDGER-8: a reconcile or absorb must refuse a run-id collision, not silently replace.

A `_reconcile` (called by `push()` when the first push is rejected) replays local records onto the
remote tree using `update-index --add --cacheinfo`. If the remote already holds a path for that
run id with different content, today's code overwrites it. `_merge_ref` (used by `absorb`) does
the same in the other direction.

Both paths must refuse with `HistoryError` instead of replacing, and the record already held by
the destination must be unchanged afterwards. Identical content is the no-op it has always been
(same blob, git records no change), and that must remain green.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from in_lockstep.platform.ledger import DEFAULT_BRANCH, GitLedger, HistoryError


def _repo(tmp_path: Path) -> Path:
    root = tmp_path
    root.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run(args, cwd=root, capture_output=True, check=True)

    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.test")
    run("git", "config", "user.name", "t")
    (root / "app.py").write_text("x = 1\n")
    run("git", "add", "-A")
    run("git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "base")
    run("git", "branch", "-M", "main")
    return root


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True).stdout.strip()


# -- GATE-LEDGER-8: push-path collision --------------------------------------------------------


def test_gate_ledger_8_a_reconcile_refuses_when_a_run_id_collides_with_different_content(
    tmp_path: Path,
) -> None:
    """Two clones that both record under the same run id with different content: the second push
    is refused by name, the remote's copy is kept exactly as it was, and a fresh fetch verifies
    clean.

    Before the fix `_reconcile` replayed the local record onto the remote tree unconditionally,
    replacing whatever was already there, and `verify()` on the result would flag the modification.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)

    first_clone = _repo(tmp_path / "a")
    second_clone = _repo(tmp_path / "b")
    for clone in (first_clone, second_clone):
        subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=clone, check=True)

    # Both clones append a record for the SAME run id but with different content.
    shared_run_id = "run-collision"
    asyncio.run(GitLedger(root=first_clone).append(shared_run_id, {"kind": "review", "cost_usd": 1.00}))
    asyncio.run(GitLedger(root=second_clone).append(shared_run_id, {"kind": "review", "cost_usd": 9.99}))

    # First clone pushes successfully.
    GitLedger(root=first_clone).push()

    # The content the remote holds for the run id after the first push.
    remote_blob_before = _git(origin, "show", f"{DEFAULT_BRANCH}:records/{shared_run_id}.json")

    # The second push must be refused because its run id collides with different content.
    with pytest.raises(HistoryError, match=shared_run_id):
        GitLedger(root=second_clone).push()

    # The remote's record must be exactly what the first clone wrote — untouched.
    remote_blob_after = _git(origin, "show", f"{DEFAULT_BRANCH}:records/{shared_run_id}.json")
    assert remote_blob_before == remote_blob_after, (
        "the remote's record was replaced by the second push even though content differed"
    )

    # A third observer that clones the remote should see a clean ledger.
    reader = _repo(tmp_path / "c")
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=reader, check=True)
    subprocess.run(
        ["git", "fetch", "origin", f"refs/heads/{DEFAULT_BRANCH}:refs/heads/{DEFAULT_BRANCH}"],
        cwd=reader,
        check=True,
        capture_output=True,
    )
    assert GitLedger(root=reader).verify() == [], (
        "verify() is non-empty on the remote copy — the collision was not refused, it was silently written"
    )


# -- GATE-LEDGER-8: absorb-path collision -------------------------------------------------------


def test_gate_ledger_8_absorbing_a_bundle_refuses_when_a_run_id_collides_with_different_content(
    tmp_path: Path,
) -> None:
    """Absorbing a bundle whose records include a run id already present with different content
    must raise HistoryError and leave the local record unchanged.

    Before the fix `_merge_ref` iterated the bundle's records and wrote each into the index
    unconditionally via `update-index --add --cacheinfo`, replacing any existing entry.
    """
    a = _repo(tmp_path / "a")
    b = _repo(tmp_path / "b")

    shared_run_id = "run-collision"
    asyncio.run(GitLedger(root=a).append(shared_run_id, {"kind": "review", "cost_usd": 1.00}))
    asyncio.run(GitLedger(root=b).append(shared_run_id, {"kind": "review", "cost_usd": 9.99}))

    # Record the blob for the run id as held by `a` before the absorb attempt.
    blob_before = _git(a, "show", f"{DEFAULT_BRANCH}:records/{shared_run_id}.json")

    bundle = GitLedger(root=b).bundle(tmp_path / "b.bundle")

    with pytest.raises(HistoryError, match=shared_run_id):
        GitLedger(root=a).absorb(bundle)

    # `a`'s own record must be unchanged.
    blob_after = _git(a, "show", f"{DEFAULT_BRANCH}:records/{shared_run_id}.json")
    assert blob_before == blob_after, (
        "the local record was replaced during absorb even though content differed"
    )


def test_gate_ledger_8_absorbing_a_bundle_with_identical_content_is_a_no_op(
    tmp_path: Path,
) -> None:
    """If both sides carry the same run id with the same content, absorb is idempotent: the same
    blob produces no change and verify() stays empty.

    This is the normal multi-machine flow where a bundle is absorbed twice — or where both CI and
    a local run happened to share a run id because the clock repeated. It must not fail.
    """
    a = _repo(tmp_path / "a")
    b = _repo(tmp_path / "b")

    shared_run_id = "run-same"
    payload = {"kind": "review", "cost_usd": 1.00}
    asyncio.run(GitLedger(root=a).append(shared_run_id, payload))
    asyncio.run(GitLedger(root=b).append(shared_run_id, payload))

    bundle = GitLedger(root=b).bundle(tmp_path / "b.bundle")

    # Must succeed, not raise.
    GitLedger(root=a).absorb(bundle)

    # No tampering flagged: same blob, no modification in the history.
    assert GitLedger(root=a).verify() == []
