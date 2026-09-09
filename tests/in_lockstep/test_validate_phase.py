"""GATE-VALIDATE-2: the repository's own checks run over what a model wrote, before it is proposed.

`TDD` never asked the model to run its tests -- it staged them into a worktree, ran them itself
between phases, and handed the result back. Nothing did that for Validate, so a run's lint was
whatever the model chose to check with `run_validate` (#390) and CI was the first thing to see the
answer: run 34294139197 opened a pull request whose suite passed and whose lint failed on four
errors, and the pull request #389 opened did it again.

The tiers are the design, and each has its own failure: a deterministic fix that costs no turn, a
bounded repair turn for what remains, and findings that survive both travelling with the change
rather than being lost -- keeping it out of a reviewer's queue instead of failing the run.
"""

from __future__ import annotations

import ast
import asyncio
import subprocess
from pathlib import Path
from typing import Any

from in_lockstep.adapters.ai.strategy import Validation, validated
from in_lockstep.ai.builtins import Workspace
from in_lockstep.core.outcome import Cost, Outcome, Status
from in_lockstep.core.types import (
    Build,
    BuildResult,
    ChangeSet,
    FileChange,
    Test,
    Validate,
    ValidationFinding,
    ValidationReport,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "in_lockstep"


# -- doubles -------------------------------------------------------------------------------------


class _Validator:
    """A Validate adapter that records what it was asked, and can rewrite files like a fixer."""

    def __init__(
        self,
        *,
        findings: tuple[ValidationFinding, ...] = (),
        fixes: bool = False,
        takes_paths: bool = True,
    ) -> None:
        self.fixes = fixes
        self.takes_paths = takes_paths
        self.findings = findings
        self.seen: list[Validate] = []
        #: What a `fix=True` pass writes into the tree, path -> contents.
        self.repairs: dict[str, str] = {}
        #: Findings answered after a fix pass; the initial ones until this is set.
        self.after_fix: tuple[ValidationFinding, ...] | None = None

    async def invoke(self, ctx: Any, request: Validate) -> Outcome[ValidationReport]:
        self.seen.append(request)
        if request.fix:
            for path, contents in self.repairs.items():
                (Path(request.root) / path).write_text(contents)
            return Outcome(status=Status.SUCCEEDED, value=ValidationReport())
        findings = self.after_fix if (self.after_fix is not None and self.seen[0].fix) else self.findings
        report = ValidationReport(findings=findings or ())
        return Outcome(status=Status.SUCCEEDED if report.clean else Status.FAILED, value=report, cost=Cost())


class _Builder:
    """A Build adapter that records what it was asked and can fail on the first call only."""

    def __init__(self, *, fails: int = 0) -> None:
        from in_lockstep.adapters.sandbox import Sandbox
        from in_lockstep.core.verbs import Capability

        #: What `staged_refusal` reads: a runner that declares a container is one a model's staged
        #: code may be executed in, and an adapter that declares `EXECUTES_CODE` is one whose
        #: sandbox the rule checks (#410).
        self.capabilities: frozenset[Capability] = frozenset(
            {Capability.EXECUTES_CODE, Capability.READS_REPO}
        )
        self.sandbox: Any = Sandbox(image="declared-for-this-test", require_container=True)
        self.seen: list[Build] = []
        self.fails = fails

    async def invoke(self, ctx: Any, request: Build) -> Outcome[BuildResult]:
        self.seen.append(request)
        if len(self.seen) <= self.fails:
            from in_lockstep.core.outcome import Finding, Severity

            return Outcome(
                status=Status.FAILED,
                value=BuildResult(log="calc.py:3: undefined name `helper`"),
                findings=(
                    Finding(
                        id="build.command_failed",
                        message="calc.py:3: undefined name `helper`",
                        severity=Severity.ERROR,
                        blocking=True,
                    ),
                ),
            )
        return Outcome(status=Status.SUCCEEDED, value=BuildResult())


class _Ctx:
    """A container that answers for the verbs actually bound, and a `do` that dispatches by type."""

    def __init__(self, validator: _Validator | None = None, tester: Any = None, builder: Any = None) -> None:
        self.bound: dict[Any, Any] = {}
        if validator is not None:
            self.bound[Validate] = validator
        if builder is not None:
            self.bound[Build] = builder
        if tester is not None:
            self.bound[Test] = tester
        self.order: list[str] = []
        ctx = self

        class _Container:
            @staticmethod
            def has(verb: Any) -> bool:
                return verb in ctx.bound

            @staticmethod
            def resolve(verb: Any) -> Any:
                return ctx.bound[verb]

        self.container = _Container()
        self.repo = type("R", (), {"root": "."})()

    async def do(self, request: Any) -> Any:
        self.order.append(type(request).__name__)
        return await self.bound[type(request)].invoke(self, request)


class _Invocation:
    stalled = False
    truncated = False
    findings: tuple[Any, ...] = ()

    def __init__(self, turn_count: int = 1) -> None:
        self.turn_count = turn_count
        self.cost = Cost()
        self.content = "{}"
        self.idle_turns = 0
        self.exhausted = False


class _Invoker:
    """An invoker whose turn stages whatever `writes` says, the way a repair turn would."""

    def __init__(self, workspace: Workspace, writes: dict[str, str] | None = None) -> None:
        self.workspace = workspace
        self.writes = writes or {}
        self.systems: list[str] = []
        self.messages: list[Any] = []

    async def run(self, **kwargs: Any) -> _Invocation:
        self.systems.append(kwargs.get("system", ""))
        self.messages.append(kwargs.get("messages"))
        for path, contents in self.writes.items():
            self.workspace.record(path, contents)
        return _Invocation()


class _Refusing:
    """An invoker whose turn is refused by a control, the way a ceiling refuses one."""

    async def run(self, **kwargs: Any) -> Any:
        from in_lockstep.ai.invoker import InvocationBlocked

        raise InvocationBlocked("budget", "the next turn would exceed the ceiling")


class _Session:
    def __init__(self, root: Path, invoker: Any = None) -> None:
        self.repo_root = str(root)
        self.workspace = Workspace(root=root)
        self.invoker = invoker
        self.tools = None
        self.run_tool = None
        self.policy = type("P", (), {"max_turns": 4, "max_idle_turns": 2, "max_tokens": 512})()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=root, check=True)
    (root / "seed.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "init"], cwd=root, check=True
    )
    return root


