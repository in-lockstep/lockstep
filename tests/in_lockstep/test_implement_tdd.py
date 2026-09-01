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
from in_lockstep.ai.invoker import AiInvoker, InvokePolicy
from in_lockstep.ai.pricing import CostTable, Rate
from in_lockstep.core.outcome import Status
from in_lockstep.core.spend import Budget, Spend
from in_lockstep.core.types import Test
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


class Ctx:
    """A ctx whose Test verb is a real PytestTest, so the red/green runs are real."""

    def __init__(self, *, test_bound: bool = True) -> None:
        self.spend = Spend(budget=Budget(usd=5.0))
        self.run_id = "t"

        class _Container:
            def has(self, _verb: object) -> bool:
                return test_bound

        self.container = _Container()

    async def do(self, request: Test):  # noqa: ANN202
        return await PytestTest(args=["-q"]).invoke(self, request)


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


def _run(provider: Scripted, repo: Path, *, test_bound: bool = True):
    return asyncio.run(
        _adapter(provider, repo).invoke(Ctx(test_bound=test_bound), Implement(ticket=_ticket()))
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
    assert set(outcome.value.changeset.paths()) == {"test_calc.py", "calc.py"}
