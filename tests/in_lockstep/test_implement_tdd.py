"""`TDD` — the strategy that enforces red→green instead of asking for it.

The loop is driven with a scripted model (no cassette — the LLM seam is the only thing worth
faking) but a real git repo and a real pytest run, because the whole point of the strategy is the
deterministic Test verb standing between the two model steps: a mocked Test would test nothing.

Four things matter: the happy path goes red then green; a strategy with no Test bound refuses
rather than degrading to an untested oneshot; a test that does not fail is caught (`tdd.not_red`);
and an implementation that leaves it failing is caught (`tdd.not_green`).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from in_lockstep.adapters.ai.implement import Implement
from in_lockstep.adapters.ai.tdd import TDD
from in_lockstep.adapters.pytest_adapter import PytestTest
from in_lockstep.adapters.sandbox import Runner, Sandbox
from in_lockstep.ai.invoker import AiInvoker, InvokePolicy
from in_lockstep.ai.pricing import CostTable, Rate
from in_lockstep.core.outcome import Outcome, Status
from in_lockstep.core.spend import Budget, Spend
from in_lockstep.core.types import Assess, AssessReport, CriterionVerdict, Test, Validate
from in_lockstep.llm.interface import LLMProvider
from in_lockstep.llm.types import LLMInput, LLMOutput, TokenUsage, ToolCall
from in_lockstep.platform.tickets import Ticket
from in_lockstep.privileged.egress import UnsandboxedEgress

MODEL = "test-model"


class Scripted(LLMProvider):
    """Replies in order; the last repeats so a turn cap is reachable without scripting forty."""

    def __init__(self, replies: list[LLMOutput]) -> None:
        self.replies = list(replies)
        self.calls: list[LLMInput] = []

    def name(self) -> str:
        return "scripted"

    async def generate(self, input: LLMInput) -> LLMOutput:
        self.calls.append(input)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        reply.usage = TokenUsage(input_tokens=100, output_tokens=20)
        return reply


def _table() -> CostTable:
    table = CostTable()
    table.add(MODEL, Rate(input_per_m=1.0, output_per_m=2.0))
    return table


def _invoker(provider: LLMProvider, *, spend: Spend | None = None) -> AiInvoker:
    return AiInvoker(
        provider,
        model=MODEL,
        cost_table=_table(),
        spend=spend or Spend(budget=Budget(usd=5.0)),
        egress=UnsandboxedEgress(),
    )


def _adapter(provider: LLMProvider, root: Path) -> TDD:
    return TDD(
        lambda ctx: _invoker(provider, spend=getattr(ctx, "spend", None)),
        repo_root=str(root),
        policy=InvokePolicy(max_turns=8, max_tokens=1024),
    )


class Declared(Sandbox):
    """Declares a container and runs on the host: the second scripted seam beside the model.

    The strategy refuses a Test runner that would put a MODEL-staged file on this host
    (GATE-SANDBOX-2), and it decides that from what the runner declares -- an image and
    `require_container` -- because a runner so constructed either runs in a container or refuses
    at run time. These tests need the pytest run to be REAL, and a container would need an
    image that carries pytest; so this one declares what a contained runner declares and runs
    the credential-dropped subprocess, which is exactly what `Sandbox()` did before. `runtime()`
    is None so `tooling.interpreter` resolves the host's Python rather than the bare name a
    container would get.
    """

    def __init__(self) -> None:
        super().__init__(image="declared-for-this-test", require_container=True)

    def runtime(self) -> str | None:
        return None

    async def run(self, command: list[str], *, cwd: str | None = None, timeout: float = 900.0) -> Any:
        return await self._subprocess(command, cwd=cwd, timeout=timeout)


class Ctx:
    """A ctx whose Test verb is a real PytestTest, so the red/green runs are real."""

    def __init__(
        self,
        *,
        test_bound: bool = True,
        sandbox: Runner | None = None,
        validator: Any = None,
        assessor: Any = None,
    ) -> None:
        self.spend = Spend(budget=Budget(usd=5.0))
        self.run_id = "t"
        self.adapter = PytestTest(args=["-q"], sandbox=sandbox or Declared())
        self.validator = validator
        #: The acceptance-criteria assessor, when a test binds one. None is the ordinary case and
        #: the one every test above this feature exercises: no Assess bound, so TDD's third phase
        #: is skipped entirely and the run is exactly the two-phase one it always was.
        self.assessor = assessor
        #: Which verbs were dispatched, in order — what GATE-VALIDATE-2's ordering claim reads.
        self.order: list[str] = []

        class _Container:
            def has(_self, verb: object) -> bool:
                # Test, and Validate only when one was handed in. It used to answer True to every
                # verb, which was harmless only while nothing asked for a second one: the moment a
                # strategy asked whether a Validate was bound, this fixture said yes and handed
                # back a pytest runner.
                if verb is Validate:
                    return validator is not None
                if verb is Assess:
                    return assessor is not None
                return verb is Test and test_bound

            def resolve(_self, verb: object) -> Any:
                if verb is Validate:
                    return self.validator
                return self.assessor if verb is Assess else self.adapter

        self.container = _Container()

    async def do(self, request: Any) -> Outcome[Any]:
        self.order.append(type(request).__name__)
        if isinstance(request, Validate):
            checked: Outcome[Any] = await self.validator.invoke(self, request)
            return checked
        if isinstance(request, Assess):
            assessed: Outcome[Any] = await self.assessor.invoke(self, request)
            return assessed
        return await self.adapter.invoke(self, request)


def _ticket() -> Ticket:
    return Ticket(key="#7", title="Add add()", description="calc.add(a, b) returns a + b.")


def _call(name: str, **args: Any) -> LLMOutput:
    return LLMOutput(content="", tool_calls=[ToolCall(id="1", name=name, input=args)])


def _done(summary: str = "did it") -> LLMOutput:
    return LLMOutput(content=json.dumps({"summary": summary, "notes": [], "unfinished": []}))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()

    def run(*args: str) -> None:
        subprocess.run(args, cwd=root, capture_output=True, check=True)

    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.test")
    run("git", "config", "user.name", "t")
    (root / "README.md").write_text("# calc\n")  # HEAD has no calc module yet
    run("git", "add", "-A")
    run("git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "base")
    run("git", "branch", "-M", "main")
    return root


_FAILING_TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def _run(
    provider: Scripted,
    repo: Path,
    *,
    test_bound: bool = True,
    sandbox: Runner | None = None,
    ctx: Ctx | None = None,
    ticket: Ticket | None = None,
) -> Outcome[Any]:
    return asyncio.run(
        _adapter(provider, repo).invoke(
            ctx or Ctx(test_bound=test_bound, sandbox=sandbox), Implement(ticket=ticket or _ticket())
        )
    )


def test_tdd_writes_a_failing_test_then_makes_it_green(repo: Path) -> None:
    provider = Scripted(
        [
            _call("write_file", path="test_calc.py", contents=_FAILING_TEST),
            _done("staged the failing test"),
            _call("write_file", path="calc.py", contents="def add(a, b):\n    return a + b\n"),
            _done("implemented add()"),
        ]
    )
    outcome = _run(provider, repo)
    assert outcome.status is Status.SUCCEEDED, outcome.findings
    assert outcome.value is not None
    paths = set(outcome.value.changeset.paths())
    assert paths == {"test_calc.py", "calc.py"}
    assert outcome.value.strategy == "implement/tdd"
    # Revert-and-verify confirmed the implementation is load-bearing: undo it and the suite is red.
    assert any(f.id == "tdd.fix_verified" for f in outcome.findings)


def test_tdd_flags_a_fix_that_is_not_load_bearing(repo: Path) -> None:
    """If phase 2 weakens the test into passing on its own, red→green still goes green — but
    reverting the implementation leaves it green too, and revert-and-verify catches that."""
    provider = Scripted(
        [
            _call("write_file", path="test_calc.py", contents=_FAILING_TEST),  # red: needs calc.add
            _done("staged the failing test"),
            # Phase 2 rewrites the test to pass trivially, then adds an unrelated implementation.
            _call("write_file", path="test_calc.py", contents="def test_trivial():\n    assert True\n"),
            _call("write_file", path="calc.py", contents="def add(a, b):\n    return a + b\n"),
            _done("weakened the test"),
        ]
    )
    outcome = _run(provider, repo)
    # The (weakened) test passes with the implementation, so the run still succeeds...
    assert outcome.status is Status.SUCCEEDED, outcome.findings
    # ...but revert-and-verify shows the implementation was not what made it pass.
    assert any(f.id == "tdd.fix_not_load_bearing" for f in outcome.findings)


def test_tdd_refuses_when_no_test_verb_is_bound(repo: Path) -> None:
    """Red→green is meaningless without a way to run the tests, so it refuses rather than degrade."""
    outcome = _run(Scripted([_done()]), repo, test_bound=False)
    assert outcome.status is Status.BLOCKED
    assert outcome.reason == "tdd.no_test"


def test_gate_sandbox_2_a_test_runner_that_would_use_the_host_is_refused_before_the_model_is_asked(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GATE-SANDBOX-2. `Sandbox()` with no image is the runner `init` scaffolded and this repository
    bound, and it ran a test file the model wrote as a subprocess on the host with `HOME` and an
    open socket (#308). TDD asks before its first model call: nothing spent, nothing materialised."""
    from in_lockstep.adapters import worktree

    async def never(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a worktree was materialised for a run that should have been refused")

    monkeypatch.setattr(worktree, "materialize", never)
    provider = Scripted([_call("write_file", path="test_calc.py", contents=_FAILING_TEST), _done()])
    outcome = _run(provider, repo, sandbox=Sandbox())
    assert outcome.status is Status.BLOCKED
    assert outcome.reason == "sandbox.host_fallback"
    assert provider.calls == [], (
        "the red phase was asked for a test that would then have been refused a runner"
    )
    message = outcome.findings[0].message
    assert "names no container image" in message
    assert 'Sandbox(image="..."' in message, "the refusal must name the line that would make it a yes"


def test_gate_sandbox_2_an_image_with_no_runtime_and_no_requirement_is_the_same_refusal(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one real probe: an image WITHOUT `require_container` falls back to a subprocess when
    no runtime is on PATH, which is the hole, so it is refused before the fallback can happen."""
    monkeypatch.setattr(Sandbox, "runtime", lambda self: None)
    outcome = _run(Scripted([_done()]), repo, sandbox=Sandbox(image="ghcr.io/x/ci:1"))
    assert outcome.status is Status.BLOCKED and outcome.reason == "sandbox.host_fallback"
    assert "fall back to a subprocess" in outcome.findings[0].message


def test_gate_progress_1_a_red_phase_that_stops_moving_is_blocked_with_its_test_intact(repo: Path) -> None:
    """GATE-PROGRESS-1 through TDD: the red phase stages its test and then reads until the idle
    ceiling; `implement.no_progress`, the test in the outcome, and no green phase asked for."""
    provider = Scripted(
        [
            _call("write_file", path="test_calc.py", contents=_FAILING_TEST),
            _call("read_file", path="README.md"),
        ]
    )
    adapter = TDD(
        lambda ctx: _invoker(provider, spend=getattr(ctx, "spend", None)),
        repo_root=str(repo),
        policy=InvokePolicy(max_turns=12, max_tokens=1024, max_idle_turns=2),
    )
    outcome = asyncio.run(adapter.invoke(Ctx(), Implement(ticket=_ticket())))
    assert outcome.status is Status.BLOCKED and outcome.reason == "implement.no_progress"
    assert outcome.value is not None and outcome.value.changeset.paths() == ("test_calc.py",)
    assert len(provider.calls) == 3


def test_tdd_fails_when_the_staged_test_does_not_go_red(repo: Path) -> None:
    """A test that passes before anything is written has specified nothing to implement."""
    provider = Scripted(
        [
            _call("write_file", path="test_trivial.py", contents="def test_trivial():\n    assert True\n"),
            _done("staged a test"),
        ]
    )
    outcome = _run(provider, repo)
    assert outcome.status is Status.FAILED
    assert outcome.reason == "tdd.not_red"
    assert "did not fail against the current code" in outcome.findings[0].message


@pytest.fixture
def repo_with_conventions(repo: Path) -> Path:
    """The shape that cost $52 across two runs: a repository whose pytest configuration decides
    which names are collected, and an existing suite so the run is not the empty-suite case."""

    def run(*args: str) -> None:
        subprocess.run(args, cwd=repo, capture_output=True, check=True)

    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npython_classes = ["*Tests"]\ntestpaths = ["tests"]\n'
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_existing.py").write_text("def test_existing():\n    assert True\n")
    run("git", "add", "-A")
    run("git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "conventions")
    return repo


def test_a_test_the_suite_never_collected_is_not_reported_as_a_test_that_passed(
    repo_with_conventions: Path,
) -> None:
    """The $31 failure, reproduced.

    `python_classes = ["*Tests"]` means a `Test*` class is collected by nothing. The suite runs,
    stays green because it executed no new test, and the old code reported "the staged test did not
    fail against the current code" — which sends a model to rewrite assertions that were never run.
    Run 33566828825 executed 1581 tests, exactly the number a clean tree runs, and said the staged
    test had passed.

    The reason id is separate because a caller keying on `tdd.not_red` should not silently start
    matching a different failure.
    """
    provider = Scripted(
        [
            _call(
                "write_file",
                path="tests/test_feature.py",
                contents="class TestFeature:\n    def test_it(self):\n        assert False\n",
            ),
            _done("staged a failing test"),
        ]
    )
    outcome = _run(provider, repo_with_conventions)

    assert outcome.status is Status.FAILED
    assert outcome.reason == "tdd.test_not_collected"

    message = outcome.findings[0].message
    assert "tests/test_feature.py" in message, "name the file, so the next attempt knows where"
    assert "python_classes" in message, "name the setting that decides collection"
    assert "did not execute your test" in message


def test_a_staged_test_that_really_passes_is_still_reported_as_not_red(
    repo_with_conventions: Path,
) -> None:
    """The other side of the split, and the one that keeps it honest. A collected test that passes
    is a different problem with a different fix, and calling it a collection failure would be a
    confident wrong answer — worse than the vague one this replaced."""
    provider = Scripted(
        [
            _call(
                "write_file",
                path="tests/test_feature.py",
                contents="def test_it():\n    assert True\n",
            ),
            _done("staged a passing test"),
        ]
    )
    outcome = _run(provider, repo_with_conventions)

    assert outcome.status is Status.FAILED
    assert outcome.reason == "tdd.not_red"
    assert "python_classes" not in outcome.findings[0].message


def test_gate_verdict_1_a_staged_test_that_exits_clean_at_import_is_not_red(repo: Path) -> None:
    """`os._exit(0)` before pytest prints a line: a green suite of zero tests, which used to satisfy
    nothing in the red phase and let the strategy proceed to implement against no test (#313).

    The reason is asserted exactly rather than as "one of the two it might be". A module that kills
    the interpreter at import IS the suite collecting nothing, and that is now its own verdict --
    which points at the file that did it instead of at assertions that were never executed (#435).
    """
    provider = Scripted(
        [
            _call("write_file", path="test_exit.py", contents="import os\n\nos._exit(0)\n"),
            _done("staged a test"),
        ]
    )
    outcome = _run(provider, repo)
    assert outcome.status is Status.FAILED, outcome
    assert outcome.reason == "tdd.suite_collected_nothing"
    assert not outcome.decided


def test_gate_verdict_1_a_staged_test_that_exits_failing_at_import_is_errored_not_red(repo: Path) -> None:
    """`os._exit(1)` used to read as red -- exit 1, zero counts. A runner that never reported is a
    broken run, and the strategy passes that through as itself rather than as a verdict."""
    provider = Scripted(
        [
            _call("write_file", path="test_exit.py", contents="import os\n\nos._exit(1)\n"),
            _done("staged a test"),
        ]
    )
    outcome = _run(provider, repo)
    assert outcome.status is Status.ERRORED and outcome.reason == "test.no_summary", outcome


def test_tdd_fails_when_the_implementation_leaves_the_test_red(repo: Path) -> None:
    """A change that does not make its own test pass is returned, not proposed."""
    provider = Scripted(
        [
            _call("write_file", path="test_calc.py", contents=_FAILING_TEST),
            _done("staged the failing test"),
            _call("write_file", path="calc.py", contents="def add(a, b):\n    return a - b\n"),  # wrong
            _done("implemented add() (badly)"),
        ]
    )
    outcome = _run(provider, repo)
    assert outcome.status is Status.FAILED
    assert outcome.reason == "tdd.not_green"
    # The change is still carried so a person can see what it tried.
    assert outcome.value is not None
    assert set(outcome.value.changeset.paths()) == {"test_calc.py", "calc.py"}


def test_gate_validate_2_the_checks_run_before_the_green_is_confirmed(repo: Path) -> None:
    """GATE-VALIDATE-2, the ordering, which is not a preference.

    A deterministic fix and a repair turn both change the code. Confirming green first would prove
    it of bytes that no longer travel -- so the suite is run last, over what the checks left, and
    both claims are claims about the same change.
    """
    from in_lockstep.core.types import ValidationFinding, ValidationReport

    class _Validator:
        """Reports one finding, then nothing: the second pass sees a repaired tree."""

        fixes = False

        def __init__(self) -> None:
            self.calls = 0

        async def invoke(self, ctx: Any, request: Validate) -> Outcome[ValidationReport]:
            self.calls += 1
            findings = (
                (ValidationFinding(rule="F401", message="`os` imported but unused", path="calc.py"),)
                if self.calls == 1
                else ()
            )
            return Outcome(
                status=Status.SUCCEEDED if not findings else Status.FAILED,
                value=ValidationReport(findings=findings),
            )

    ctx = Ctx(validator=_Validator())
    provider = Scripted(
        [
            _call("write_file", path="test_calc.py", contents=_FAILING_TEST),
            _done("staged the failing test"),
            _call("write_file", path="calc.py", contents="import os\n\n\ndef add(a, b):\n    return a + b\n"),
            _done("implemented add()"),
            # The repair turn: the unused import goes, and the implementation stays.
            _call("write_file", path="calc.py", contents="def add(a, b):\n    return a + b\n"),
            _done("removed the unused import"),
        ]
    )
    outcome = _run(provider, repo, ctx=ctx)

    assert outcome.status is Status.SUCCEEDED, outcome.findings
    # Red, then the checks and their repair, and only then the green that decides. The trailing
    # Test is revert-and-verify, which runs after everything.
    assert ctx.order[:4] == ["Test", "Validate", "Validate", "Test"], ctx.order
    assert outcome.value is not None
    contents = {c.path: c.contents for c in outcome.value.changeset.changes}
    assert contents["calc.py"] == "def add(a, b):\n    return a + b\n", "the repair is what travels"
    assert outcome.value.validation is not None and outcome.value.validation.clean


def test_gate_report_1_an_unparsed_cover_note_stays_on_the_report(repo: Path) -> None:
    """GATE-REPORT-1. `read_reply` keeps the text of a reply that was not the JSON the schema asked
    for, so the work is not thrown away — and #389 published it, because the change set's summary
    is what a pull-request body renders. It stays on the report, where the record has it, and out
    of the set that travels."""
    thinking = "Good. Now let me verify there are no issues with how the mock signature works…"
    provider = Scripted(
        [
            _call("write_file", path="test_calc.py", contents=_FAILING_TEST),
            _done("staged the failing test"),
            _call("write_file", path="calc.py", contents="def add(a, b):\n    return a + b\n"),
            LLMOutput(content=thinking),
        ]
    )
    outcome = _run(provider, repo)

    assert outcome.status is Status.SUCCEEDED, outcome.findings
    assert outcome.value is not None
    assert outcome.value.summary == thinking, "the text is still kept for the record"
    assert outcome.value.changeset.summary == "", "and never in what a body renders"
    assert any(f.id == "implement.unstructured" for f in outcome.findings)


# -- phase 3: does it answer the ticket? (#452) ----------------------------------------


class _Assessor:
    """An assessor scripted per round: `rounds[i]` is what it says the (i+1)th time it is asked."""

    def __init__(self, *rounds: tuple[tuple[str, bool], ...]) -> None:
        self.rounds = rounds
        self.seen: list[int] = []

    async def invoke(self, _ctx: Any, inp: Assess) -> Outcome[AssessReport]:
        self.seen.append(inp.round_number)
        verdicts = self.rounds[min(len(self.seen), len(self.rounds)) - 1]
        return Outcome(
            status=Status.SUCCEEDED,
            value=AssessReport(
                verdicts=tuple(
                    CriterionVerdict(criterion=c, met=m, reason="" if m else "it does not do that")
                    for c, m in verdicts
                ),
                assessor="stub:assessor",
            ),
            decided=True,
        )


def _criteria_ticket() -> Ticket:
    return Ticket(
        key="#7",
        title="Add add()",
        description="calc.add(a, b) returns a + b.",
        acceptance_criteria=("add() returns the sum", "it is documented"),
    )


def _two_phase() -> Scripted:
    return Scripted(
        [
            _call("write_file", path="test_calc.py", contents=_FAILING_TEST),
            _done("staged the failing test"),
            _call("write_file", path="calc.py", contents="def add(a, b):\n    return a + b\n"),
            _done("implemented add()"),
        ]
    )


def _three_phase() -> Scripted:
    """Red, green, then one correcting round."""
    return Scripted(
        [
            _call("write_file", path="test_calc.py", contents=_FAILING_TEST),
            _done("staged the failing test"),
            _call("write_file", path="calc.py", contents="def add(a, b):\n    return a + b\n"),
            _done("implemented add()"),
            _call(
                "write_file",
                path="calc.py",
                contents='def add(a, b):\n    """Return the sum."""\n    return a + b\n',
            ),
            _done("documented it"),
        ]
    )


def test_gate_assess_1_a_change_that_meets_its_criteria_says_so(repo: Path) -> None:
    """The happy path, and the one that has to stay cheap: met on the first ask, no correction."""
    assessor = _Assessor((("add() returns the sum", True), ("it is documented", True)))
    ctx = Ctx(assessor=assessor)

    outcome = _run(_two_phase(), repo, ctx=ctx, ticket=_criteria_ticket())

    assert outcome.status is Status.SUCCEEDED, outcome.findings
    assert assessor.seen == [1], "one assessment, no correcting round"
    report = outcome.value
    assert report is not None and report.assessment is not None and report.assessment.met
    assert any(f.id == "assess.met" for f in outcome.findings)


def test_gate_assess_1_an_unmet_criterion_goes_back_to_the_model_and_is_corrected(repo: Path) -> None:
    """The loop. A criterion a second reader can name is usually one the author can fix, so it goes
    back to the phase that wrote the code with the assessor's reason attached."""
    assessor = _Assessor(
        (("add() returns the sum", True), ("it is documented", False)),
        (("add() returns the sum", True), ("it is documented", True)),
    )
    ctx = Ctx(assessor=assessor)

    outcome = _run(_three_phase(), repo, ctx=ctx, ticket=_criteria_ticket())

    assert outcome.status is Status.SUCCEEDED, outcome.findings
    assert assessor.seen == [1, 2], "assessed, corrected, assessed again"
    report = outcome.value
    assert report is not None and report.assessment is not None and report.assessment.met
    assert any(f.id == "assess.met" and "after 2 rounds" in f.message for f in outcome.findings)


def test_gate_assess_1_the_suite_runs_again_after_a_correcting_round(repo: Path) -> None:
    """A correction edits code to satisfy a criterion and can break the tests the green phase
    proved. Without a re-run the change ships whose green was established a round ago -- a verdict
    about a suite that did not run, wearing a different hat (#435)."""
    assessor = _Assessor(
        (("add() returns the sum", True), ("it is documented", False)),
        (("add() returns the sum", True), ("it is documented", True)),
    )
    ctx = Ctx(assessor=assessor)

    _run(_three_phase(), repo, ctx=ctx, ticket=_criteria_ticket())

    # BETWEEN the two assessments, not merely after the first. Counting from the first Assess to
    # the end of the run passes whatever the loop does, because `_revert_verify` dispatches a Test
    # after the loop has finished -- which is the vacuous shape this suite keeps finding elsewhere,
    # and the first version of this test had it.
    first, second = (i for i, verb in enumerate(ctx.order) if verb == "Assess")
    between = ctx.order[first + 1 : second]

    assert "Test" in between, (
        f"the suite did not run between the correcting round and the reassessment: {ctx.order}"
    )


def test_gate_assess_1_rounds_are_bounded_and_exhaustion_says_which_criteria_are_unmet(
    repo: Path,
) -> None:
    """The bound is a rule, not a bill. Two models can disagree about a criterion for as long as
    somebody is paying, so `max_assess_rounds` is what terminates this -- and the change still
    travels, with the gap named, because four criteria of five is work."""
    assessor = _Assessor((("add() returns the sum", True), ("it is documented", False)))
    ctx = Ctx(assessor=assessor)

    outcome = _run(_three_phase(), repo, ctx=ctx, ticket=_criteria_ticket())

    assert outcome.status is Status.SUCCEEDED, "an unmet criterion is not a failed run"
    assert assessor.seen == [1, 2], "bounded at max_assess_rounds, which defaults to 2"
    report = outcome.value
    assert report is not None and report.assessment is not None and not report.assessment.met
    unmet = next(f for f in outcome.findings if f.id == "assess.unmet")
    assert "it is documented" in unmet.message and "it does not do that" in unmet.message


def test_a_ticket_with_no_acceptance_criteria_is_not_assessed(repo: Path) -> None:
    """Nothing to assess is not everything assessed. An assessor bound and a ticket stating no
    criteria costs nothing and claims nothing."""
    assessor = _Assessor((("x", True),))
    ctx = Ctx(assessor=assessor)

    outcome = _run(_two_phase(), repo, ctx=ctx)

    assert assessor.seen == [], "not asked"
    report = outcome.value
    assert report is not None and report.assessment is None, "and not reported as met"


def test_no_assessor_bound_is_the_run_that_always_was(repo: Path) -> None:
    """The backward-compatibility claim, asserted rather than assumed: an adopter who has bound no
    `Assess` gets exactly the two-phase TDD they had, with no finding and no cost."""
    ctx = Ctx()

    outcome = _run(_two_phase(), repo, ctx=ctx, ticket=_criteria_ticket())

    assert outcome.status is Status.SUCCEEDED
    report = outcome.value
    assert report is not None and report.assessment is None
    assert "Assess" not in ctx.order
    assert not any(f.id.startswith("assess.") for f in outcome.findings)