def _messages() -> list[Any]:
    from dataclasses import dataclass

    @dataclass
    class _Message:
        role: str
        content: str

    return [_Message(role="user", content="do the work")]


def _staged(session: _Session, path: str = "a.py", contents: str = "import os\n") -> ChangeSet:
    session.workspace.record(path, contents)
    return session.workspace.changeset()


def _run(ctx: _Ctx, session: _Session, changeset: ChangeSet) -> Validation:
    return asyncio.run(
        validated(
            ctx,
            session,
            changeset,
            system="s",
            messages=_messages(),
            package=None,
            prefix="implement",
        )
    )


# -- tier one: what costs no turn ----------------------------------------------------------------


def test_gate_validate_2_what_the_validator_can_fix_costs_no_model_call(tmp_path: Path) -> None:
    """GATE-VALIDATE-2, the deterministic tier. Import sorting is arithmetic wearing a prompt, and
    a turn spent on it is a turn bought at model prices."""
    session = _Session(_repo(tmp_path), invoker=_Invoker(Workspace(root=tmp_path)))
    changeset = _staged(session)
    validator = _Validator(fixes=True, findings=(ValidationFinding(rule="I001", message="unsorted"),))
    validator.repairs = {"a.py": "import os  # sorted\n"}
    validator.after_fix = ()

    result = _run(_Ctx(validator), session, changeset)

    assert result.fixed == ("a.py",)
    assert result.clean, "the fixer's work is what the second pass sees"
    assert result.invocations == (), "no model call was made"
    (staged,) = session.workspace.changes
    assert staged.contents == "import os  # sorted\n", "the session holds the fixed bytes"


def test_the_fixed_bytes_are_what_a_later_turn_reads_and_what_travels(tmp_path: Path) -> None:
    """The fold-back goes through the workspace rather than a returned change set, because a repair
    turn reads the workspace and the strategy rebuilds from it. A fix folded anywhere else is a fix
    the next read does not see and the rebuild throws away."""
    session = _Session(_repo(tmp_path))
    session.invoker = _Invoker(session.workspace, writes={"b.py": "second\n"})
    changeset = _staged(session)
    validator = _Validator(fixes=True, findings=(ValidationFinding(rule="E1", message="bad"),))
    validator.repairs = {"a.py": "fixed\n"}

    _run(_Ctx(validator), session, changeset)

    held = {c.path: c.contents for c in session.workspace.changes}
    assert held["a.py"] == "fixed\n", "the deterministic fix survived the repair turn"
    assert held["b.py"] == "second\n", "and the repair turn's own write is there"


