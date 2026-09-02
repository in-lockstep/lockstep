"""Claude wire formatting, shared by the anthropic / bedrock / vertex providers.

Promoted from a private cross-provider import (`from .vertex_claude import _format_claude_messages`)
to a module of its own — that import was the only coupling between two providers and made
vertex_claude load-bearing for two providers that do not otherwise use it.
"""

from __future__ import annotations

from .types import Message


def format_claude_messages(messages: list[Message]) -> list[dict[str, object]]:
    """Format messages for the Claude API, handling tool_result messages.

    - assistant messages with tool_calls become one message with tool_use blocks.
    - consecutive tool_result messages are batched into one "user" message with all tool_result
      content blocks (never mixed into an assistant message).
    """
    result: list[dict[str, object]] = []

    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            content: list[dict[str, object]] = []
            if m.content:
                content.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                content.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input})
            result.append({"role": "assistant", "content": content})

        elif m.role == "tool_result":
            block: dict[str, object] = {
                "type": "tool_result",
                "tool_use_id": m.tool_call_id,
                "content": m.content,
            }
            if result and result[-1].get("role") == "user":
                last = result[-1].get("content")
                if (
                    isinstance(last, list)
                    and last
                    and isinstance(last[0], dict)
                    and last[0].get("type") == "tool_result"
                ):
                    last.append(block)
                    continue
            result.append({"role": "user", "content": [block]})

        else:
            result.append({"role": m.role, "content": m.content})

    return result


#: A cache breakpoint. Everything BEFORE it in the request — tools, then system, then messages, in
#: that order — is stored and served at a tenth of the input price for five minutes.
EPHEMERAL: dict[str, object] = {"type": "ephemeral"}


def _mark_cacheable(message: dict[str, object]) -> None:
    """Put a cache breakpoint on a message's final content block.

    A plain-string `content` is promoted to a one-element block list, because `cache_control` is a
    property of a block and a string has nowhere to carry it.
    """
    content = message.get("content")
    if isinstance(content, str):
        if content:
            message["content"] = [{"type": "text", "text": content, "cache_control": dict(EPHEMERAL)}]
        return
    if isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1]["cache_control"] = dict(EPHEMERAL)


def claude_kwargs(input_: object) -> dict[str, object]:
    """Build the create() kwargs shared by all three Claude transports.

    ## Why this asks for caching

    An agentic loop re-sends its whole accumulated history every turn — it has to, because an
    assistant turn must be present for its tool results to be valid — so turn N pays for everything
    read in turns 1..N-1 and spend is quadratic in turns rather than linear. `_project` in the
    invoker says as much in its own docstring, and until now nothing was done about it.

    The framework already *measured* the caching it never asked for: `claude_output` has read
    `cache_read_input_tokens` and `cache_creation_input_tokens` since it was written, and
    `CostTable` has priced both. Every one of those numbers was zero, because no request ever
    carried a breakpoint.

    What it was costing, from a real run: 33566828825 billed 2,049,146 tokens for $31.53, and at
    Opus's $15/M input that is essentially the entire bill in re-sent prefix. Cache reads are
    $1.50/M — a tenth — so the same run should land nearer $3 to $6.

    ## Where the breakpoints go

    Two, of the four the API allows:

      * **After `system`.** The cached prefix runs tools → system → messages in that order, so one
        breakpoint on system covers the tool definitions too. This is the large, entirely stable
        part: guardrails, the strategy body, the repository's own house rules.
      * **After the last message**, moved each turn. Turn N marks the prefix that turn N+1 reads,
        which is what makes an incremental conversation cache at all.

    A prefix shorter than the model's minimum (1024 tokens, 2048 on Haiku) is silently not cached
    rather than refused, so the early turns of a short session cost nothing extra. A write is 1.25x
    input, so a prefix cached and never re-read loses 25% of it — which is why the rolling
    breakpoint sits at the end of the conversation rather than after every message.

    Cassettes are unaffected. `_key` hashes the `LLMInput`, not this wire format, so nothing
    recorded before this change stops replaying.
    """
    from .types import LLMInput

    assert isinstance(input_, LLMInput)
    messages = format_claude_messages(input_.messages)
    kwargs: dict[str, object] = {
        "model": input_.model,
        "max_tokens": input_.max_tokens,
        "messages": messages,
    }
    if input_.system:
        # A block list rather than a bare string, which is the only shape that can carry a
        # breakpoint. The API accepts both spellings for the same text.
        kwargs["system"] = [{"type": "text", "text": input_.system, "cache_control": dict(EPHEMERAL)}]
    if input_.temperature > 0:
        kwargs["temperature"] = input_.temperature
    if input_.tools:
        tools = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in input_.tools
        ]
        if not input_.system:
            # Nothing follows the tools to carry the breakpoint, so it goes on the last one. A
            # session with tools and no system prompt is unusual and still worth caching.
            tools[-1]["cache_control"] = dict(EPHEMERAL)
        kwargs["tools"] = tools
    if messages:
        _mark_cacheable(messages[-1])
    return kwargs


def claude_output(response: object) -> tuple[str, list[object], object, str]:
    """Unpack a Claude response into (content, tool_calls, usage, stop_reason)."""
    from .types import TokenUsage, ToolCall

    content = ""
    tool_calls: list[object] = []
    for block in getattr(response, "content", []):
        if hasattr(block, "text"):
            content += block.text
        elif hasattr(block, "name") and hasattr(block, "input"):
            tool_calls.append(ToolCall(id=getattr(block, "id", ""), name=block.name, input=block.input))
    raw = getattr(response, "usage", None)
    usage = TokenUsage(
        input_tokens=getattr(raw, "input_tokens", 0) or 0,
        output_tokens=getattr(raw, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(raw, "cache_creation_input_tokens", 0) or 0,
    )
    return content, tool_calls, usage, getattr(response, "stop_reason", "") or ""
