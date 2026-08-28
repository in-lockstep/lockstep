"""Record and replay.

Cassettes sit at the `LLMInput`/`LLMOutput` seam rather than at HTTP, so one is portable across
the six providers and survives swapping one for another — which is what makes them usable as an
eval substrate rather than only as a debugging aid.

Tool IO is recorded too. A cassette that captures only provider calls replays a tool-using loop by
re-running the tools, which is neither offline nor deterministic; and the gap would not show up
until a strategy that actually uses tools arrives.

Every cassette is written through `Redact`. They are committed to the repository as fixtures, and
they contain whole prompts and whole tool results.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..llm.interface import LLMProvider
from ..llm.types import LLMInput, LLMOutput, TokenUsage, ToolCall
from ..privileged.redact import Redact


def _key(request: LLMInput) -> str:
    """Identity of a request. Deterministic, and independent of dict ordering."""
    payload = {
        "model": request.model,
        "system": request.system,
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "tool_calls": [{"id": tc.id, "name": tc.name, "input": tc.input} for tc in m.tool_calls],
                "tool_call_id": m.tool_call_id,
                "tool_name": m.tool_name,
            }
            for m in request.messages
        ],
        "tools": [t.name for t in request.tools],
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _tool_key(server: str, name: str, args: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps({"server": server, "name": name, "args": args}, sort_keys=True, default=str).encode()
    ).hexdigest()


@dataclass
class Cassette:
    path: Path
    provider_calls: dict[str, dict[str, object]] = field(default_factory=dict)
    tool_calls: dict[str, str] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> Cassette:
        p = Path(path)
        if not p.exists():
            return cls(path=p)
        data = json.loads(p.read_text())
        return cls(
            path=p,
            provider_calls=data.get("provider_calls", {}),
            tool_calls=data.get("tool_calls", {}),
            order=data.get("order", []),
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "provider_calls": self.provider_calls,
                    "tool_calls": self.tool_calls,
                    "order": self.order,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    def record_provider(self, request: LLMInput, output: LLMOutput, redact: Redact) -> None:
        key = _key(request)
        self.provider_calls[key] = {
            "content": redact.text(output.content),
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "input": redact.value(tc.input)} for tc in output.tool_calls
            ],
            "usage": {
                "input_tokens": output.usage.input_tokens,
                "output_tokens": output.usage.output_tokens,
                "cache_read_tokens": output.usage.cache_read_tokens,
                "cache_write_tokens": output.usage.cache_write_tokens,
            },
            "stop_reason": output.stop_reason,
        }
        self.order.append(key)

    def replay_provider(self, request: LLMInput) -> LLMOutput | None:
        entry: Any = self.provider_calls.get(_key(request))
        if entry is None:
            return None
        usage = entry.get("usage", {})
        return LLMOutput(
            content=str(entry.get("content", "")),
            tool_calls=[
                ToolCall(id=str(c["id"]), name=str(c["name"]), input=dict(c["input"]))
                for c in entry.get("tool_calls", [])
            ],
            usage=TokenUsage(
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                cache_read_tokens=int(usage.get("cache_read_tokens", 0)),
                cache_write_tokens=int(usage.get("cache_write_tokens", 0)),
            ),
            stop_reason=str(entry.get("stop_reason", "")),
        )

    def record_tool(
        self, server: str, name: str, args: dict[str, object], result: str, redact: Redact
    ) -> None:
        self.tool_calls[_tool_key(server, name, args)] = redact.text(result)

    def replay_tool(self, server: str, name: str, args: dict[str, object]) -> str | None:
        return self.tool_calls.get(_tool_key(server, name, args))


class RecordingProvider(LLMProvider):
    """Wraps a real provider and writes what it saw."""

    def __init__(self, inner: LLMProvider, cassette: Cassette, redact: Redact | None = None) -> None:
        self.inner = inner
        self.cassette = cassette
        self.redact = redact or Redact()

    def name(self) -> str:
        return f"recording:{self.inner.name()}"

    def base_url(self) -> str:
        return self.inner.base_url()

    async def generate(self, input: LLMInput) -> LLMOutput:
        output = await self.inner.generate(input)
        self.cassette.record_provider(input, output, self.redact)
        self.cassette.save()
        return output


class ReplayProvider(LLMProvider):
    """Serves from a cassette. No network, no keys, no spend."""

    def __init__(self, cassette: Cassette) -> None:
        self.cassette = cassette

    def name(self) -> str:
        return "replay"

    async def generate(self, input: LLMInput) -> LLMOutput:
        output = self.cassette.replay_provider(input)
        if output is None:
            raise LookupError(
                f"no cassette entry for this request against {input.model!r}. A replay that "
                f"silently called out would not be a replay; re-record with --record."
            )
        return output


class DryRunProvider(LLMProvider):
    """Canned answers, for pipeline smoke tests where the content does not matter."""

    def __init__(self, content: str = "", *, usage: TokenUsage | None = None) -> None:
        self.content = content or '{"findings": []}'
        self.usage = usage or TokenUsage(input_tokens=10, output_tokens=5)
        self.calls: list[LLMInput] = []

    def name(self) -> str:
        return "dry-run"

    async def generate(self, input: LLMInput) -> LLMOutput:
        self.calls.append(input)
        return LLMOutput(content=self.content, usage=self.usage, stop_reason="end_turn")
