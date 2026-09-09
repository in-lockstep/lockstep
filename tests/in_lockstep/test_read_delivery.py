"""What a `read_file` actually reaches the model as, driven through the invoker that delivers it.

GATE-READ-1 promises that a truncated read "says how to continue -- how many lines the file has,
and that `offset` and `limit` reach the rest". Every test discharging that clause called the tool
runner directly, and the runner is not what hands a result to a model: `AiInvoker._dispatch` sits
between them and applied a SECOND cap, `max_tool_result_chars`, which was half `MAX_READ_CHARS`.
The sentence `_window` appends at the end of a long read was exactly what that second cut removed,
so the gate was green while the property was false end to end (#412).

The tests here run through `AiInvoker` for that reason. A property asserted one layer below the
one that delivers it is a property nobody is testing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from in_lockstep.ai.builtins import MAX_READ_CHARS, Workspace, read_only
from in_lockstep.ai.invoker import AiInvoker, InvokePolicy
from in_lockstep.ai.pricing import CostTable, Rate
from in_lockstep.ai.retry import RetryPolicy
from in_lockstep.core.spend import Spend
from in_lockstep.core.verbs import Capability
from in_lockstep.llm.interface import LLMProvider
from in_lockstep.llm.types import LLMInput, LLMOutput, Message, ToolCall
from in_lockstep.privileged.egress import UnsandboxedEgress


class _Stub(LLMProvider):
    """Answers with one tool call, then with prose. `calls` keeps what it was sent."""

    def __init__(self, tool: str, args: dict[str, object]) -> None:
        self.calls: list[LLMInput] = []
        self._replies = [
            LLMOutput(content="", tool_calls=[ToolCall(id="1", name=tool, input=args)]),
            LLMOutput(content="done"),
        ]

    def name(self) -> str:
        return "stub"

    async def generate(self, input: LLMInput) -> LLMOutput:
        self.calls.append(input)
        return self._replies.pop(0) if self._replies else LLMOutput(content="done")


def _invoker(provider: _Stub) -> AiInvoker:
    table = CostTable()
    table.add("m", Rate(3.0, 15.0))
    return AiInvoker(
        provider,
        model="m",
        cost_table=table,
        spend=Spend(),
        retry=RetryPolicy(attempts=1, base_delay=0),
        # Tests about what a tool result looks like are not tests about egress, and the default
        # reads the ambient environment.
        egress=UnsandboxedEgress(),
    )


def _delivered(workspace: Workspace, args: dict[str, object]) -> str:
    """The `read_file` result as the model is sent it, on the turn after it asked."""
    tools, run = read_only(workspace)
    provider = _Stub("read_file", args)
    asyncio.run(
        _invoker(provider).run(
            system="s",
            messages=[Message(role="user", content="go")],
            tools=tools,
            run_tool=run,
            policy=InvokePolicy(max_turns=3),
        )
    )
    second = provider.calls[1]
    return next(m.content for m in second.messages if m.role == "tool_result")


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(root=tmp_path)


def test_gate_read_1_the_paging_sentence_reaches_the_model_through_the_invoker(
    workspace: Workspace,
) -> None:
    """GATE-READ-1. The clause the row has always stated, asserted where it is delivered rather
    than where it is composed. A model told only that a file was cut works around the tool instead
    of using it, which is what run 34361896059 did and what #402 was built to stop."""
    lines = MAX_READ_CHARS  # one char plus a newline each, so comfortably over the cap
    (workspace.root / "big.txt").write_text("x\n" * lines)

    delivered = _delivered(workspace, {"path": "big.txt"})

    assert f"has {lines} line(s)" in delivered, "the size it could not show"
    assert "`offset`" in delivered and "`limit`" in delivered, "and how to ask for the rest"
    assert not delivered.endswith("…[truncated]"), (
        "the invoker's own bound cut the sentence off again; this is the defect, not the fix"
    )


