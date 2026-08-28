"""Wire types for model invocation.

Vendored from pipeline-framework `src/llm/types.py` at 6ac3cde. The shapes are preserved verbatim
because they are the substitution the pivot committed to (`LLMProvider.generate(LLMInput) ->
LLMOutput`); the only addition is cache accounting on TokenUsage, added at vendoring time because
retrofitting a field onto a type that serializes into checkpoints and the ledger is a breaking
change later (§4.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Message:
    role: str  # "user" | "assistant" | "tool_result"
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""  # for tool_result messages
    tool_name: str = ""  # for tool_result messages (Gemini needs this)


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, object] = field(default_factory=dict)


@dataclass
class ToolCall:
    id: str = ""  # provider-assigned ID for pairing with tool_result
    name: str = ""
    input: dict[str, object] = field(default_factory=dict)


@dataclass
class LLMInput:
    model: str
    system: str = ""
    messages: list[Message] = field(default_factory=list)
    max_tokens: int = 16384
    tools: list[ToolDefinition] = field(default_factory=list)
    temperature: float = 0.0


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    # Added at vendoring. Prompt caching changes what a token costs, so a budget built on
    # input/output alone is wrong the moment caching lands — and this type is checkpointed and
    # ledgered, so the field cannot be added later without breaking a serialized layout.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class LLMOutput:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=lambda: TokenUsage())
    stop_reason: str = ""
    # Set by AiInvoker when the loop hit its turn cap. Upstream returned the provider's own
    # stop_reason ("tool_use") on exhaustion, making a partial ChangeSet indistinguishable from a
    # completed one — a verb adapter cannot map that to an Outcome honestly.
    exhausted: bool = False
