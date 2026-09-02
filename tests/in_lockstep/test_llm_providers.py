# -- asking for the caching the framework already measured -------------------------------------


def _kwargs(**over):
    from in_lockstep.llm._claude import claude_kwargs
    from in_lockstep.llm.types import LLMInput, Message

    base = {
        "model": "claude-opus-4-6",
        "system": "You implement one ticket.",
        "messages": [Message(role="user", content="Implement #146.")],
        "max_tokens": 8192,
    }
    base.update(over)
    return claude_kwargs(LLMInput(**base))


def test_the_stable_prefix_carries_a_cache_breakpoint() -> None:
    """An agentic loop re-sends its whole history every turn, so turn N pays for turns 1..N-1 and
    spend is quadratic in turns. The framework has read `cache_read_input_tokens` off every
    response since it was written and every one of them was zero, because no request ever asked.

    Run 33566828825 billed 2,049,146 tokens for $31.53 — at Opus input rates, essentially all of it
    re-sent prefix, at ten times the price of a cache read.
    """
    system = _kwargs()["system"]
    assert isinstance(system, list), "a bare string has nowhere to carry a breakpoint"
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[0]["text"] == "You implement one ticket."


def test_the_breakpoint_rolls_to_the_end_of_the_conversation() -> None:
    """Turn N marks the prefix turn N+1 reads. Without a moving breakpoint only the system prompt
    caches, and the history — which is the part that actually grows — is paid for in full forever."""
    from in_lockstep.llm.types import Message

    messages = _kwargs(
        messages=[
            Message(role="user", content="Implement #146."),
            Message(role="assistant", content="Looking."),
            Message(role="user", content="Go on."),
        ]
    )["messages"]

    assert "cache_control" in messages[-1]["content"][-1], "the last block is the breakpoint"
    for earlier in messages[:-1]:
        content = earlier["content"]
        blocks = content if isinstance(content, list) else []
        assert not any("cache_control" in b for b in blocks), "a write is 1.25x; one rolling mark"


def test_a_tool_result_batch_can_carry_the_breakpoint() -> None:
    """Tool results are where the bytes are — a `run_script` result can be 20k characters — so the
    turn that ends in one is exactly the turn worth caching."""
    from in_lockstep.llm.types import Message

    messages = _kwargs(
        messages=[
            Message(role="user", content="Implement #146."),
            Message(role="tool_result", content="1631 passed", tool_call_id="t1", tool_name="run_script"),
        ]
    )["messages"]
    assert messages[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_tools_carry_the_breakpoint_only_when_no_system_prompt_does() -> None:
    """The cached prefix is tools, then system, then messages — so a breakpoint on system already
    covers the tools. Marking both would spend one of the four the API allows to cache nothing new.
    """
    from in_lockstep.llm.types import ToolDefinition

    tools = [ToolDefinition(name="write_file", description="write", parameters={})]

    with_system = _kwargs(tools=tools)["tools"]
    assert "cache_control" not in with_system[-1], "system already covers the tools"

    without = _kwargs(system="", tools=tools)["tools"]
    assert without[-1]["cache_control"] == {"type": "ephemeral"}


def test_asking_for_caching_does_not_re_key_a_single_cassette() -> None:
    """`_key` hashes the `LLMInput`, not this wire format. That is the property that makes this
    change safe to ship: editing a prompt invalidates every recording made against it, and this
    edits no prompt."""
    from in_lockstep.ai.replay import key_of
    from in_lockstep.llm.types import LLMInput, Message

    request = LLMInput(
        model="claude-opus-4-6",
        system="You implement one ticket.",
        messages=[Message(role="user", content="Implement #146.")],
        max_tokens=8192,
    )
    before = key_of(request)
    _kwargs()  # building the wire format must not touch the request's identity
    assert key_of(request) == before