def test_a_validator_that_cannot_fix_is_never_asked_to(tmp_path: Path) -> None:
    """`fixes` is declared, not assumed: a repository's `make lint` may take no such flag, or one
    that means something else."""
    session = _Session(_repo(tmp_path), invoker=_Invoker(Workspace(root=tmp_path)))
    validator = _Validator(fixes=False, findings=())
    _run(_Ctx(validator), session, _staged(session))
    assert [r.fix for r in validator.seen] == [False]


def test_the_fixer_never_adds_a_path_to_the_change_set(tmp_path: Path) -> None:
    """A file a fixer created is not one this session is proposing."""
    workspace = Workspace(root=tmp_path)
    workspace.record("a.py", "one\n")
    assert workspace.restage("b.py", "invented\n") is False
    assert [c.path for c in workspace.changes] == ["a.py"]
    assert workspace.restage("a.py", "one\n") is False, "unchanged bytes are not a restage"
    assert workspace.restage("a.py", "two\n") is True


# -- tier two: the bounded repair ------------------------------------------------------------------


def test_gate_validate_2_what_remains_goes_back_named_with_the_rule_and_the_place(
    tmp_path: Path,
) -> None:
    """GATE-VALIDATE-2, the repair turn. `GATE-VERDICT-2`'s rule about a red suite: a verdict that
    says a check failed and not which one sends the session back to run it again to learn what the
    run already knew."""
    session = _Session(_repo(tmp_path))
    session.invoker = _Invoker(session.workspace, writes={"a.py": "repaired\n"})
    changeset = _staged(session)
    validator = _Validator(
        findings=(ValidationFinding(rule="F401", message="`os` imported but unused", path="a.py", line=1),)
    )

    result = _run(_Ctx(validator), session, changeset)

    assert len(result.invocations) == 1, "exactly one repair turn"
    sent = str(session.invoker.messages[0][-1].content)
    assert "a.py:1: F401 `os` imported but unused" in sent
    assert "Fix the finding, not the code that caused it" in sent
    assert "Change only what these findings name" in sent


def test_a_repair_that_cannot_be_made_carries_the_findings_rather_than_failing_the_run(
    tmp_path: Path,
) -> None:
    """A ceiling refusing the optional turn must not throw away a change that was already complete.
    The findings travel instead, and the propose half keeps the change out of a review queue."""
    session = _Session(_repo(tmp_path), invoker=_Refusing())
    changeset = _staged(session)
    validator = _Validator(findings=(ValidationFinding(rule="E1", message="bad", path="a.py"),))

    result = _run(_Ctx(validator), session, changeset)

    assert result.invocations == ()
    assert not result.clean and result.checked
    assert "the repair turn was not made" in result.unrepaired
    assert "budget" in result.unrepaired, "and it says which control refused it"


def test_findings_that_survive_the_repair_are_reported_as_unrepaired(tmp_path: Path) -> None:
    session = _Session(_repo(tmp_path))
    session.invoker = _Invoker(session.workspace, writes={"a.py": "still wrong\n"})
    validator = _Validator(findings=(ValidationFinding(rule="E1", message="bad", path="a.py"),))

    result = _run(_Ctx(validator), session, _staged(session))

    assert len(result.invocations) == 1, "one round, then it stops"
    assert not result.clean
    assert "did not settle it" in result.unrepaired
    ids = {f.id for f in result.findings()}
    assert "validate.e1" in ids and "validate.unrepaired" in ids


# -- what it is pointed at, and what it declines ---------------------------------------------------


def test_gate_validate_2_the_check_is_scoped_to_the_paths_the_run_staged(tmp_path: Path) -> None:
    """GATE-VALIDATE-2. A validator pointed at the whole tree reports the repository's existing
    debt, and a run that spends turns on code it never touched widens its own diff to answer for
    findings nobody asked it about."""
    session = _Session(_repo(tmp_path), invoker=_Invoker(Workspace(root=tmp_path)))
    session.workspace.record("a.py", "one\n")
    session.workspace.record("b.py", "two\n")
    session.workspace.record("gone.py", None)
    validator = _Validator()

    _run(_Ctx(validator), session, session.workspace.changeset())

    (asked,) = validator.seen
    assert asked.paths == ("a.py", "b.py"), "a deletion has nothing to check"
    assert asked.root and asked.root != str(session.repo_root), "and it ran over a materialised tree"


