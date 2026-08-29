"""Wire types for model invocation.

These are the framework's own types, not any SDK's. That is the point of them: a provider adapter
translates in and out of these shapes, so `ProviderRegistry` can route a verb to a different
provider without anything above the transport changing, and a cassette recorded at this seam
replays against a provider it was never recorded from.

Cache accounting is on TokenUsage from the start, because retrofitting a field onto a type that
serializes into checkpoints and the ledger is a breaking change later (§4.2).
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
    # Prompt caching changes what a token costs, so a budget built on
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
