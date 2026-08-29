"""`fix/diagnose-then-fix` — reproduce the bug, then fix it, and prove both.

Driven with a scripted model but a real git repo and real pytest: the deterministic Test run
between the two model steps is the whole point of the strategy, so it is not mocked. Four things
matter — the happy path reproduces then fixes and reports the two apart; a strategy with no Test
bound refuses; a reproducer that does not fail is caught (`fix.not_reproduced`); and a change that
does not make it pass is caught (`fix.not_fixed`).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from in_lockstep.adapters.ai.fix import AiFix, FixSpec
from in_lockstep.adapters.pytest_adapter import PytestTest
from in_lockstep.ai.invoker import AiInvoker, InvokePolicy
from in_lockstep.ai.pricing import CostTable, Rate
from in_lockstep.core.outcome import Status
from in_lockstep.core.spend import Budget, Spend
from in_lockstep.core.types import TestSpec
from in_lockstep.llm.interface import LLMProvider
from in_lockstep.llm.types import LLMInput, LLMOutput, TokenUsage, ToolCall
from in_lockstep.platform.tickets import Ticket
from in_lockstep.privileged.egress import UnsandboxedEgress
from in_lockstep.strategies import default_registry

MODEL = "test-model"


class Scripted(LLMProvider):
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


def _invoker(provider: LLMProvider, *, spend: Spend | None = None) -> AiInvoker:
    table = CostTable()
    table.add(MODEL, Rate(input_per_m=1.0, output_per_m=2.0))
    return AiInvoker(
        provider,
        model=MODEL,
        cost_table=table,
        spend=spend or Spend(budget=Budget(usd=5.0)),
        egress=UnsandboxedEgress(),
    )


def _adapter(provider: LLMProvider, root: Path) -> AiFix:
    return AiFix(
        lambda ctx: _invoker(provider, spend=getattr(ctx, "spend", None)),
        registry=default_registry(),
        repo_root=str(root),
        policy=InvokePolicy(max_turns=8, max_tokens=1024),
    )


class Ctx:
    """A ctx whose Test verb is a real PytestTest, so reproduce/fix run for real."""

    def __init__(self, *, test_bound: bool = True) -> None:
        self.spend = Spend(budget=Budget(usd=5.0))
        self.run_id = "t"

        class _Container:
            def has(self, _verb: object) -> bool:
                return test_bound

        self.container = _Container()

    async def do(self, _verb: object, spec: TestSpec):  # noqa: ANN202
        return await PytestTest(args=["-q"]).invoke(self, spec)


def _ticket() -> Ticket:
    return Ticket(key="#9", title="add() subtracts", description="calc.add(2, 3) returns -1, not 5.")


def _call(name: str, **args: Any) -> LLMOutput:
    return LLMOutput(content="", tool_calls=[ToolCall(id="1", name=name, input=args)])


def _done(summary: str = "did it") -> LLMOutput:
    return LLMOutput(content=json.dumps({"summary": summary, "notes": [], "unfinished": []}))


_REPRODUCER = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()

    def run(*args: str) -> None:
        subprocess.run(args, cwd=root, capture_output=True, check=True)

    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.test")
    run("git", "config", "user.name", "t")
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")  # the bug
    run("git", "add", "-A")
    run("git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "base")
    run("git", "branch", "-M", "main")
    return root


def _run(provider: Scripted, repo: Path, *, test_bound: bool = True):
    return asyncio.run(
        _adapter(provider, repo).invoke(
            Ctx(test_bound=test_bound), FixSpec(ticket=_ticket(), strategy="fix/diagnose-then-fix")
        )
    )


def test_fix_reproduces_the_bug_then_fixes_it_and_reports_them_apart(repo: Path) -> None:
    provider = Scripted(
        [
            _call("write_file", path="test_calc.py", contents=_REPRODUCER),  # red vs the buggy add
            _done("reproduced the bug"),
            _call("write_file", path="calc.py", contents="def add(a, b):\n    return a + b\n"),  # fix
            _done("fixed the sign"),
        ]
    )
    outcome = _run(provider, repo)
    assert outcome.status is Status.SUCCEEDED, outcome.findings
    report = outcome.value
    # The reproducer and the fix are separate change sets.
    assert set(report.reproducer.paths()) == {"test_calc.py"}
    assert set(report.fix.paths()) == {"calc.py"}
    # And the combined view is what apply would write.
    assert set(report.changeset.paths()) == {"test_calc.py", "calc.py"}
    assert not (repo / "test_calc.py").exists(), "nothing touched the real tree"


def test_the_fix_step_is_shown_the_reproducer_it_must_pass(repo: Path) -> None:
    """The reproducer is staged, not on disk, so read_file cannot reach it — it has to travel in
    the fix step's prompt, or the model is fixing blind."""
    provider = Scripted(
        [
            _call("write_file", path="test_calc.py", contents=_REPRODUCER),
            _done("reproduced"),
            _call("write_file", path="calc.py", contents="def add(a, b):\n    return a + b\n"),
            _done("fixed"),
        ]
    )
    _run(provider, repo)
    # The fix step is the invocation that carries the fix-writer body; the reproducer's assertion
    # must appear somewhere in its messages.
    fix_calls = [c for c in provider.calls if "assert add(2, 3) == 5" in _text_of(c)]
    assert fix_calls, "the reproducer contents never reached the fix step"


def _text_of(inp: LLMInput) -> str:
    parts = [getattr(inp, "system", "") or ""]
    for m in getattr(inp, "messages", []) or []:
        parts.append(str(getattr(m, "content", "")))
    return "\n".join(parts)


def test_fix_refuses_when_no_test_verb_is_bound(repo: Path) -> None:
    outcome = _run(Scripted([_done()]), repo, test_bound=False)
    assert outcome.status is Status.BLOCKED
    assert outcome.reason == "fix.no_test"


def test_fix_fails_when_the_reproducer_does_not_fail(repo: Path) -> None:
    provider = Scripted(
        [
            _call("write_file", path="test_ok.py", contents="def test_ok():\n    assert True\n"),
            _done("staged a test that does not reproduce anything"),
        ]
    )
    outcome = _run(provider, repo)
    assert outcome.status is Status.FAILED
    assert outcome.reason == "fix.not_reproduced"


def test_fix_fails_when_the_change_does_not_make_the_reproducer_pass(repo: Path) -> None:
    provider = Scripted(
        [
            _call("write_file", path="test_calc.py", contents=_REPRODUCER),
            _done("reproduced the bug"),
            # A "fix" that does not fix it — add still subtracts.
            _call("write_file", path="calc.py", contents="def add(a, b):\n    return a - b  # noop\n"),
            _done("did not actually fix it"),
        ]
    )
    outcome = _run(provider, repo)
    assert outcome.status is Status.FAILED
    assert outcome.reason == "fix.not_fixed"
    # The attempt is still carried so a person can see what it tried.
    assert set(outcome.value.changeset.paths()) == {"test_calc.py", "calc.py"}