def test_gate_read_1_a_read_is_never_cut_twice(workspace: Workspace) -> None:
    """GATE-READ-1. The invariant that makes the clause above hold for every file rather than for
    the one this suite happens to write: a read is bounded once, by `MAX_READ_CHARS`, INCLUDING
    what it appends to itself -- so the invoker's cap, which is the same number, never fires on
    one. Two caps that merely happened to be ordered correctly is how this broke."""
    assert InvokePolicy().max_tool_result_chars >= MAX_READ_CHARS, (
        "the prompt bound is below a whole read, so every long read is cut a second time"
    )

    wide = "".join("x" * 200 + "\n" for _ in range(2_000))
    (workspace.root / "wide.txt").write_text(wide)
    tools, run = read_only(workspace)

    whole = asyncio.run(run("builtin", "read_file", {"path": "wide.txt"}))
    ranged = asyncio.run(run("builtin", "read_file", {"path": "wide.txt", "offset": 1, "limit": 1_999}))

    assert len(whole) <= MAX_READ_CHARS, "a whole-file read overflowed the only cap it has"
    assert len(ranged) <= MAX_READ_CHARS, "a ranged read's header and notice must be inside the cap"


def test_a_file_between_the_two_old_caps_arrives_whole(workspace: Workspace) -> None:
    """The silent band. Between 20,000 and 40,000 characters the tool reported no truncation at
    all -- the file was under `MAX_READ_CHARS` -- and the invoker cut it mid-word with a bare
    marker, so the model had no signal the file continued, let alone how to reach the rest. There
    is no such band now: one cap, and a file under it arrives whole."""
    body = "line\n" * 6_000  # 30,000 chars: whole under the old read cap, halved by the old bound
    (workspace.root / "mid.py").write_text(body)

    delivered = _delivered(workspace, {"path": "mid.py"})

    assert delivered == body, "a file inside the cap must arrive as itself"
    assert "truncated" not in delivered


def test_a_tool_result_the_invoker_cuts_says_how_much_it_dropped() -> None:
    """The bound on everything that is not a read -- a delegated child's answer, an adopter's own
    tool. `…[truncated]` alone cannot tell a result that was nearly whole from one that was a
    tenth, and gives a model no number to narrow the next query with."""
    from in_lockstep.ai.tools import Tool, ToolSet

    oversized = "y" * (InvokePolicy().max_tool_result_chars + 5_000)

    async def run_tool(server: str, name: str, args: dict[str, object]) -> str:
        return oversized

    provider = _Stub("log", {})
    asyncio.run(
        _invoker(provider).run(
            system="s",
            messages=[Message(role="user", content="go")],
            tools=ToolSet.of(Tool(server="git", name="log", capabilities=frozenset({Capability.READS_REPO}))),
            run_tool=run_tool,
            policy=InvokePolicy(max_turns=3),
        )
    )
    delivered = next(m.content for m in provider.calls[1].messages if m.role == "tool_result")

    assert str(len(oversized)) in delivered, "the model is not told how much there was"
    assert "dropped" in delivered, "nor how much it is missing"


# ---------------------------------------------------------------------------
# A repository states its own window (#414)
#
# The default above was measured against THIS repository. The number that suits a tree of short
# Go files or one carrying generated clients an order of magnitude larger is a property of that
# tree, so `Workshop(max_read_chars=...)` states it and the policy stack may narrow it. What no
# amount of configuration changes is that a MODEL cannot move it: that is the caller GATE-READ-1
# means, and the last test here proves the rule rather than the constant.
# ---------------------------------------------------------------------------

CONFIGURED = 9_000


class _Module:
    """The parts of a `Lockstep` module that `complete_for` reads."""

    def __init__(self, root: Path, workshop: object) -> None:
        from in_lockstep.core.policy import PolicyStack

        self.workshop = workshop
        self.policy = PolicyStack()
        self.repo = type("_Repo", (), {"root": str(root)})()


def test_a_repository_states_its_own_read_window(tmp_path: Path) -> None:
    """`Workshop(max_read_chars=...)` reaches the tool that reads, through the policy the strategy
    resolves. The wire, asserted end to end rather than at either end of it."""
    from in_lockstep.adapters.ai.oneshot import Oneshot
    from in_lockstep.lockstep import Workshop

    strategy = Oneshot(lambda ctx: None, repo_root=str(tmp_path))
    strategy.complete_for(_Module(tmp_path, Workshop(max_read_chars=CONFIGURED)))

    assert strategy.policy.max_read_chars == CONFIGURED, "the workshop's value never reached the policy"
    assert strategy._session(object()).run_tool.max_read_chars == CONFIGURED, (
        "the policy's value never reached the tool runner"
    )


