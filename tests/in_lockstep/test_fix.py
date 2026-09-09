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

from in_lockstep.adapters.ai.fix import DiagnoseThenFix, Fix
from in_lockstep.adapters.pytest_adapter import PytestTest
from in_lockstep.adapters.sandbox import Runner, Sandbox
from in_lockstep.ai.invoker import AiInvoker, InvokePolicy
from in_lockstep.ai.pricing import CostTable, Rate
from in_lockstep.core.outcome import Outcome, Status
from in_lockstep.core.spend import Budget, Spend
from in_lockstep.core.types import Test
from in_lockstep.llm.interface import LLMProvider
from in_lockstep.llm.types import LLMInput, LLMOutput, TokenUsage, ToolCall
from in_lockstep.platform.tickets import Ticket
from in_lockstep.privileged.egress import UnsandboxedEgress

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


def _adapter(provider: LLMProvider, root: Path) -> DiagnoseThenFix:
    return DiagnoseThenFix(
        lambda ctx: _invoker(provider, spend=getattr(ctx, "spend", None)),
        repo_root=str(root),
        policy=InvokePolicy(max_turns=8, max_tokens=1024),
    )


class Declared(Sandbox):
    """Declares a container and runs on the host. `test_implement_tdd.Declared` says why: the
    strategy judges the runner by what it declares (GATE-SANDBOX-2), and these runs must be real."""

    def __init__(self) -> None:
        super().__init__(image="declared-for-this-test", require_container=True)

    def runtime(self) -> str | None:
        return None

    async def run(self, command: list[str], *, cwd: str | None = None, timeout: float = 900.0) -> Any:
        return await self._subprocess(command, cwd=cwd, timeout=timeout)


class Ctx:
    """A ctx whose Test verb is a real PytestTest, so reproduce/fix run for real."""

    def __init__(self, *, test_bound: bool = True, sandbox: Runner | None = None) -> None:
        self.spend = Spend(budget=Budget(usd=5.0))
        self.run_id = "t"
        self.adapter = PytestTest(args=["-q"], sandbox=sandbox or Declared())

        class _Container:
            def has(_self, verb: object) -> bool:
                # Test and nothing else. It used to answer True to every verb, which was harmless
                # only while nothing asked for a second one: the moment a strategy asked whether a
                # Validate was bound, this fixture said yes and handed back a pytest runner.
                return verb is Test and test_bound

            def resolve(_self, _verb: object) -> PytestTest:
                return self.adapter

        self.container = _Container()

    async def do(self, request: Test) -> Outcome[Any]:
        return await self.adapter.invoke(self, request)


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


def _run(
    provider: Scripted, repo: Path, *, test_bound: bool = True, sandbox: Runner | None = None
) -> Outcome[Any]:
    return asyncio.run(
        _adapter(provider, repo).invoke(Ctx(test_bound=test_bound, sandbox=sandbox), Fix(ticket=_ticket()))
    )


def test_gate_sandbox_2_the_reproducer_is_not_asked_for_when_its_runner_would_be_the_host(
    repo: Path,
) -> None:
    """GATE-SANDBOX-2, the fixing verb's half: a reproducer is a test the model wrote, and it is
    refused a host runner before the reproduce step spends anything (#308)."""
    provider = Scripted([_done()])
    outcome = _run(provider, repo, sandbox=Sandbox())
    assert outcome.status is Status.BLOCKED and outcome.reason == "sandbox.host_fallback"
    assert provider.calls == []


def test_gate_progress_1_a_reproduce_phase_that_stops_moving_is_blocked_with_its_reproducer_intact(
    repo: Path,
) -> None:
    """GATE-PROGRESS-1 through the fixing verb: `fix.no_progress`, the reproducer returned."""
    provider = Scripted(
        [_call("write_file", path="test_calc.py", contents=_REPRODUCER), _call("read_file", path="calc.py")]
    )
    adapter = DiagnoseThenFix(
        lambda ctx: _invoker(provider, spend=getattr(ctx, "spend", None)),
        repo_root=str(repo),
        policy=InvokePolicy(max_turns=12, max_tokens=1024, max_idle_turns=2),
    )
    outcome = asyncio.run(adapter.invoke(Ctx(), Fix(ticket=_ticket())))
    assert outcome.status is Status.BLOCKED and outcome.reason == "fix.no_progress"
    assert outcome.value is not None and outcome.value.changeset.paths() == ("test_calc.py",)
    assert len(provider.calls) == 3


def test_a_fix_staged_in_the_reproduce_step_is_a_head_start_not_a_failed_reproduction(repo: Path) -> None:
    """The eighth `/fix` on #319 staged the reproducer, the fix and a ledger row in the reproduce
    step; the red run passed over all three and the run ended `fix.not_reproduced` about a bug
    it had reproduced and fixed (#337). Red is a claim about the tests alone: the test-shaped
    changes go red on their own, and the rest travel into the fix step, which sees them."""
    provider = Scripted(
        [
            _call("write_file", path="test_calc.py", contents=_REPRODUCER),
            _call("write_file", path="calc.py", contents="def add(a, b):\n    return a + b\n"),  # eager
            _done("reproduced and fixed"),
            _call("read_file", path="calc.py"),
            _done("kept the fix"),
        ]
    )
    outcome = _run(provider, repo)
    assert outcome.status is Status.SUCCEEDED, (outcome.reason, outcome.findings)
    report = outcome.value
    assert report is not None
    assert report.reproducer.paths() == ("test_calc.py",)
    assert report.fix.paths() == ("calc.py",)
    fix_prompt = provider.calls[3].messages[0].content
    assert "already staged changes to `calc.py`" in fix_prompt
    shown = next(m.content for m in provider.calls[4].messages if m.role == "tool_result")
    assert "return a + b" in shown, "the fix step reads its own staged fix, not the disk"


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
    assert report is not None
    # The reproducer and the fix are separate change sets.
    assert set(report.reproducer.paths()) == {"test_calc.py"}
    assert set(report.fix.paths()) == {"calc.py"}
    # And the combined view is what apply would write.
    assert set(report.changeset.paths()) == {"test_calc.py", "calc.py"}
    assert not (repo / "test_calc.py").exists(), "nothing touched the real tree"
    # The cover note travels on the combined view: `write_changeset` serialises that and nothing
    # else, and the first fix this loop opened on its own repository was titled by its ticket
    # number over an empty body because the merge of two halves with no summary had none (#343).
    assert report.changeset.summary == "fixed the sign"
    assert report.changeset.ticket == "#9"


def test_a_fix_report_proposes_under_its_own_cover_note_whichever_half_holds_it() -> None:
    """A report built by hand with a bare `fix` set and a `summary` of its own — the shape every
    caller before #343 produced — still hands its summary and notes to the artifact."""
    from in_lockstep.adapters.ai.fix import FixReport
    from in_lockstep.core.types import ChangeSet, FileChange

    report = FixReport(
        reproducer=ChangeSet(changes=(FileChange(path="test_x.py", contents="assert 1\n"),)),
        fix=ChangeSet(changes=(FileChange(path="x.py", contents="x = 1\n"),)),
        summary="what changed, and why",
        notes=("a thing the reviewer should know",),
    )
    merged = report.changeset
    assert set(merged.paths()) == {"test_x.py", "x.py"}
    assert merged.summary == "what changed, and why"
    assert merged.notes == ("a thing the reviewer should know",)


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
    assert outcome.value is not None
    assert set(outcome.value.changeset.paths()) == {"test_calc.py", "calc.py"}
