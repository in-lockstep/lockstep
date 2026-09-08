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
import json
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
    assert sorted(str(r["run_id"]) for r in GitLedger(root=publisher).records()) == [
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
    assert sorted(str(r["run_id"]) for r in GitLedger(root=landed).records()) == ["run-a", "run-b"]
    for clone in (first, second):
        left = subprocess.run(
            ["git", "for-each-ref", "refs/lockstep/"], cwd=clone, capture_output=True, text=True
        )
        assert left.stdout == "", f"a reconcile left a scratch ref behind: {left.stdout}"


# -- GATE-LEDGER-12: the ledger is readable on a checkout that never wrote it --------------------
#
# A CI checkout and a fresh clone create no local branch for a ref that is not HEAD, so every
# reader in CI read nothing for as long as the ledger read `refs/heads/lockstep-history` and
# nothing else (#307). The read falls back to the remote-tracking ref and creates no ref of its
# own; `pull()` is the act that makes a local branch.


def _shared(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A bare origin, a clone that records and pushes, and a `git clone` that never recorded.

    The second is a real clone rather than `_repo` plus a remote: `git clone` is what a second
    engineer and `actions/checkout` at full depth actually produce -- a remote-tracking ref and no
    local branch -- and a test that set the refs up by hand would prove the ledger reads what the
    test wrote.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    writer = _repo(tmp_path / "a")
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=writer, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=writer, check=True, capture_output=True)
    asyncio.run(GitLedger(root=writer).append("review-a", {"kind": "review", "cost_usd": 0.19}))
    GitLedger(root=writer).push()
    reader = tmp_path / "b"
    subprocess.run(["git", "clone", "-q", str(origin), str(reader)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "b@example.test"], cwd=reader, check=True)
    subprocess.run(["git", "config", "user.name", "b"], cwd=reader, check=True)
    return origin, writer, reader


def test_gate_ledger_12_a_clone_that_never_recorded_reads_the_remote_copy_and_creates_no_ref(
    tmp_path: Path,
) -> None:
    """B reads A's record without pushing, pulling or writing anything, and says which ref it read."""
    _origin, _writer, reader = _shared(tmp_path)
    ledger = GitLedger(root=reader)
    assert _git(reader, "rev-parse", "--verify", "--quiet", f"refs/heads/{DEFAULT_BRANCH}") == ""

    assert [r["run_id"] for r in ledger.records()] == ["review-a"]
    assert asyncio.run(ledger.read("review-a")) is not None
    assert ledger.resolved() == (
        f"refs/remotes/origin/{DEFAULT_BRANCH}",
        _git(reader, "rev-parse", "origin/lockstep-history"),
    )
    assert ledger.verify() == [] and ledger.absorbed_runs() == set()
    assert _git(reader, "rev-parse", "--verify", "--quiet", f"refs/heads/{DEFAULT_BRANCH}") == "", (
        "a read left a local branch behind"
    )
    diverged = ledger.divergence()
    assert diverged.read == f"refs/remotes/origin/{DEFAULT_BRANCH}"
    assert diverged.local_only is None and diverged.remote_only is None, "no local branch: not 0 behind"


def test_gate_ledger_12_a_clone_with_neither_ref_reads_nothing_and_says_so(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    ledger = GitLedger(root=root)
    assert ledger.resolved() is None and ledger.records() == []
    assert ledger.divergence().read == ""


def test_gate_ledger_12_the_first_write_on_such_a_clone_creates_the_local_branch_on_the_remote_commit(
    tmp_path: Path,
) -> None:
    """A record appended where only the remote-tracking ref exists lands on a local branch whose
    parent is the remote's commit, so the record sits beside A's and not on a second orphan
    history that a later push would have to reconcile."""
    _origin, _writer, reader = _shared(tmp_path)
    ledger = GitLedger(root=reader)
    asyncio.run(ledger.append("review-b", {"kind": "review"}))
    assert ledger.resolved() == (f"refs/heads/{DEFAULT_BRANCH}", ledger.head())
    assert _git(reader, "rev-parse", f"{DEFAULT_BRANCH}^") == _git(
        reader, "rev-parse", "origin/lockstep-history"
    )
    assert sorted(str(r["run_id"]) for r in ledger.records()) == ["review-a", "review-b"]
    diverged = ledger.divergence()
    assert (diverged.local_only, diverged.remote_only) == (1, 0)


def test_gate_ledger_12_a_clone_reading_the_remote_copy_has_nothing_of_its_own_to_push(
    tmp_path: Path,
) -> None:
    _origin, _writer, reader = _shared(tmp_path)
    with pytest.raises(HistoryError, match="no history"):
        GitLedger(root=reader).push()


def test_gate_ledger_12_pull_brings_both_sides_records_onto_the_local_branch_without_pushing(
    tmp_path: Path,
) -> None:
    """Local-only and remote-only records end up together on the local ref, `verify()` is empty,
    and the origin is exactly where A left it."""
    origin, writer, reader = _shared(tmp_path)
    ledger = GitLedger(root=reader)
    asyncio.run(ledger.append("review-b", {"kind": "review"}))
    asyncio.run(GitLedger(root=writer).append("review-a2", {"kind": "implement"}))
    GitLedger(root=writer).push()
    at_origin = _git(origin, "rev-parse", DEFAULT_BRANCH)

    pulled = ledger.pull()
    assert pulled.gained == 1 and not pulled.created
    assert sorted(str(r["run_id"]) for r in ledger.records()) == ["review-a", "review-a2", "review-b"]
    assert ledger.verify() == []
    assert _git(origin, "rev-parse", DEFAULT_BRANCH) == at_origin, "pull pushed"
    assert ledger.divergence().remote_only == 0 and ledger.divergence().local_only == 1
    assert ledger.pull().gained == 0, "nothing new: nothing folded"


def test_gate_ledger_12_pull_on_a_clone_with_no_local_branch_creates_one(tmp_path: Path) -> None:
    _origin, _writer, reader = _shared(tmp_path)
    pulled = GitLedger(root=reader).pull()
    assert pulled.created and pulled.gained == 1
    assert _git(reader, "rev-parse", DEFAULT_BRANCH) == _git(reader, "rev-parse", "origin/lockstep-history")


def test_gate_ledger_12_pull_from_a_remote_without_the_branch_refuses_by_name(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    root = _repo(tmp_path / "a")
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=root, check=True)
    with pytest.raises(HistoryError, match="could not fetch lockstep-history from origin"):
        GitLedger(root=root).pull()


def _second_engineer(reader: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    (reader / ".lockstep").mkdir()
    (reader / ".lockstep" / "lockstep.py").write_text(
        "from in_lockstep import Lockstep\nlockstep = Lockstep.detect()\n"
    )
    for var in [v for v in os.environ if v.startswith("GITHUB_")] + ["GITLAB_CI"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(reader)


def test_gate_ledger_12_report_and_explain_serve_the_second_engineer_and_name_the_ref_they_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command-line half: `report` and `history --explain` on a clone that never recorded
    show A's run instead of "no records yet", and say the numbers came from the remote copy."""
    from click.testing import CliRunner

    from in_lockstep.cli import main

    _origin, _writer, reader = _shared(tmp_path)
    _second_engineer(reader, monkeypatch)

    report = CliRunner().invoke(main, ["report"])
    assert report.exit_code == 0, report.output
    assert "no records yet" not in report.output
    assert f"ledger    read refs/remotes/origin/{DEFAULT_BRANCH}; no local branch yet" in report.output
    assert "`history --pull` creates one" in report.output

    explain = CliRunner().invoke(main, ["history", "--explain", "review-a"])
    assert explain.exit_code == 0, explain.output
    assert "review-a" in explain.output

    listing = CliRunner().invoke(main, ["history"])
    assert f"1 record(s), read from refs/remotes/origin/{DEFAULT_BRANCH})" in listing.output
    assert _git(reader, "rev-parse", "--verify", "--quiet", f"refs/heads/{DEFAULT_BRANCH}") == "", (
        "a command that only reads created the local branch"
    )


def test_gate_ledger_12_history_pull_then_report_shows_the_divergence_both_ways(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from in_lockstep.cli import main

    _origin, writer, reader = _shared(tmp_path)
    _second_engineer(reader, monkeypatch)

    pulled = CliRunner().invoke(main, ["history", "--pull"])
    assert pulled.exit_code == 0, pulled.output
    assert "pulled    created the local branch from origin/lockstep-history  (+1 record(s))" in pulled.output

    # A moves on and B records: one record each way, and the footer counts both directions.
    asyncio.run(GitLedger(root=writer).append("review-a2", {"kind": "review"}))
    GitLedger(root=writer).push()
    asyncio.run(GitLedger(root=reader).append("review-b", {"kind": "review"}))
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=reader, check=True, capture_output=True)
    report = CliRunner().invoke(main, ["report"])
    assert (
        f"ledger    read refs/heads/{DEFAULT_BRANCH}; 1 record(s) here not on origin/lockstep-history, "
        "1 there not here  (`history --pull` brings them)"
    ) in report.output, report.output


def test_the_ledger_line_is_a_dash_when_no_remote_copy_was_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A laptop with a local branch and no fetched remote copy is not "0 behind"."""
    from click.testing import CliRunner

    from in_lockstep.cli import main

    root = _repo(tmp_path)
    _second_engineer(root, monkeypatch)
    asyncio.run(GitLedger(root=root).append("review-1", {"kind": "review"}))
    report = CliRunner().invoke(main, ["report"])
    assert (
        f"ledger    read refs/heads/{DEFAULT_BRANCH}; origin/lockstep-history — (not fetched)"
        in report.output
    )


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


# -- GATE-LEDGER-11: a flagged rewrite can be acknowledged by name ------------------------------
#
# Every report on this repository opened with TAMPERED for four days because nobody could answer
# the flag (#295). An acknowledgement is a note appended to the branch -- who, why, what -- and it
# is the one shape in which a contradiction stops being an alarm without becoming a secret.


def _rewritten(tmp_path: Path) -> tuple[GitLedger, str]:
    """A ledger with one rewrite in it, and the commit that made it."""
    ledger = GitLedger(root=_repo(tmp_path))
    asyncio.run(ledger.append("run-1", {"kind": "review", "cost_usd": 0.02}))
    asyncio.run(ledger.append("run-2", {"kind": "review", "cost_usd": 0.03}))
    _tamper(ledger, "run-1")
    return ledger, str(ledger.head())


def test_gate_ledger_11_an_acknowledged_rewrite_stops_being_an_alarm_and_keeps_its_name(
    tmp_path: Path,
) -> None:
    ledger, rewrite = _rewritten(tmp_path)
    assert len(ledger.verify()) == 1

    note = ledger.acknowledge(
        rewrite[:8], reason="the same run id was reused before ids carried a stamp", by="t <t@example.test>"
    )
    assert ledger.verify() == [], "acknowledged, so no longer an alarm"
    (found,) = ledger.acknowledged_rewrites()
    assert found == note
    assert found.commit == rewrite and found.by == "t <t@example.test>"
    assert found.lines and "records/run-1.json" in found.lines[0], "what was rewritten travels with the note"
    assert len(ledger.records()) == 2, "a note is not a record"
    subject = _git(ledger.root, "log", "-1", "--format=%s", f"refs/heads/{DEFAULT_BRANCH}")
    assert subject == f"acknowledge {rewrite[:12]}: rewrote 1 record(s)"


def test_gate_ledger_11_a_note_covers_exactly_the_commit_it_names(tmp_path: Path) -> None:
    """A later rewrite, or an earlier one in another commit, is still an alarm. The note is not an
    allow-list and not a switch."""
    ledger, rewrite = _rewritten(tmp_path)
    ledger.acknowledge(rewrite, reason="known migration", by="t")
    _tamper(ledger, "run-2")
    problems = ledger.verify()
    assert len(problems) == 1 and "records/run-2.json" in problems[0]
    assert len(ledger.acknowledged_rewrites()) == 1


def test_gate_ledger_11_editing_the_note_itself_is_a_contradiction(tmp_path: Path) -> None:
    """The note is protected by the same walk as a record, so an acknowledgement cannot be
    quietly reworded after the fact."""
    import tempfile

    ledger, rewrite = _rewritten(tmp_path)
    ledger.acknowledge(rewrite, reason="known migration", by="t")
    head = str(ledger.head())
    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp) / "index"
        ledger._git("read-tree", head, index=index)
        reworded = json.dumps({"commit": rewrite, "by": "x", "reason": "z"}) + "\n"
        blob = ledger._git("hash-object", "-w", "--stdin", stdin=reworded)
        ledger._git(
            "update-index", "--add", "--cacheinfo", f"100644,{blob},acknowledged/{rewrite}.json", index=index
        )
        tree = ledger._git("write-tree", index=index)
    made = ledger._git(*ledger._identity(), "commit-tree", tree, "-p", head, "-m", "reword")
    ledger._git("update-ref", ledger.ref, made, head)
    assert any(f"acknowledged/{rewrite}.json was modified" in p for p in ledger.verify())


@pytest.mark.parametrize(
    ("reason", "by", "why"),
    [("", "t", "who and why"), ("known", "", "who and why"), ("   ", "t", "who and why")],
)
def test_gate_ledger_11_a_note_that_says_nothing_checkable_is_refused(
    tmp_path: Path, reason: str, by: str, why: str
) -> None:
    ledger, rewrite = _rewritten(tmp_path)
    with pytest.raises(HistoryError, match=why):
        ledger.acknowledge(rewrite, reason=reason, by=by)
    assert len(ledger.verify()) == 1, "nothing was written"


def test_gate_ledger_11_a_commit_that_rewrote_nothing_cannot_be_acknowledged(tmp_path: Path) -> None:
    """An acknowledgement of nothing is the allow-list this is not."""
    ledger, _rewrite = _rewritten(tmp_path)
    first = _git(ledger.root, "rev-list", "--max-parents=0", f"refs/heads/{DEFAULT_BRANCH}")
    with pytest.raises(HistoryError, match="rewrote nothing"):
        ledger.acknowledge(first, reason="x", by="t")
    with pytest.raises(HistoryError, match="not a commit on"):
        ledger.acknowledge("0" * 40, reason="x", by="t")
    with pytest.raises(HistoryError, match="not a commit on"):
        ledger.acknowledge(_git(ledger.root, "rev-parse", "main"), reason="x", by="t")


def test_gate_ledger_11_a_second_acknowledgement_is_refused_and_the_first_stands(tmp_path: Path) -> None:
    ledger, rewrite = _rewritten(tmp_path)
    ledger.acknowledge(rewrite, reason="first", by="amy")
    with pytest.raises(HistoryError, match="already acknowledged by amy"):
        ledger.acknowledge(rewrite, reason="second", by="zed")
    (found,) = ledger.acknowledged_rewrites()
    assert found.by == "amy" and found.reason == "first"


def test_gate_ledger_11_a_note_travels_with_the_records_through_absorb_and_push(tmp_path: Path) -> None:
    """Both rebuilds start from the other side's tree and add records; a note that did not travel
    would be a flag that came back the moment the branch was published."""
    a, b = _repo(tmp_path / "a"), _repo(tmp_path / "b")
    ledger_a, ledger_b = GitLedger(root=a), GitLedger(root=b)
    asyncio.run(ledger_a.append("run-a", {"kind": "review"}))
    asyncio.run(ledger_b.append("run-b", {"kind": "review"}))
    _tamper(ledger_b, "run-b")
    rewrite = str(ledger_b.head())
    ledger_b.acknowledge(rewrite, reason="known", by="t")

    ledger_a.absorb(ledger_b.bundle(tmp_path / "b.bundle"))
    assert ledger_a.verify() == [], "the absorbed rewrite arrived with its note"
    assert [n.commit for n in ledger_a.acknowledged_rewrites()] == [rewrite]

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    for clone in (a, b):
        subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=clone, check=True)
    ledger_a.push()
    asyncio.run(ledger_b.append("run-b2", {"kind": "review"}))
    ledger_b.push()  # rejected once, reconciled onto a's tree, pushed
    landed = GitLedger(root=a)
    landed._git("fetch", "origin", f"{landed.ref}:{landed.ref}")
    assert landed.verify() == [] and len(landed.acknowledged_rewrites()) == 1


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


def test_gate_ledger_11_doctor_notes_an_acknowledged_rewrite_and_stays_red_for_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The required check stops failing on a rewrite somebody explained, and says who and why as
    a NOTE -- so the explanation is on the same page as the alarm would have been."""
    from in_lockstep import doctor as doctor_module

    ledger, rewrite = _rewritten(tmp_path)
    ledger.acknowledge(rewrite, reason="known migration", by="t <t@example.test>")
    monkeypatch.setenv("IN_LOCKSTEP_ORG_SPEND_LIMIT", "100")
    report = doctor_module.run(ledger.root)
    codes = [c.code for c in report.checks]
    assert "DOC167" not in codes
    noted = next(c for c in report.checks if c.code == "DOC173")
    assert noted.severity is doctor_module.Severity.NOTE
    assert "t <t@example.test>" in noted.message and "known migration" in noted.hint

    _tamper(ledger, "run-2")
    again = doctor_module.run(ledger.root)
    assert "DOC167" in [c.code for c in again.checks], "an unexplained rewrite is still an ERROR"
    assert "DOC173" in [c.code for c in again.checks], "and the explained one is still named"


def test_doctor_says_nothing_about_a_repo_that_never_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from in_lockstep import doctor as doctor_module

    monkeypatch.setenv("IN_LOCKSTEP_ORG_SPEND_LIMIT", "100")
    report = doctor_module.run(_repo(tmp_path))
    assert "DOC167" not in [c.code for c in report.checks]


# -- a fixture id cannot be published ----------------------------------------------------------


def test_a_run_id_only_a_fixture_mints_cannot_be_pushed_reconciled_or_absorbed(tmp_path: Path) -> None:
    """Issue 312. `triage-412` and two `wayfinder-*-local` records sat on this repository's real
    ledger, and `report` counted a triage run that never happened. A local append accepts them --
    a test's tmp root is where they belong -- and every path that publishes refuses them by name:
    the fast-forward push, the reconcile a rejected push makes, and a bundle absorbed on the way."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    first, second = _repo(tmp_path / "a"), _repo(tmp_path / "b")
    for clone in (first, second):
        subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=clone, check=True)

    asyncio.run(GitLedger(root=first).append("triage-412", {"kind": "triage"}))
    with pytest.raises(HistoryError, match="triage-412"):
        GitLedger(root=first).push()  # the fast path: an empty remote
    assert _git(origin, "rev-parse", "--verify", "--quiet", f"refs/heads/{DEFAULT_BRANCH}") == "", (
        "the refused push reached the remote"
    )

    asyncio.run(GitLedger(root=second).append("run-real", {"kind": "review"}))
    GitLedger(root=second).push()
    asyncio.run(GitLedger(root=first).append("run-mine", {"kind": "review"}))
    with pytest.raises(HistoryError, match="triage-412"):
        GitLedger(root=first).push()  # the reconcile path: a rejected push
    landed = _repo(tmp_path / "c")
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=landed, check=True)
    assert GitLedger(root=landed).pull().gained == 1, "the remote holds only the real record"

    bundle = GitLedger(root=first).bundle(tmp_path / "a.bundle")
    with pytest.raises(HistoryError, match="triage-412"):
        GitLedger(root=second).absorb(bundle)
    assert [r["run_id"] for r in GitLedger(root=second).records()] == ["run-real"]

    fresh = _repo(tmp_path / "d")
    with pytest.raises(HistoryError, match="wayfinder-work-local"):
        asyncio.run(GitLedger(root=fresh).append("wayfinder-work-local", {"kind": "workflow"}))
        GitLedger(root=fresh).absorb(GitLedger(root=fresh).bundle(tmp_path / "d.bundle"))
        # A bundle that would CREATE the branch is checked too.
        GitLedger(root=_repo(tmp_path / "e")).absorb(tmp_path / "d.bundle")


def test_a_fixture_id_the_branch_already_carries_does_not_block_the_cleanup(tmp_path: Path) -> None:
    """The four that leaked are removed by a person; until then a push or pull that merely meets
    them on the branch is not refused, or the refusal would block its own cleanup."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    first = _repo(tmp_path / "a")
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=first, check=True)
    asyncio.run(GitLedger(root=first).append("triage-412", {"kind": "triage"}))
    subprocess.run(
        ["git", "push", "-q", "origin", f"refs/heads/{DEFAULT_BRANCH}:refs/heads/{DEFAULT_BRANCH}"],
        cwd=first,
        check=True,
        capture_output=True,
    )  # the leak, made the way it happened: outside the framework's own push
    second = _repo(tmp_path / "b")
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=second, check=True)
    assert GitLedger(root=second).pull().gained == 1
    asyncio.run(GitLedger(root=second).append("run-new", {"kind": "review"}))
    GitLedger(root=second).push()
    assert sorted(str(r["run_id"]) for r in GitLedger(root=second).records()) == ["run-new", "triage-412"]


# -- GATE-OUT-4, the SHARED half: compare-and-set over the remote ref --------------------------


def _origin_and_clones(tmp_path: Path, count: int) -> tuple[Path, list[Path]]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    clones = []
    for i in range(count):
        clone = _repo(tmp_path / f"clone{i}")
        subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=clone, check=True)
        clones.append(clone)
    return origin, clones


def test_gate_out_4_a_shared_git_ledger_swaps_on_the_remote_ref_and_exactly_one_claim_wins(
    tmp_path: Path,
) -> None:
    """GATE-OUT-4, SHARED. Eight clones of one bare origin claim one key at once, each expecting
    it absent: the remote's ref lock lets exactly one create it, the other seven are told it
    moved, and nothing of ours sat between them. A later swap with the winner's value succeeds,
    the same swap again is stale, and every clone reads the value the remote holds."""
    import asyncio

    _origin, clones = _origin_and_clones(tmp_path, 8)
    ledgers = [GitLedger(root=c, shared=True) for c in clones]
    assert {ledger.scope for ledger in ledgers} == {"shared"}

    async def race() -> list[bool]:
        return list(
            await asyncio.gather(
                *(led.compare_and_set("park/run-1", None, f"claim-{i}") for i, led in enumerate(ledgers))
            )
        )

    outcomes = asyncio.run(race())
    assert outcomes.count(True) == 1, outcomes
    winner = outcomes.index(True)
    held = asyncio.run(ledgers[0].state("park/run-1"))
    assert held == f"claim-{winner}"
    assert all(asyncio.run(led.state("park/run-1")) == held for led in ledgers[1:3])

    # The swap: correct expectation wins, a stale one is refused, and the value moved once.
    assert asyncio.run(ledgers[3].compare_and_set("park/run-1", held, "resumed")) is True
    assert asyncio.run(ledgers[4].compare_and_set("park/run-1", held, "resumed-again")) is False
    assert asyncio.run(ledgers[5].state("park/run-1")) == "resumed"
    assert asyncio.run(ledgers[6].compare_and_set("park/run-1", None, "late")) is False, (
        "create-if-absent must fail once the key exists"
    )
    assert asyncio.run(ledgers[7].state("never-claimed")) is None
    for clone in clones:
        left = subprocess.run(
            ["git", "for-each-ref", "refs/lockstep/"], cwd=clone, capture_output=True, text=True
        )
        assert left.stdout == "", f"a swap left a local ref behind: {left.stdout}"


def test_gate_out_4_a_local_git_ledger_still_refuses_compare_and_set(tmp_path: Path) -> None:
    """The default construction is the LOCAL store it always was, and its refusal names the
    constructor that would make it shared, rather than swapping on a ref one machine can see."""
    import asyncio

    from in_lockstep.platform.ledger import Unsupported

    ledger = GitLedger(root=_repo(tmp_path))
    assert ledger.scope == "local"
    with pytest.raises(Unsupported, match="shared=True"):
        asyncio.run(ledger.compare_and_set("k", None, "v"))
    with pytest.raises(Unsupported):
        asyncio.run(ledger.state("k"))


def test_a_run_context_carries_the_store_its_record_goes_to(tmp_path: Path) -> None:
    """`ctx.ledger` is the port, resolved once by the same decision the record writer makes, so
    what a parked branch would write through is where the run's own record lands."""
    from in_lockstep import Lockstep
    from in_lockstep.core.context import RunContext
    from in_lockstep.core.spend import Budget

    root = _repo(tmp_path)
    lockstep = Lockstep.detect(root)
    lockstep.budget = Budget(usd=1.0)
    ctx = lockstep.context("r1")
    assert isinstance(ctx.ledger, GitLedger) and ctx.ledger.scope == "local"
    assert RunContext.__dataclass_fields__["ledger"].default is None


def test_a_depth_one_publisher_absorbs_a_bundle_carrying_ids_the_remote_already_holds(tmp_path: Path) -> None:
    """#352's publish step. A runner's bundle carries the whole branch, the leaked fixture ids
    included, and the publishing job absorbs it from a depth-one checkout with no local
    `lockstep-history`: the refusal must ask the remote what it already holds, or every publish
    refuses its own bundle by ids a person has not yet removed -- which is what happened to each
    one after #346. A bundle carrying a fixture id the remote does NOT hold is still refused."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    seed = _repo(tmp_path / "seed")
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=seed, check=True, capture_output=True)
    asyncio.run(GitLedger(root=seed).append("triage-412", {"kind": "triage"}))  # the leak, as it happened
    subprocess.run(
        ["git", "push", "-q", "origin", f"refs/heads/{DEFAULT_BRANCH}:refs/heads/{DEFAULT_BRANCH}"],
        cwd=seed,
        check=True,
        capture_output=True,
    )

    # A runner at full depth: its branch starts from the remote's, so its bundle carries the leak.
    runner = _repo(tmp_path / "runner")
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=runner, check=True)
    subprocess.run(
        [
            "git",
            "fetch",
            "-q",
            "origin",
            f"+refs/heads/{DEFAULT_BRANCH}:refs/remotes/origin/{DEFAULT_BRANCH}",
        ],
        cwd=runner,
        check=True,
    )
    asyncio.run(GitLedger(root=runner).append("review-security-20260907T000000Z-ab12", {"kind": "review"}))
    bundle = GitLedger(root=runner).bundle(tmp_path / "runner.bundle")

    # The publisher: depth one, no lockstep-history ref of any kind.
    publisher = tmp_path / "publisher"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", str(origin), str(publisher)], check=True, capture_output=True
    )
    ledger = GitLedger(root=publisher)
    assert ledger.head() is None
    ledger.absorb(bundle, run_id="77")
    assert sorted(str(r["run_id"]) for r in ledger.records()) == [
        "review-security-20260907T000000Z-ab12",
        "triage-412",
    ]
    ledger.push()

    # A NEW fixture id, which the remote does not hold, is still refused at the same seam.
    rogue = _repo(tmp_path / "rogue")
    asyncio.run(GitLedger(root=rogue).append("wayfinder-chart-local", {"kind": "workflow"}))
    with pytest.raises(HistoryError, match="wayfinder-chart-local"):
        ledger.absorb(GitLedger(root=rogue).bundle(tmp_path / "rogue.bundle"), run_id="78")