def test_gate_validate_2_with_no_validate_bound_nothing_is_invented(tmp_path: Path) -> None:
    """GATE-VALIDATE-2, O1's rule. Detection declines where a repository configured no linter, and
    a framework that supplied one anyway would judge a change by rules nobody agreed to."""
    session = _Session(_repo(tmp_path), invoker=_Invoker(Workspace(root=tmp_path)))
    result = _run(_Ctx(validator=None), session, _staged(session))
    assert result == Validation()
    assert not result.checked and not result.clean, "unchecked is not clean"


def test_a_validator_that_did_not_report_is_not_read_as_clean(tmp_path: Path) -> None:
    """Absent is not zero, the rule this repository applies to every number nobody measured."""

    class _Broken(_Validator):
        async def invoke(self, ctx: Any, request: Validate) -> Any:
            return Outcome(status=Status.ERRORED, reason="ruff is not installed")

    session = _Session(_repo(tmp_path), invoker=_Invoker(Workspace(root=tmp_path)))
    result = _run(_Ctx(_Broken()), session, _staged(session))
    assert not result.checked and not result.clean
    assert "the validator did not report: ruff is not installed" in result.unrepaired


def test_a_change_set_of_deletions_alone_is_checked_by_nothing(tmp_path: Path) -> None:
    session = _Session(_repo(tmp_path), invoker=_Invoker(Workspace(root=tmp_path)))
    session.workspace.record("seed.txt", None)
    validator = _Validator()
    result = _run(_Ctx(validator), session, session.workspace.changeset())
    assert validator.seen == [] and not result.checked


# -- what a reviewer is asked for -------------------------------------------------------------------


class _Tickets:
    def __init__(self) -> None:
        self.said: list[str] = []

    async def get(self, key: str) -> Any:
        from in_lockstep.platform.tickets import Ticket

        return Ticket(key=key, title="a title")

    async def comment(self, ticket: Any, body: str) -> None:
        self.said.append(body)


class _Scm:
    """Records what was opened and whether it was taken out of draft."""

    shared_numbering = True

    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []
        self.ready: list[Any] = []

    async def ticket_of(self, number: int) -> str | None:
        return None

    async def open_change(self, cs: Any, **kwargs: Any) -> Any:
        from in_lockstep.platform.scm.base import ChangeRequest

        self.opened.append(kwargs)
        return ChangeRequest(id="u", url="https://x/1", branch="b", title="t", number=1)

    async def mark_ready(self, change: Any) -> None:
        self.ready.append(change)


class _ProposeCtx:
    def __init__(self, root: Path) -> None:
        self.run_id = "r1"
        self.max_attempts = 3
        self.repo = type("R", (), {"root": str(root)})()


def _proposed(tmp_path: Path, validation: ValidationReport | None, name: str) -> tuple[_Scm, str]:
    """Run the real propose half over an artifact carrying a green suite and `validation`."""
    from in_lockstep.core.types import TestVerdict
    from in_lockstep.platform.artifacts import write_changeset
    from in_lockstep.workflows.implement import implement_propose

    artifact = str(tmp_path / name)
    write_changeset(
        artifact,
        ChangeSet(changes=(FileChange(path="a.py", contents="x\n"),), summary="s"),
        verdict=TestVerdict(status="succeeded", decided=True, total=2, passed=2),
        validation=validation,
    )
    scm, tickets = _Scm(), _Tickets()
    outcome = asyncio.run(
        implement_propose(
            _ProposeCtx(tmp_path),  # type: ignore[arg-type]
            "#1",
            tickets,  # type: ignore[arg-type]
            scm,  # type: ignore[arg-type]
            artifact=artifact,
        )
    )
    assert outcome.status is Status.SUCCEEDED
    return scm, tickets.said[0]


