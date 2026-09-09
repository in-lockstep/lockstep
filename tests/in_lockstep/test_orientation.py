"""A reading phase is not a stalled one, and the counter can now tell them apart.

`GATE-PROGRESS-1` stops a session that has stopped making progress. Progress was a count of writes
the workspace accepted and suite runs over a change set not run before, so every read and every
search counted for nothing -- and forty distinct queries over fifteen files and forty repeats of
one query were the same number, zero.

Run 34402407065 was the second `/implement` on #410 to die that way. Its forty calls are below,
lifted from the evidence case the run itself filed: forty calls, forty of them distinct, ending
"Now I have a thorough understanding. Let me write the failing test." -- cut one turn before its
first write, at $3.46. The row had already recorded this failing once at half the number (run
34152787128, #337) and the remedy was to double it; this is the same defect one doubling later,
which is why what is counted changed rather than the number changing again.

Before the first write, a question the session has not asked before is progress. After it, nothing
changes: twenty turns of reading over a change already staged is wandering, and still stops.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from in_lockstep.ai.builtins import Workspace, read_write
from in_lockstep.ai.invoker import AiInvoker, InvokePolicy
from in_lockstep.ai.pricing import CostTable, Rate
from in_lockstep.ai.retry import RetryPolicy
from in_lockstep.core.spend import Spend
from in_lockstep.llm.interface import LLMProvider
from in_lockstep.llm.types import LLMInput, LLMOutput, Message, ToolCall
from in_lockstep.privileged.egress import UnsandboxedEgress

#: Every tool call run 34402407065 made, in order, from the evidence case it filed. Forty
#: calls, forty distinct, fifteen files. Kept verbatim rather than paraphrased: the claim
#: this file makes is about THIS session, and a tidied-up version of it proves less.
RUN_34402407065: tuple[tuple[str, dict[str, object]], ...] = (
    ("search_code", {"mode": "map"}),
    ("search_text", {"pattern": "CommandValidate"}),
    ("search_text", {"pattern": "staged_refusal"}),
    ("search_code", {"file": "src/in_lockstep/adapters/command.py", "mode": "skeleton"}),
    ("read_file", {"limit": 95, "offset": 246, "path": "src/in_lockstep/adapters/command.py"}),
    ("read_file", {"path": "src/in_lockstep/adapters/worktree.py"}),
    ("search_text", {"pattern": "READS_REPO"}),
    ("read_file", {"limit": 50, "offset": 855, "path": "src/in_lockstep/adapters/ai/strategy.py"}),
    ("search_text", {"glob": "src/in_lockstep/adapters/ai/strategy.py", "pattern": "def _checked"}),
    ("read_file", {"limit": 60, "offset": 619, "path": "src/in_lockstep/adapters/ai/strategy.py"}),
    ("read_file", {"path": "src/in_lockstep/adapters/sandbox.py"}),
    ("read_file", {"limit": 120, "offset": 1, "path": "tests/in_lockstep/test_validate_phase.py"}),
    ("read_file", {"limit": 100, "offset": 120, "path": "tests/in_lockstep/test_validate_phase.py"}),
    ("search_text", {"glob": "src/in_lockstep/core/verbs.py", "pattern": "EXECUTES_CODE"}),
    ("read_file", {"limit": 30, "offset": 155, "path": "src/in_lockstep/core/verbs.py"}),
    ("read_file", {"limit": 60, "offset": 600, "path": "tests/in_lockstep/test_validate_phase.py"}),
    ("read_file", {"limit": 50, "offset": 1, "path": "src/in_lockstep/adapters/ruff_adapter.py"}),
    ("read_file", {"path": "src/in_lockstep/adapters/detected.py"}),
    ("search_text", {"glob": "src/in_lockstep/adapters/ai/strategy.py", "pattern": "def _test_runner"}),
    ("read_file", {"limit": 50, "offset": 818, "path": "src/in_lockstep/adapters/ai/strategy.py"}),
    ("read_file", {"path": "tests/in_lockstep/test_validate_tool.py"}),
    ("search_text", {"pattern": "staged_refusal.*Validate|validate.*staged_refusal"}),
    ("search_text", {"pattern": "def test.*staged.*refus"}),
    (
        "search_text",
        {"pattern": "def test.*sandbox.*host|def test.*host.*fallback|def test.*container.*rule"},
    ),
    ("read_file", {"limit": 60, "offset": 263, "path": "tests/in_lockstep/test_controls.py"}),
    ("read_file", {"limit": 15, "offset": 130, "path": "src/in_lockstep/adapters/command.py"}),
    ("search_text", {"glob": "design/gates.md", "pattern": "GATE-VALIDATE"}),
    ("read_file", {"limit": 5, "offset": 121, "path": "design/gates.md"}),
    ("search_text", {"glob": "design/gates.md", "pattern": "GATE-SANDBOX"}),
    ("read_file", {"limit": 5, "offset": 182, "path": "design/gates.md"}),
    ("read_file", {"limit": 50, "offset": 130, "path": ".lockstep/lockstep.py"}),
    ("read_file", {"limit": 40, "offset": 95, "path": ".lockstep/lockstep.py"}),
    ("read_file", {"limit": 30, "offset": 55, "path": "tests/in_lockstep/test_detect.py"}),
    (
        "search_text",
        {"glob": "src/in_lockstep/cli.py", "pattern": "capabilities.*Validate|Validate.*capabilities"},
    ),
    ("search_text", {"glob": "src/in_lockstep/cli.py", "pattern": "def ls"}),
    ("read_file", {"limit": 100, "offset": 1427, "path": "src/in_lockstep/cli.py"}),
    ("search_text", {"glob": "src/in_lockstep/cli.py", "pattern": "capabilities"}),
    ("read_file", {"limit": 55, "offset": 570, "path": "src/in_lockstep/adapters/ai/strategy.py"}),
    ("read_file", {"limit": 80, "offset": 215, "path": "tests/in_lockstep/test_validate_phase.py"}),
    ("search_text", {"pattern": "def test_gate_validate_2_a_build_that_cannot_be_contained"}),
)


class _Scripted(LLMProvider):
    """Emits one scripted tool call per turn, then answers."""

    def __init__(self, calls: tuple[tuple[str, dict[str, object]], ...]) -> None:
        self._calls = list(calls)
        self.turns = 0

    def name(self) -> str:
        return "scripted"

    async def generate(self, input: LLMInput) -> LLMOutput:
        self.turns += 1
        if not self._calls:
            return LLMOutput(content="done")
        name, args = self._calls.pop(0)
        return LLMOutput(content="", tool_calls=[ToolCall(id=str(self.turns), name=name, input=dict(args))])


def _drive(
    workspace: Workspace, calls: tuple[tuple[str, dict[str, object]], ...], *, max_turns: int = 200
) -> object:
    table = CostTable()
    table.add("m", Rate(3.0, 15.0))
    tools, run = read_write(workspace)
    invoker = AiInvoker(
        _Scripted(calls),
        model="m",
        cost_table=table,
        spend=Spend(),
        retry=RetryPolicy(attempts=1, base_delay=0),
        egress=UnsandboxedEgress(),
    )
    return asyncio.run(
        invoker.run(
            system="s",
            messages=[Message(role="user", content="go")],
            tools=tools,
            run_tool=run,
            policy=InvokePolicy(max_turns=max_turns),
        )
    )


@pytest.fixture
def repo(tmp_path: Path) -> Workspace:
    """The fifteen files run 34402407065 touched, long enough for its offsets to land."""
    for _, args in RUN_34402407065:
        for key in ("path", "glob", "file"):
            name = args.get(key)
            if isinstance(name, str) and "*" not in name:
                target = tmp_path / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.write_text("".join(f"line {n}\n" for n in range(1, 1_600)))
    return Workspace(root=tmp_path)


def test_gate_progress_1_a_session_that_asked_forty_different_questions_is_not_stopped(
    repo: Workspace,
) -> None:
    """GATE-PROGRESS-1. The regression case, driven through the counter rather than through a
    model: run 34402407065's own forty calls, none of them a repeat, must not read as idle."""
    result = _drive(repo, RUN_34402407065)

    assert not getattr(result, "stalled", False), (
        "a session that asked forty different questions and repeated none was stopped as idle"
    )


