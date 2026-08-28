"""Phase-4 gates: SCM, tickets, ledger, CI detection."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from in_lockstep.core.types import ChangeAuthor, ChangeSet, FileChange
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
from in_lockstep.platform.scm import DirectPushRefused, GitLocal, branch_for
from in_lockstep.platform.scm.base import GuardRefused
from in_lockstep.platform.tickets import TicketState, criteria_from
from in_lockstep.platform.tickets.base import Ticket


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
    assert cr.branch == "in-lockstep/implement/r1"
    assert (root / "src" / "new.py").read_text() == "x = 1\n"
    log = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=root, capture_output=True, text=True).stdout
    # Trailers are the most portable traceability layer: greppable forever.
    assert "Ticket: P-1" in log
    assert "In-Lockstep-Run: r1" in log


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
    ledger = InRepoLedger(root=tmp_path / "ledger")
    asyncio.run(ledger.append("r1", {"kind": "review", "tokens": 100}))
    record = json.loads((tmp_path / "ledger" / "r1.json").read_text())
    assert record["schema"] == 2
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
