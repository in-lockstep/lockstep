"""The ledger on an orphan branch.

The store that shipped first wrote one JSON file per run into the working tree — which is
gitignored, so every local run's record was written and then lost, and CI's survived ninety days
as an artifact. Meanwhile `docs/controls-crosswalk.md` and `docs/needs.md` both lean on the ledger
as the project's evidence.

An orphan branch fixes that without putting framework output into a diff a human is reading. What
these tests hold is the part that makes it safe to run at any moment: nothing is checked out, the
index is untouched, and the working branch is not written to.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from in_lockstep.platform.ledger import DEFAULT_BRANCH, GitLedger, HistoryError


def _repo(tmp_path: Path, *, identity: bool = True) -> Path:
    root = tmp_path / "repo" if tmp_path.name not in ("a", "b", "c") else tmp_path
    root.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run(args, cwd=root, capture_output=True, check=True)

    run("git", "init", "-q")
    if identity:
        run("git", "config", "user.email", "t@example.test")
        run("git", "config", "user.name", "t")
    (root / "app.py").write_text("x = 1\n")
    run("git", "add", "-A")
    run("git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "base")
    run("git", "branch", "-M", "main")
    return root


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True).stdout.strip()


def test_a_record_becomes_a_commit_on_the_history_branch(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    ledger = GitLedger(root=root)
    assert ledger.head() is None, "the branch does not exist until something records"

    asyncio.run(ledger.append("run-1", {"kind": "review", "cost_usd": 0.02}))
    asyncio.run(ledger.append("run-2", {"kind": "implement", "cost_usd": 0.44}))

    log = _git(root, "log", "--oneline", f"refs/heads/{DEFAULT_BRANCH}").splitlines()
    assert len(log) == 2
    assert [r["run_id"] for r in ledger.records()] == ["run-1", "run-2"]


def test_the_working_branch_and_tree_are_untouched(tmp_path: Path) -> None:
    """A run records itself in the middle of whatever the developer had going on."""
    root = _repo(tmp_path)
    (root / "wip.py").write_text("half a thought\n")
    before_head = _git(root, "rev-parse", "main")
    before_status = _git(root, "status", "--porcelain")

    asyncio.run(GitLedger(root=root).append("run-1", {"kind": "review"}))

    assert _git(root, "rev-parse", "main") == before_head, "the working branch moved"
    assert _git(root, "status", "--porcelain") == before_status, "the index or tree changed"
    assert (root / "wip.py").read_text() == "half a thought\n"
    assert _git(root, "rev-parse", "--abbrev-ref", "HEAD") == "main", "something was checked out"


def test_the_history_branch_shares_no_commit_with_the_working_branch(tmp_path: Path) -> None:
    """Orphan is the point: it must not appear in `git log main` or in anybody's diff."""
    root = _repo(tmp_path)
    asyncio.run(GitLedger(root=root).append("run-1", {"kind": "review"}))
    merge_base = subprocess.run(
        ["git", "merge-base", "main", DEFAULT_BRANCH], cwd=root, capture_output=True, text=True
    )
    assert merge_base.returncode != 0, "the history branch shares an ancestor with main"