def test_a_workshop_that_says_nothing_gets_the_shipped_window(tmp_path: Path) -> None:
    """`None` means "whatever the framework ships" rather than a number restated in `Workshop`:
    `lockstep` may not import `ai`, so a literal there would be a second copy of the default."""
    from in_lockstep.adapters.ai.oneshot import Oneshot
    from in_lockstep.lockstep import Workshop

    strategy = Oneshot(lambda ctx: None, repo_root=str(tmp_path))
    strategy.complete_for(_Module(tmp_path, Workshop()))

    assert strategy.policy.max_read_chars == MAX_READ_CHARS


def test_every_session_type_honours_the_configured_window(tmp_path: Path) -> None:
    """A review reads files exactly as an implement does. A cap honoured in the session that
    executes and not in the two that do not is two answers to one question."""
    from in_lockstep.ai.builtins import read_write, read_write_execute

    space = Workspace(root=tmp_path)
    for build in (read_only, read_write, read_write_execute):
        _, runner = build(space, max_read_chars=CONFIGURED)
        assert runner.max_read_chars == CONFIGURED, f"{build.__name__} ignored it"


def test_a_policy_layer_may_lower_the_read_window_and_may_not_raise_it() -> None:
    """The stack is monotone: a contribution narrows what a model may pull into a prompt, or it
    does nothing. A layer that could widen one would be a control running backwards."""
    from in_lockstep.core.policy import Policy, PolicyStack

    def resolved_under(contributed: int) -> int:
        stack = PolicyStack()
        stack.contribute(Policy(name="org", max_read_chars=contributed))
        return InvokePolicy.under(stack.resolve(), max_turns=10, max_read_chars=CONFIGURED).max_read_chars

    assert resolved_under(CONFIGURED // 2) == CONFIGURED // 2, "a layer must be able to narrow it"
    assert resolved_under(CONFIGURED * 10) == CONFIGURED, "a layer must not be able to widen it"


def test_the_result_bound_cannot_fall_below_the_configured_read_window() -> None:
    """#413's invariant, over a configured value rather than only over the default. Two settable
    numbers would put the double cut back in through the front door: a repository that raised its
    read window and left the result bound at the shipped default would be halved again."""
    for size in (1_000, CONFIGURED, MAX_READ_CHARS * 4):
        assert InvokePolicy(max_read_chars=size).max_tool_result_chars >= size


def test_gate_read_1_a_model_cannot_widen_the_window_it_reads_through(workspace: Workspace) -> None:
    """GATE-READ-1's "not negotiable by the caller", proved against a NON-DEFAULT cap so it is the
    rule under test and not the constant.

    The caller in that clause is the model, reaching the reader through `read_file`'s arguments.
    `offset` moves the window and `limit` selects lines; neither is a byte budget. An adopter
    setting a different number does not open that door, because the value is read from the session
    the framework built and never from the request."""
    (workspace.root / "wide.txt").write_text("".join("x" * 200 + "\n" for _ in range(2_000)))
    _, run = read_only(workspace, max_read_chars=CONFIGURED)

    for args in (
        {"path": "wide.txt"},
        {"path": "wide.txt", "limit": 2_000},
        {"path": "wide.txt", "offset": 1, "limit": 1_999},
        {"path": "wide.txt", "offset": 500},
    ):
        answer = asyncio.run(run("builtin", "read_file", dict(args)))
        assert len(answer) <= CONFIGURED, f"{args} widened the window to {len(answer)}"


def test_the_configured_window_bounds_what_the_model_is_actually_sent(workspace: Workspace) -> None:
    """And it holds where #412 broke: through `AiInvoker`, which applies the result bound. A
    configured read that the dispatch bound then halved would be the same defect with a knob."""
    (workspace.root / "big.txt").write_text("x\n" * MAX_READ_CHARS)
    tools, run = read_only(workspace, max_read_chars=CONFIGURED)
    provider = _Stub("read_file", {"path": "big.txt"})
    asyncio.run(
        _invoker(provider).run(
            system="s",
            messages=[Message(role="user", content="go")],
            tools=tools,
            run_tool=run,
            policy=InvokePolicy(max_turns=3, max_read_chars=CONFIGURED),
        )
    )
    delivered = next(m.content for m in provider.calls[1].messages if m.role == "tool_result")

    assert len(delivered) <= CONFIGURED
    assert "`offset`" in delivered and "`limit`" in delivered, "the notice must still survive"
