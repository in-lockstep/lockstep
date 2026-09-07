"""The records a read-only job could only bundle come home, once each (GATE-LEDGER-10).

Every pull request pays for a review whose job holds the provider credential and `contents: read`,
so its record leaves as a bundle in an artifact on a 30-day clock — and for two days of pull
requests that is where every one stayed, while the branch `report` reads held eight paid runs
(#294). The sweep here is the deterministic half of the fix: which artifacts are outstanding, by
the run id a record and an artifact both carry, and taking each in exactly once.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from in_lockstep.cli import main
from in_lockstep.platform.ledger import GitLedger, HistoryError
from in_lockstep.platform.ledger.reconcile import RunArtifact, absorb_outstanding, outstanding


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run(args, cwd=path, capture_output=True, check=True)

    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.test")
    run("git", "config", "user.name", "t")
    (path / "app.py").write_text("x = 1\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "base")
    return path


def _bundle_from_a_runner(tmp_path: Path, *, ci_run: str) -> Path:
    """What a review job leaves in its artifact: a bundle of the one record it wrote."""
    recorder = _repo(tmp_path / "runner")
    ledger = GitLedger(root=recorder)
    asyncio.run(
        ledger.append(f"review-security-{ci_run}", {"kind": "review", "cost_usd": 0.19, "ci_run": ci_run})
    )
    return ledger.bundle(tmp_path / f"bundle-{ci_run}" / "history.bundle")


class _Forge:
    """A host that lists three artifacts: one with a bundle, one expired, one that carried none."""

    def __init__(self, bundle: Path) -> None:
        self.bundle = bundle
        self.downloads: list[int] = []

    def run_artifacts(self, name: str) -> tuple[RunArtifact, ...]:
        assert name == "lockstep-run"
        return (RunArtifact(1, "77"), RunArtifact(2, "78", expired=True), RunArtifact(3, "79"))

    def download_artifact(self, artifact_id: int, into: Path) -> Path:
        self.downloads.append(artifact_id)
        if artifact_id == 1:
            shutil.copy(self.bundle, into / "history.bundle")
        return into


# -- which artifacts are outstanding --------------------------------------------------------


def test_gate_ledger_10_an_artifact_is_outstanding_until_a_record_or_an_absorb_commit_names_its_run() -> None:
    """The join, and nothing else: no bundle is downloaded to answer this, which is what lets
    `report` print the number without a fetch per artifact."""
    artifacts = [
        RunArtifact(1, "1"),
        RunArtifact(2, "2"),
        RunArtifact(3, "3", expired=True),
        RunArtifact(4, "4", expired=True),
        RunArtifact(5, ""),
    ]
    found = outstanding(artifacts, [{"ci_run": "1"}, {"kind": "review"}], absorbed={"3"})
    assert [a.id for a in found.pending] == [2, 5], (
        "an artifact with no run id cannot be matched, so it is pending"
    )
    assert [a.id for a in found.expired] == [4], "expired and unabsorbed: those records are gone, and counted"
    assert found.absorbed == 2


def test_an_empty_run_id_on_a_record_matches_nothing() -> None:
    """`ci_run` is absent outside CI. An empty string must not match an artifact with no run id,
    or every local record would read as having absorbed every unmatchable artifact."""
    found = outstanding([RunArtifact(5, "")], [{"ci_run": ""}, {}])
    assert [a.id for a in found.pending] == [5] and found.absorbed == 0


# -- absorbing each one once -----------------------------------------------------------------


def test_gate_ledger_10_the_sweep_absorbs_each_outstanding_artifact_once(tmp_path: Path) -> None:
    """The whole sweep against a stubbed host and two real git ledgers. The bundled record reaches
    the publisher's branch; the expired artifact is counted and not fetched; the artifact that
    carried no bundle is noted so tomorrow's sweep does not fetch it again; and a second sweep
    finds nothing to do."""
    bundle = _bundle_from_a_runner(tmp_path, ci_run="77")
    publisher = GitLedger(root=_repo(tmp_path / "publisher"))
    forge = _Forge(bundle)

    swept = absorb_outstanding(publisher, forge)
    assert [a.id for a in swept.taken] == [1]
    assert [a.id for a in swept.empty] == [3]
    assert [a.id for a in swept.expired] == [2]
    assert swept.failed == ()
    assert forge.downloads == [1, 3], "the expired artifact was never fetched"
    assert [r["run_id"] for r in publisher.records()] == ["review-security-77"]
    assert publisher.absorbed_runs() == {"77", "79"}, "both the bundle and the empty artifact are noted"

    again = absorb_outstanding(publisher, forge)
    assert again.taken == () and again.empty == (), "once means once"
    assert forge.downloads == [1, 3], "nothing was fetched the second time"
    assert [a.id for a in again.expired] == [2], "still gone, still said"


def test_gate_ledger_12_a_fresh_checkout_sweeps_once_too(tmp_path: Path) -> None:
    """The sweep on the machine that runs it: a checkout with only the remote-tracking ref. For as
    long as the ledger read the local ref alone, every artifact was outstanding every night and
    yesterday's absorb notes were never read (#307). The first sweep reads the remote copy and
    creates the local branch with its absorbs; the second, on another fresh checkout after a
    push, downloads nothing."""
    bundle = _bundle_from_a_runner(tmp_path, ci_run="77")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    seed = GitLedger(root=_repo(tmp_path / "seed"))
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=seed.root, check=True)
    subprocess.run(
        ["git", "push", "-q", "origin", "HEAD:main"], cwd=seed.root, check=True, capture_output=True
    )
    asyncio.run(seed.append("local-1", {"kind": "review"}))
    seed.push()

    def checkout(name: str) -> GitLedger:
        root = tmp_path / name
        subprocess.run(["git", "clone", "-q", str(origin), str(root)], check=True, capture_output=True)
        return GitLedger(root=root)

    forge = _Forge(bundle)
    first = checkout("runner-1")
    assert first.resolved() == ("refs/remotes/origin/lockstep-history", first.head())
    swept = absorb_outstanding(first, forge)
    assert [a.id for a in swept.taken] == [1] and [a.id for a in swept.empty] == [3]
    assert sorted(str(r["run_id"]) for r in first.records()) == ["local-1", "review-security-77"], (
        "the seed's record survived the absorb"
    )
    first.push()

    again = absorb_outstanding(checkout("runner-2"), forge)
    assert again.taken == () and again.empty == (), "once means once, across checkouts"
    assert forge.downloads == [1, 3], "the second checkout fetched nothing"


def test_an_absorb_into_a_branch_that_already_exists_names_the_run_in_its_commit(tmp_path: Path) -> None:
    """The second half of once. A bundle merged into an existing branch leaves a merge commit, and
    the run it came from is in that commit's subject rather than only in the records it carried —
    so a bundle carrying records older than schema 8, which name no run, is still absorbed once."""
    bundle = _bundle_from_a_runner(tmp_path, ci_run="55")
    publisher = GitLedger(root=_repo(tmp_path / "publisher"))
    asyncio.run(publisher.append("local-1", {"kind": "review"}))
    publisher.absorb(bundle, run_id="55")
    assert publisher.absorbed_runs() == {"55"}
    assert sorted(str(r["run_id"]) for r in publisher.records()) == ["local-1", "review-security-55"]
    assert publisher.verify() == [], "an absorb never rewrites a record"


def test_gate_ledger_10_two_bundles_from_two_runners_are_both_absorbed_and_no_scratch_ref_remains(
    tmp_path: Path,
) -> None:
    """The first dispatched sweep absorbed one bundle and reported 219 failures: each bundle is a
    different runner's orphan history, and a plain fetch into the scratch ref the first absorb
    left behind is refused non-fast-forward (#323). Two unrelated bundles, both taken, nothing
    left under `refs/lockstep/`."""
    first = _bundle_from_a_runner(tmp_path, ci_run="77")
    second = _bundle_from_a_runner(tmp_path / "other", ci_run="78")
    publisher = GitLedger(root=_repo(tmp_path / "publisher"))
    asyncio.run(publisher.append("local-1", {"kind": "review"}))

    class _Two(_Forge):
        def run_artifacts(self, name: str) -> tuple[RunArtifact, ...]:
            return (RunArtifact(1, "77"), RunArtifact(2, "78"))

        def download_artifact(self, artifact_id: int, into: Path) -> Path:
            shutil.copy(first if artifact_id == 1 else second, into / "history.bundle")
            return into

    swept = absorb_outstanding(publisher, _Two(first))
    assert swept.failed == (), swept.failed
    assert [a.id for a in swept.taken] == [1, 2]
    assert publisher.absorbed_runs() == {"77", "78"}
    assert sorted(str(r["run_id"]) for r in publisher.records()) == [
        "local-1",
        "review-security-77",
        "review-security-78",
    ]
    assert publisher.verify() == []
    left = subprocess.run(
        ["git", "for-each-ref", "refs/lockstep/"], cwd=publisher.root, capture_output=True, text=True
    ).stdout
    assert left == "", f"a scratch ref outlived its absorb: {left}"


def test_gate_ledger_10_a_scratch_ref_left_by_an_older_version_does_not_refuse_the_next_absorb(
    tmp_path: Path,
) -> None:
    """A clone that absorbed before #323 still carries `refs/lockstep/incoming`. Deleting the ref
    after each fold does not help that clone's first absorb; forcing the fetch does."""
    bundle = _bundle_from_a_runner(tmp_path, ci_run="77")
    publisher = GitLedger(root=_repo(tmp_path / "publisher"))
    asyncio.run(publisher.append("local-1", {"kind": "review"}))
    subprocess.run(["git", "update-ref", "refs/lockstep/incoming", "HEAD"], cwd=publisher.root, check=True)
    publisher.absorb(bundle, run_id="77")
    assert sorted(str(r["run_id"]) for r in publisher.records()) == ["local-1", "review-security-77"]


def test_one_failing_artifact_does_not_hold_the_rest_behind_it(tmp_path: Path) -> None:
    bundle = _bundle_from_a_runner(tmp_path, ci_run="77")
    publisher = GitLedger(root=_repo(tmp_path / "publisher"))

    class _Flaky(_Forge):
        def download_artifact(self, artifact_id: int, into: Path) -> Path:
            if artifact_id == 3:
                raise RuntimeError("gh: HTTP 500")
            return super().download_artifact(artifact_id, into)

    swept = absorb_outstanding(publisher, _Flaky(bundle))
    assert [a.id for a in swept.taken] == [1]
    assert [(a.id, why) for a, why in swept.failed] == [(3, "gh: HTTP 500")]


def test_a_host_that_cannot_list_artifacts_is_refused_by_name(tmp_path: Path) -> None:
    """Not "nothing outstanding". A local git host has no artifacts, and reading its silence as
    zero is the reassuring figure the ledger exists to refuse."""
    publisher = GitLedger(root=_repo(tmp_path / "publisher"))
    with pytest.raises(HistoryError, match="cannot list and download run artifacts"):
        absorb_outstanding(publisher, object())


# -- the command --------------------------------------------------------------------------------


def test_gate_ledger_10_history_from_artifacts_reports_what_it_took_and_what_was_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import in_lockstep.platform.hosted as hosted

    bundle = _bundle_from_a_runner(tmp_path, ci_run="77")
    root = _repo(tmp_path / "publisher")
    (root / ".lockstep").mkdir()
    (root / ".lockstep" / "lockstep.py").write_text(
        "from in_lockstep import Lockstep\nlockstep = Lockstep.detect()\n"
    )
    for var in [v for v in os.environ if v.startswith("GITHUB_")] + ["GITLAB_CI"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(root)
    forge = _Forge(bundle)
    monkeypatch.setattr(hosted, "hosted_scm", lambda *a, **k: forge)

    result = CliRunner().invoke(main, ["history", "--from-artifacts", "lockstep-run"])
    assert result.exit_code == 0, result.output
    assert "absorbed  artifact 1  (run 77)" in result.output
    assert "empty     artifact 3  (run 79) carried no bundle" in result.output
    assert "expired   1 artifact(s) expired before anyone absorbed them" in result.output
    assert "artifacts 1 absorbed, 1 empty, 0 failed, 1 expired" in result.output
    assert "review-security-77" in result.output, (
        "the listing after the sweep shows the record it brought home"
    )

    again = CliRunner().invoke(main, ["history", "--from-artifacts", "lockstep-run"])
    assert "artifacts 0 absorbed, 0 empty, 0 failed, 1 expired" in again.output, again.output


def test_history_from_artifacts_refuses_a_host_that_cannot_list_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import in_lockstep.platform.hosted as hosted

    root = _repo(tmp_path / "publisher")
    (root / ".lockstep").mkdir()
    (root / ".lockstep" / "lockstep.py").write_text(
        "from in_lockstep import Lockstep\nlockstep = Lockstep.detect()\n"
    )
    for var in [v for v in os.environ if v.startswith("GITHUB_")] + ["GITLAB_CI"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(root)
    monkeypatch.setattr(hosted, "hosted_scm", lambda *a, **k: object())

    result = CliRunner().invoke(main, ["history", "--from-artifacts", "lockstep-run"])
    assert result.exit_code != 0
    assert "cannot list and download run artifacts" in result.output


# -- the GitHub host, against the shape `gh api` returns ----------------------------------------


def test_github_lists_artifacts_by_name_expired_ones_included() -> None:
    from in_lockstep.platform.scm import GitHubScm

    calls: list[tuple[str, ...]] = []
    listing = (
        '{"id":5,"expired":false,"created_at":"2026-09-06T00:00:00Z","run_id":99}\n'
        '{"id":6,"expired":true,"created_at":"2026-08-01T00:00:00Z","run_id":98}\n'
        "\n"
    )

    def fake_gh(*args: str) -> tuple[int, str, str]:
        calls.append(args)
        return 0, listing, ""

    scm = GitHubScm(".")
    scm._gh = fake_gh  # type: ignore[method-assign]
    found = scm.run_artifacts("lockstep-run")
    assert found == (
        RunArtifact(5, "99", expired=False, created_at="2026-09-06T00:00:00Z"),
        RunArtifact(6, "98", expired=True, created_at="2026-08-01T00:00:00Z"),
    )
    (args,) = calls
    assert "--paginate" in args, "fifty runs in two days is more than one page"
    assert any("name=lockstep-run" in a for a in args)


def test_github_says_why_when_it_cannot_list_artifacts() -> None:
    from in_lockstep.platform.scm import GitHubScm

    scm = GitHubScm(".")
    scm._gh = lambda *a: (1, "", "gh: not logged in")  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="not logged in"):
        scm.run_artifacts("lockstep-run")
