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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..llm.interface import LLMProvider
from ..llm.types import LLMInput, LLMOutput, Message, TokenUsage, ToolCall, ToolDefinition
from ..privileged import sink
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


def _request_record(request: LLMInput, redact: Redact) -> dict[str, Any]:
    """A request as a cassette stores it: everything `_key` hashes, so it round-trips exactly.

    Deliberately the same field set as the key rather than a friendlier summary, so a reader can
    reconstruct the request and hash it.

    It does NOT round-trip to the key it is filed under, and the sentence that used to claim it did
    was wrong for as long as nothing recorded a secret. The filing key hashes the request that was
    SENT; this stores the one that was WRITTEN, and `redact.text` sits between them. Both are
    right — a live lookup needs the raw hash, and a file on disk must not hold a credential — so
    the two hashes are simply not the same number and `Cassette.as_stored` is the index that
    reconciles them. `GATE-EVAL-3` is the assertion that a recording can be found either way.
    """
    return {
        "model": request.model,
        "system": redact.text(request.system),
        "messages": [
            {
                "role": m.role,
                "content": redact.text(m.content),
                "tool_calls": [
                    {"id": tc.id, "name": tc.name, "input": redact.value(tc.input)} for tc in m.tool_calls
                ],
                "tool_call_id": m.tool_call_id,
                "tool_name": m.tool_name,
            }
            for m in request.messages
        ],
        "max_tokens": request.max_tokens,
        "tools": [
            {"name": t.name, "description": t.description, "parameters": t.parameters} for t in request.tools
        ],
        "temperature": request.temperature,
    }