def test_gate_progress_1_a_session_repeating_one_question_still_stops(repo: Workspace) -> None:
    """GATE-PROGRESS-1. The other side of the ratchet, and the reason this is about novelty rather
    than about a bigger number: forty repeats of one read is the case the ceiling exists for, and
    it stops at exactly the count it stopped at before."""
    one = RUN_34402407065[4]
    result = _drive(repo, tuple(one for _ in range(60)))

    assert getattr(result, "stalled", False), "a session re-asking one question is idling"
    assert getattr(result, "idle_turns", 0) == 40, "the doubled pre-staging allowance, unchanged"


def test_gate_progress_1_after_the_first_write_reading_is_wandering_again(repo: Workspace) -> None:
    """GATE-PROGRESS-1. Nothing changes after the first stage, which is the half the #319 evidence
    supports: twenty turns of reading over a change already staged is a session going nowhere,
    however many different files it opens."""
    write: tuple[str, dict[str, object]] = (
        "write_file",
        {"path": "src/new.py", "contents": "x = 1"},
    )
    reads = tuple(
        ("read_file", {"path": "src/in_lockstep/cli.py", "offset": n, "limit": 5}) for n in range(1, 40)
    )
    result = _drive(repo, (write, *reads))

    assert getattr(result, "stalled", False), "reading after a write is the wandering case"
    assert getattr(result, "idle_turns", 0) == 20, "the ceiling, not the doubled allowance"
    assert "wrote src/new.py" in getattr(result, "last_progress", "")


