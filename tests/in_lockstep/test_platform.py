"""Phase-4 gates: SCM, tickets, ledger, CI detection."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

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
from in_lockstep.platform.scm import DirectPushRefused, GitLocal, branch_for
from in_lockstep.platform.scm.base import GuardRefused
from in_lockstep.platform.tickets import TicketState, criteria_from
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
    aio.run(lockstep.context(run_id="m").do(_Iface, None))

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