def test_a_record_is_readable_back(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    ledger = GitLedger(root=root)
    asyncio.run(ledger.append("run-1", {"kind": "review", "decided": True}))
    record = asyncio.run(ledger.read("run-1"))
    assert record is not None
    assert record["kind"] == "review"
    assert record["run_id"] == "run-1"
    assert record["schema"], "records are stamped with a schema and an epoch"


def test_reading_a_run_that_never_recorded_is_none_not_an_error(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    asyncio.run(GitLedger(root=root).append("run-1", {"kind": "review"}))
    assert asyncio.run(GitLedger(root=root).read("run-404")) is None


def test_a_repository_with_no_git_identity_can_still_record(tmp_path: Path) -> None:
    """`commit-tree` refuses without one, and a fresh CI runner frequently has none."""
    root = _repo(tmp_path, identity=False)
    subprocess.run(["git", "config", "user.useConfigOnly", "true"], cwd=root, check=True)
    asyncio.run(GitLedger(root=root).append("run-1", {"kind": "review"}))
    assert GitLedger(root=root).head() is not None


def test_a_run_id_cannot_escape_the_records_directory(tmp_path: Path) -> None:
    """Run ids are partly caller-supplied, and this one is used as a path inside the tree."""
    root = _repo(tmp_path)
    ledger = GitLedger(root=root)
    asyncio.run(ledger.append("../../etc/passwd", {"kind": "review"}))
    listing = _git(root, "ls-tree", "-r", "--name-only", DEFAULT_BRANCH)
    assert all(line.startswith("records/") for line in listing.splitlines()), listing
    assert ".." not in listing


def test_secrets_do_not_reach_a_permanent_record(tmp_path: Path) -> None:
    """A ledger commit is forever, which makes it the worst place to leak a credential."""
    from in_lockstep.privileged.redact import Redact, SecretRegistry

    registry = SecretRegistry()
    registry.add("sk-abcdefghijklmnopqrstuvwxyz")
    root = _repo(tmp_path)
    ledger = GitLedger(root=root, redact=Redact(registry))
    asyncio.run(
        ledger.append("run-1", {"kind": "review", "reason": "rejected sk-abcdefghijklmnopqrstuvwxyz"})
    )
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in _git(root, "show", f"{DEFAULT_BRANCH}:records/run-1.json")


def test_pushing_with_no_history_refuses_rather_than_succeeding_quietly(tmp_path: Path) -> None:
    with pytest.raises(HistoryError, match="no history"):
        GitLedger(root=_repo(tmp_path)).push()


# -- moving history between machines -----------------------------------------------------------
#
# The job that records has `contents: read` and cannot push; the job that can push is a different
# runner with a fresh checkout. Without this, a CI run's record dies with its runner — the exact
# durability failure the orphan branch exists to fix, reintroduced one layer up.


def test_a_bundle_carries_history_to_a_runner_that_never_recorded(tmp_path: Path) -> None:
    recorder, publisher = _repo(tmp_path / "a"), _repo(tmp_path / "b")
    asyncio.run(GitLedger(root=recorder).append("ci-run", {"kind": "implement"}))
    GitLedger(root=recorder).bundle(tmp_path / "history.bundle")

    assert GitLedger(root=publisher).head() is None
    GitLedger(root=publisher).absorb(tmp_path / "history.bundle")
    assert [r["run_id"] for r in GitLedger(root=publisher).records()] == ["ci-run"]


def test_absorbing_keeps_what_the_receiving_clone_already_had(tmp_path: Path) -> None:
    """Two independent orphan histories, and neither may silently replace the other."""
    recorder, publisher = _repo(tmp_path / "a"), _repo(tmp_path / "b")
    asyncio.run(GitLedger(root=recorder).append("ci-run", {"kind": "implement"}))
    asyncio.run(GitLedger(root=publisher).append("local-run", {"kind": "review"}))
    GitLedger(root=recorder).bundle(tmp_path / "history.bundle")

    GitLedger(root=publisher).absorb(tmp_path / "history.bundle")
    assert sorted(r["run_id"] for r in GitLedger(root=publisher).records()) == [
        "ci-run",
        "local-run",
    ]


def test_bundling_nothing_refuses(tmp_path: Path) -> None:
    with pytest.raises(HistoryError, match="no history"):
        GitLedger(root=_repo(tmp_path)).bundle(tmp_path / "x.bundle")


def test_absorbing_a_missing_bundle_refuses(tmp_path: Path) -> None:
    with pytest.raises(HistoryError, match="no history bundle"):
        GitLedger(root=_repo(tmp_path)).absorb(tmp_path / "nope.bundle")


def test_a_rejected_push_is_reconciled_rather_than_reported(tmp_path: Path) -> None:
    """Concurrent runs are the design, not an edge case, so divergence is the ordinary path.

    Two clones both record, both push. The second is rejected by git; reconciling puts its records
    into the remote's tree rather than asking a person to resolve an orphan-branch conflict.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)

    first, second = _repo(tmp_path / "a"), _repo(tmp_path / "b")
    for clone in (first, second):
        subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=clone, check=True)

    asyncio.run(GitLedger(root=first).append("run-a", {"kind": "review"}))
    GitLedger(root=first).push()

    asyncio.run(GitLedger(root=second).append("run-b", {"kind": "implement"}))
    GitLedger(root=second).push()

    # Whatever order they arrived in, neither record was dropped.
    landed = _repo(tmp_path / "c")
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=landed, check=True)
    subprocess.run(
        ["git", "fetch", "origin", f"refs/heads/{DEFAULT_BRANCH}:refs/heads/{DEFAULT_BRANCH}"],
        cwd=landed,
        check=True,
        capture_output=True,
    )
    assert sorted(r["run_id"] for r in GitLedger(root=landed).records()) == ["run-a", "run-b"]


# -- GATE-LEDGER-8: tamper-evidence ----------------------------------------------------------
#
# The auditor's first question after "when did this run" is "how do I know this wasn't
# rewritten". `verify()` answers for the retained chain; the force-push case is the remote's
# (docs/controls-crosswalk.md), and the docstring says so rather than implying more.


def _tamper(ledger: GitLedger, run_id: str, *, delete: bool = False) -> None:
    """Rewrite the past the way a cover-up would: a new commit that edits an old record."""
    import tempfile

    head = ledger.head()
    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp) / "index"
        ledger._git("read-tree", str(head), index=index)
        if delete:
            ledger._git("update-index", "--force-remove", ledger.path_for(run_id), index=index)
        else:
            blob = ledger._git("hash-object", "-w", "--stdin", stdin='{"cost_usd": 0.0001}\n')
            ledger._git(
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{blob},{ledger.path_for(run_id)}",
                index=index,
            )
        tree = ledger._git("write-tree", index=index)
    commit = ledger._git(
        *ledger._identity(), "commit-tree", tree, "-p", str(head), "-m", "routine maintenance"
    )
    ledger._git("update-ref", ledger.ref, commit, str(head))


def test_gate_ledger_8_an_honest_history_verifies_clean(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    ledger = GitLedger(root=root)
    for i in range(3):
        asyncio.run(ledger.append(f"run-{i}", {"kind": "review", "cost_usd": 0.01 * i}))
    assert ledger.verify() == []


def test_gate_ledger_8_a_rewritten_record_is_flagged_with_its_commit(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    ledger = GitLedger(root=root)
    asyncio.run(ledger.append("run-1", {"kind": "review", "cost_usd": 26.32}))
    asyncio.run(ledger.append("run-2", {"kind": "review", "cost_usd": 0.02}))

    _tamper(ledger, "run-1")

    problems = ledger.verify()
    assert len(problems) == 1
    assert "records/run-1.json" in problems[0] and "modified" in problems[0]
    assert ledger.records(), "the tampered store still reads; verify is evidence, not a lock"


def test_gate_ledger_8_a_deleted_record_is_flagged_too(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    ledger = GitLedger(root=root)
    asyncio.run(ledger.append("run-1", {"kind": "review", "cost_usd": 1.00}))
    asyncio.run(ledger.append("run-2", {"kind": "review", "cost_usd": 0.02}))

    _tamper(ledger, "run-1", delete=True)

    problems = ledger.verify()
    assert len(problems) == 1
    assert "deleted" in problems[0]


def test_a_run_id_collision_reads_as_a_rewrite_because_it_is_one(tmp_path: Path) -> None:
    """Two runs sharing an id means one record silently replaced the other — worth the alarm."""
    root = _repo(tmp_path)
    ledger = GitLedger(root=root)
    asyncio.run(ledger.append("run-1", {"kind": "review", "cost_usd": 0.01}))
    asyncio.run(ledger.append("run-1", {"kind": "review", "cost_usd": 0.99}))
    assert any("modified" in p for p in ledger.verify())


def test_absorbing_a_bundle_does_not_read_as_tampering(tmp_path: Path) -> None:
    """The legitimate multi-machine flow must verify clean, or people learn to ignore the flag."""
    a = _repo(tmp_path / "a")
    b = _repo(tmp_path / "b")
    ledger_a, ledger_b = GitLedger(root=a), GitLedger(root=b)
    asyncio.run(ledger_a.append("run-a", {"kind": "review"}))
    asyncio.run(ledger_b.append("run-b", {"kind": "implement"}))

    bundle = ledger_b.bundle(tmp_path / "b.bundle")
    ledger_a.absorb(bundle)

    assert ledger_a.verify() == []
    assert [r["run_id"] for r in ledger_a.records()] == ["run-a", "run-b"]


def test_an_empty_history_has_nothing_to_verify(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert GitLedger(root=root).verify() == []


def test_doctor_fails_on_a_rewritten_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DOC167: tampering with the evidence breaks the same required check the controls use."""
    from in_lockstep import doctor as doctor_module

    root = _repo(tmp_path)
    ledger = GitLedger(root=root)
    asyncio.run(ledger.append("run-1", {"kind": "review", "cost_usd": 26.32}))
    _tamper(ledger, "run-1")

    monkeypatch.setenv("IN_LOCKSTEP_ORG_SPEND_LIMIT", "100")
    report = doctor_module.run(root)
    codes = [c.code for c in report.checks]
    assert "DOC167" in codes
    tampered = next(c for c in report.checks if c.code == "DOC167")
    assert tampered.severity is doctor_module.Severity.ERROR
    assert "force-push" in tampered.hint, "the check's blind spot is stated where it fires"


def test_doctor_says_nothing_about_a_repo_that_never_recorded(tmp_path: Path, monkeypatch) -> None:
    from in_lockstep import doctor as doctor_module

    monkeypatch.setenv("IN_LOCKSTEP_ORG_SPEND_LIMIT", "100")
    report = doctor_module.run(_repo(tmp_path))
    assert "DOC167" not in [c.code for c in report.checks]