def test_orientation_does_not_halve_the_allowance_it_exists_for(repo: Workspace) -> None:
    """A question answered is not a change made. If exploring set `last_progress` the allowance
    would drop from forty to twenty in the middle of the phase the doubling is for, so a session
    that has only read must still be running at turn thirty."""
    reads = tuple(
        ("read_file", {"path": "src/in_lockstep/cli.py", "offset": n, "limit": 5}) for n in range(1, 31)
    )
    result = _drive(repo, reads)

    assert not getattr(result, "stalled", False)
    assert getattr(result, "last_progress", "") == "", (
        "reading set `last_progress`, which halves the pre-staging allowance"
    )


def test_two_windows_of_one_file_are_two_questions_and_a_repeat_is_not(tmp_path: Path) -> None:
    """The arguments are part of what makes a question new. Offset 1 and offset 600 of one file
    are two questions -- that is how a model reads something too long to hold in one call, and
    every ranged read run 34402407065 made was one of them. Asking twice is not."""
    (tmp_path / "long.py").write_text("".join(f"line {n}\n" for n in range(1, 1_000)))
    _, runner = read_write(Workspace(root=tmp_path))

    async def ask(name: str, args: dict[str, object]) -> None:
        await runner("builtin", name, args)

    asyncio.run(ask("read_file", {"path": "long.py", "offset": 1, "limit": 50}))
    assert runner.explored == 1
    asyncio.run(ask("read_file", {"path": "long.py", "offset": 1, "limit": 50}))
    assert runner.explored == 1, "the same window twice is one question"
    asyncio.run(ask("read_file", {"path": "long.py", "offset": 600, "limit": 50}))
    assert runner.explored == 2, "a different window of one file is a different question"
    assert runner.progress == 0, "and none of it is progress in the sense a write is"