def test_gate_validate_2_an_unclean_change_is_opened_as_a_draft(tmp_path: Path) -> None:
    """GATE-VALIDATE-2, the consequence, through the half that decides it. A change whose checks
    failed is precisely one that should not be asking for a person's time -- and an UNCHECKED
    change is not blocked by this, because absent is not failing, the reading a missing test
    verdict already gets.

    Driven through `implement_propose` rather than by re-stating its condition here. The first
    version of this test recomputed `ready` itself and passed with the workflow's own term
    deleted, which is the vacuous control this repository replaced once already (#309).
    """
    findings = (ValidationFinding(rule="F401", message="unused", path="a.py", line=1),)

    unclean, said = _proposed(tmp_path, ValidationReport(findings=findings), "dirty")
    assert unclean.opened and unclean.ready == [], "a change with findings asked for review"
    assert "draft" in said

    clean, said = _proposed(tmp_path, ValidationReport(), "clean")
    assert clean.ready, "a green, clean change is ready for review"
    assert "ready for review" in said

    unchecked, _ = _proposed(tmp_path, None, "unchecked")
    assert unchecked.ready, "nothing checked it, which is not the same as failing"


def test_the_findings_reach_the_pull_request_body_a_reviewer_reads(tmp_path: Path) -> None:
    """A draft whose body does not say why is one a reviewer has to go hunting to understand."""
    findings = (ValidationFinding(rule="F401", message="`os` imported but unused", path="a.py", line=1),)
    scm, _ = _proposed(tmp_path, ValidationReport(findings=findings), "body")
    body = str(scm.opened[0]["body"])
    assert "1 unresolved finding(s)" in body
    assert "F401" in body and "`os` imported but unused" in body


def test_the_body_tells_a_reviewer_which_checks_ran(tmp_path: Path) -> None:
    from in_lockstep.platform.report import implement_body

    changes = ChangeSet(changes=(FileChange(path="a.py", contents="x\n"),), summary="s")
    unchecked = implement_body(changes, None, None)
    assert "not checked" in unchecked and "no Validate verb is bound" in unchecked
    assert "clean" in implement_body(changes, None, ValidationReport())
    dirty = implement_body(
        changes, None, ValidationReport(findings=(ValidationFinding(rule="F401", message="unused"),))
    )
    assert "1 unresolved finding(s)" in dirty and "F401" in dirty


# -- every strategy that reports a change also checks it ---------------------------------------------