#: Where a `--record` run puts its tape when nobody said. Relative on purpose — the caller joins it
#: to the repository root, because the CLI's own default used to be resolved against the process
#: working directory and a run started from a subdirectory wrote a recording that the repository's
#: anchored ignore line did not match. One constant, so `doctor` looks where the CLI writes.
CASSETTE_DIR = ".lockstep/cassettes"


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
        sink.write_json(
            self.path,
            {
                "provider_calls": self.provider_calls,
                "tool_calls": self.tool_calls,
                "order": self.order,
            },
        )

    def record_provider(self, request: LLMInput, output: LLMOutput, redact: Redact) -> None:
        key = _key(request)
        self.provider_calls[key] = {
            # The request, beside its answer. A cassette used to keep only the hash, which is
            # enough to look a recording up and not enough to do anything else with it: it could
            # not be read, diffed against what the code composes now, or turned into a case. That
            # last one is the cost that mattered — a repository accumulating recordings of real
            # runs had no way to turn them into anything it could measure against, so the evidence
            # piled up and stayed inert. `eval harvest` reads this field.
            #
            # Redacted like everything else here. The request is the likelier of the two to carry
            # a secret: an answer is a model's prose, a request is whatever the repository put in
            # front of it.
            "request": _request_record(request, redact),
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

    def as_stored(self) -> dict[str, str]:
        """Hash of each entry's request AS STORED, mapped to the key it is filed under.

        A recording is filed under a hash of the request that was SENT and holds a request that
        was REDACTED, and those are the same bytes only when nothing was masked. So a caller
        holding a stored request — `eval run` replaying a harvested case, anyone re-reading a tape
        — computed a hash the tape had never heard of, and got "its recording is gone" about a
        recording sitting in front of it. `GATE-EVAL-3` asserts a recording can always find
        itself; the fixture that asserted it had nothing in it to redact.

        Derived rather than stored, so no tape on disk changes and nothing has to migrate: the
        stored form is right there, and hashing it is what the reader was going to do anyway. The
        filing key stays a hash of the raw request, because a LIVE request is raw and lookup by a
        redacted hash would make replay depend on which secrets this machine happens to know.
        """
        index: dict[str, str] = {}
        for key, entry in self.provider_calls.items():
            stored = entry.get("request") if isinstance(entry, dict) else None
            if isinstance(stored, dict):
                index[_key(request_from(stored))] = key
        return index

    def replay_provider(self, request: LLMInput) -> LLMOutput | None:
        key = _key(request)
        entry: Any = self.provider_calls.get(key)
        if entry is None:
            # Handed a request as it was stored rather than as it was sent. One extra pass over
            # the tape, only on a miss, and only when a miss is what would otherwise be reported.
            entry = self.provider_calls.get(self.as_stored().get(key, ""))
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

    transmits = False

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


def request_from(payload: dict[str, Any]) -> LLMInput:
    """An `LLMInput` from the JSON shape a recorded request ships in.

    A cassette stores a *hash* of its request and nothing else, which is right for lookup and
    useless for everything else: the recording cannot be read, cannot be diffed against what the
    code composes today, and cannot be replayed once anything upstream of the hash moves. Shipping
    the request beside the response is what makes a recording an artifact rather than a checksum.
    """
    return LLMInput(
        model=str(payload.get("model", "")),
        system=str(payload.get("system", "")),
        messages=[
            Message(
                role=str(m.get("role", "")),
                content=str(m.get("content", "")),
                tool_calls=[
                    ToolCall(
                        id=str(c.get("id", "")), name=str(c.get("name", "")), input=dict(c.get("input", {}))
                    )
                    for c in m.get("tool_calls", [])
                ],
                tool_call_id=str(m.get("tool_call_id", "")),
                tool_name=str(m.get("tool_name", "")),
            )
            for m in payload.get("messages", [])
        ],
        max_tokens=int(payload.get("max_tokens", 16384)),
        tools=[
            ToolDefinition(
                name=str(t.get("name", "")),
                description=str(t.get("description", "")),
                parameters=dict(t.get("parameters", {})),
            )
            for t in payload.get("tools", [])
        ],
        temperature=float(payload.get("temperature", 0.0)),
    )


def key_of(request: LLMInput) -> str:
    """The cassette identity of a request, for callers that need to compare two of them."""
    return _key(request)


class FixtureProvider(LLMProvider):
    """Replay for the *shipped demo*, which has one problem `ReplayProvider` cannot have.

    A recording is keyed on the whole composed prompt. For a user replaying their own recording
    that is exactly right: a miss means the prompt moved, and serving the old answer to a new
    question would be a fabricated result. But the shipped fixture is replayed by someone who has
    recorded nothing and cannot re-record — no key, no spend — and it is the first thing a new
    adopter runs. Under strict keying, one word edited in a guardrail turns that first run into a
    crash, and the only remedy is a real model call by somebody else.

    So this one degrades instead. On a miss it replays the request the response was *actually*
    recorded against — shipped verbatim beside the cassette, so nothing is re-keyed and nothing is
    invented — and calls `on_drift` so the difference is said out loud rather than papered over.
    A demo that quietly claimed to be today's output would be the fabrication; a demo that says
    "this is what was recorded, and this build composes something different" is a recording.

    It is deliberately not the default. `ReplayProvider` still serves every other `--offline` run,
    including this fixture the moment the user supplies their own range, because a replay that
    answers a question it was not asked is only acceptable when the answer is labelled.
    """

    transmits = False

    def __init__(
        self, cassette: Cassette, recorded: LLMInput, *, on_drift: Callable[[LLMInput, LLMInput], None]
    ) -> None:
        self.cassette = cassette
        self.recorded = recorded
        self.on_drift = on_drift

    def name(self) -> str:
        return "replay:fixture"

    async def generate(self, input: LLMInput) -> LLMOutput:
        output = self.cassette.replay_provider(input)
        if output is not None:
            return output
        output = self.cassette.replay_provider(self.recorded)
        if output is None:
            # Not drift: the shipped request and the shipped cassette disagree, which is a
            # packaging defect rather than anything the user did. `GATE-FIXTURE-1` exists so this
            # is caught in CI and never reaches the person who has nothing recorded.
            raise LookupError(
                f"the shipped request does not match the shipped cassette for "
                f"{self.recorded.model!r}. This is a defect in the fixture itself, not in your "
                f"repository; please report it."
            )
        self.on_drift(input, self.recorded)
        return output


class DryRunProvider(LLMProvider):
    """Canned answers, for pipeline smoke tests where the content does not matter."""

    transmits = False

    def __init__(self, content: str = "", *, usage: TokenUsage | None = None) -> None:
        self.content = content or '{"findings": []}'
        self.usage = usage or TokenUsage(input_tokens=10, output_tokens=5)
        self.calls: list[LLMInput] = []

    def name(self) -> str:
        return "dry-run"

    async def generate(self, input: LLMInput) -> LLMOutput:
        self.calls.append(input)
        return LLMOutput(content=self.content, usage=self.usage, stop_reason="end_turn")
