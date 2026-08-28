"""Claude wire formatting, shared by the anthropic / bedrock / vertex providers.

Promoted from a private cross-provider import (`from .vertex_claude import _format_claude_messages`)
to a module of its own — that import was the only coupling in the vendored package and made
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


def claude_kwargs(input_: object) -> dict[str, object]:
    """Build the create() kwargs shared by all three Claude transports."""
    from .types import LLMInput

    assert isinstance(input_, LLMInput)
    kwargs: dict[str, object] = {
        "model": input_.model,
        "max_tokens": input_.max_tokens,
        "messages": format_claude_messages(input_.messages),
    }
    if input_.system:
        kwargs["system"] = input_.system
    if input_.temperature > 0:
        kwargs["temperature"] = input_.temperature
    if input_.tools:
        kwargs["tools"] = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in input_.tools
        ]
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
            tool_calls.append(
                ToolCall(id=getattr(block, "id", ""), name=block.name, input=block.input)
            )
    raw = getattr(response, "usage", None)
    usage = TokenUsage(
        input_tokens=getattr(raw, "input_tokens", 0) or 0,
        output_tokens=getattr(raw, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(raw, "cache_creation_input_tokens", 0) or 0,
    )
    return content, tool_calls, usage, getattr(response, "stop_reason", "") or ""