def test_gate_validate_2_every_strategy_that_reports_a_change_validates_it_first() -> None:
    """GATE-VALIDATE-2, structurally. A new strategy is a new place to forget this, and the three
    that exist were each a place it had been forgotten. `reported(` is the marker: it is what a
    strategy calls to describe a staged change on its way out, so a module that calls it is a
    module that produced one."""
    missing = []
    for path in sorted((SRC / "adapters" / "ai").glob("*.py")):
        source = path.read_text()
        calls = {
            node.func.id
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        if "reported" in calls and "validated" not in calls:
            missing.append(path.name)
    assert not missing, f"these stage a change and never check it: {missing}"


# -- the build, which comes first ------------------------------------------------------------------


def test_gate_validate_2_the_build_runs_before_the_check(tmp_path: Path) -> None:
    """GATE-VALIDATE-2. A change that does not compile is the more important failure, and it makes
    the rest secondary: findings about a tree that does not build answer a question nobody has got
    to yet."""
    session = _Session(_repo(tmp_path), invoker=_Invoker(Workspace(root=tmp_path)))
    validator, builder = _Validator(), _Builder()
    ctx = _Ctx(validator, builder=builder)

    _run(ctx, session, _staged(session))

    assert ctx.order == ["Build", "Validate"], "the check waited for the build"


def test_gate_validate_2_a_build_failure_is_what_the_repair_turn_is_given(tmp_path: Path) -> None:
    """GATE-VALIDATE-2, one budget spent on the first thing that failed. Two budgets would be two
    turns, and O13 asks for a bound declared before the spend rather than one per gate."""
    session = _Session(_repo(tmp_path))
    session.invoker = _Invoker(session.workspace, writes={"a.py": "repaired\n"})
    validator, builder = _Validator(), _Builder(fails=1)
    ctx = _Ctx(validator, builder=builder)

    result = _run(ctx, session, _staged(session))

    sent = str(session.invoker.messages[0][-1].content)
    assert "undefined name `helper`" in sent, "the build's own words went back"
    assert "Fix the error rather than removing the code that raised it" in sent
    assert ctx.order == ["Build", "Build", "Validate"], "nothing was checked until it built"
    assert result.build == "", "and once it built, there is nothing to report about the build"
    assert len(result.invocations) == 1


def test_a_build_that_still_fails_leaves_the_check_unrun_and_says_so(tmp_path: Path) -> None:
    """The honest end state: the change does not build, so nothing checked it. Its suite will fail
    too, which is what keeps it out of a review queue -- this is the part that says why."""
    session = _Session(_repo(tmp_path))
    session.invoker = _Invoker(session.workspace, writes={"a.py": "still broken\n"})
    ctx = _Ctx(_Validator(), builder=_Builder(fails=9))

    result = _run(ctx, session, _staged(session))

    assert "Validate" not in ctx.order
    assert "undefined name `helper`" in result.build
    assert not result.checked, "unchecked, and not clean"
    assert "validate.build" in {f.id for f in result.findings()}


def test_gate_validate_2_a_build_that_cannot_be_contained_is_not_run_on_this_host(
    tmp_path: Path,
) -> None:
    """GATE-VALIDATE-2 meets GATE-SANDBOX-2. A build runs the repository's own build scripts over
    model-authored source -- a setup.py, a build.rs, a postinstall -- which is the exposure the
    container rule is about with a different file extension. The question is asked of the verb
    being run rather than of `Test` by name."""
    from in_lockstep.adapters.sandbox import Sandbox

    builder = _Builder()
    builder.sandbox = Sandbox()  # no image, no container: the fallback the rule refuses
    session = _Session(_repo(tmp_path), invoker=_Invoker(Workspace(root=tmp_path)))
    ctx = _Ctx(_Validator(), builder=builder)

    result = _run(ctx, session, _staged(session))

    assert builder.seen == [], "nothing was built on this host"
    assert "the change was not built" in result.build and "container" in result.build
    assert ctx.order == ["Validate"], "and the check still ran: it reads the tree, it does not run it"


def test_the_build_is_pointed_at_the_materialised_tree(tmp_path: Path) -> None:
    """`Build.root`, the field `Test` had and `Validate` gained in #390. Without it the build runs
    over the repository as it is on disk, which is the code the model did not write."""
    session = _Session(_repo(tmp_path), invoker=_Invoker(Workspace(root=tmp_path)))
    builder = _Builder()
    _run(_Ctx(_Validator(), builder=builder), session, _staged(session))
    (asked,) = builder.seen
    assert asked.root and asked.root != session.repo_root


# -- what the repository declares, over what a tool could be guessed -------------------------------


def test_gate_validate_2_a_target_that_takes_no_paths_checks_the_whole_tree(tmp_path: Path) -> None:
    """GATE-VALIDATE-2, the cost of preferring the repository's own target. `make lint src/a.py`
    reads the path as a TARGET and fails, so a target-bound validator is not given paths -- it
    checks the whole tree, which is what such a target is: a gate the repository keeps green."""
    session = _Session(_repo(tmp_path), invoker=_Invoker(Workspace(root=tmp_path)))
    validator = _Validator(takes_paths=False)
    _run(_Ctx(validator), session, _staged(session))
    (asked,) = validator.seen
    assert asked.paths == (), "a target was handed no paths"
    assert asked.root, "but still pointed at the staged tree"


def test_a_command_validator_reports_its_failure_in_the_report_and_not_only_beside_it(
    tmp_path: Path,
) -> None:
    """An empty `ValidationReport` is a CLEAN one. A failed `make lint` used to return one, so a
    caller reading the report -- the check step, and the pull-request body -- was told a failing
    gate had found nothing."""
    from in_lockstep.adapters.command import CommandValidate

    class _Sandbox:
        def __init__(self) -> None:
            self.ran: list[list[str]] = []

        async def run(self, command: list[str], *, cwd: str | None = None, timeout: float = 900.0) -> Any:
            self.ran.append(command)
            failed = command[-1] == "lint"
            return type(
                "R",
                (),
                {"exit_code": 1 if failed else 0, "stdout": "E501 line too long", "stderr": "", "how": "f"},
            )()

    sandbox = _Sandbox()
    adapter = CommandValidate(["make", "lint"], fix=["make", "fmt"], sandbox=sandbox)
    ctx = type("C", (), {"repo": type("R", (), {"root": str(tmp_path)})()})()

    outcome = asyncio.run(adapter.invoke(ctx, Validate(fix=True)))

    assert [c[-1] for c in sandbox.ran] == ["fmt", "lint"], "repair first, then report what stands"
    assert outcome.value is not None and not outcome.value.clean
    assert "E501 line too long" in outcome.value.findings[0].message
    assert adapter.fixes and not adapter.takes_paths
