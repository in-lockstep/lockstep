"""GATE-ASYNC-4 — the event loop is not blocked during a model call.

Defect #1 on the list that shaped this transport: five of six providers made blocking SDK calls
inside `async def`. That freezes the whole loop, so concurrent verbs, tool dispatch and the
deadline check all stop while one HTTP request is in flight — and nothing about the code looks
wrong, because it is spelled `async def`.

`GATE-ASYNC-1` catches the specific cause structurally: every client constructor must name an
`Async*` class. This is the behavioural half, and it catches what an AST scan cannot — blocking
work anywhere else in `generate`, a `time.sleep` in a retry, a synchronous read while parsing a
response.

It drives the **real provider code**, not a stand-in. `ClaudeTransport.generate` is the shared
path for the Anthropic, Bedrock and Vertex providers, and only its client construction differs, so
overriding `_make_client` exercises the request mapping, the response mapping and the error
classification exactly as they run. `OllamaProvider` goes through its own `httpx` path end to end.
The provider SDKs are optional extras and are not installed here, which is why the seam is the
client rather than the network.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from in_lockstep.llm.providers._claude_base import ClaudeTransport
from in_lockstep.llm.providers.ollama import OllamaProvider
from in_lockstep.llm.types import LLMInput, Message

# One call takes this long. Three concurrent calls should take about this long too; three
# serialized calls take three times it. The assertion sits at 2x — far enough above the concurrent
# case to survive a loaded CI box, and far enough below the serial case to fail on a real block.
DELAY = 0.3
CONCURRENT = 3
CEILING = DELAY * 2


def _input() -> LLMInput:
    return LLMInput(model="m", system="s", messages=[Message(role="user", content="go")], max_tokens=16)


async def _run_concurrently(provider) -> float:
    started = time.monotonic()
    await asyncio.gather(*(provider.generate(_input()) for _ in range(CONCURRENT)))
    return time.monotonic() - started


# -- the shared Claude path: Anthropic, Bedrock and Vertex --------------------------------------


class _FakeMessages:
    """The SDK surface `ClaudeTransport.generate` calls, and nothing else."""

    def __init__(self, delay) -> None:
        self._delay = delay

    async def create(self, **kwargs):
        await self._delay()
        return type(
            "Response",
            (),
            {
                "content": [type("Block", (), {"type": "text", "text": "ok"})()],
                "usage": type("Usage", (), {"input_tokens": 1, "output_tokens": 1})(),
                "stop_reason": "end_turn",
            },
        )()


class _FakeClaude(ClaudeTransport):
    """The real transport with the SDK client replaced. Everything else is production code."""

    def __init__(self, delay) -> None:
        self._delay = delay
        super().__init__(settings=_settings(), creds=_creds())

    def _make_client(self, settings, creds):
        return type("Client", (), {"messages": _FakeMessages(self._delay)})()


def _settings():
    from in_lockstep.llm.interface import ProviderSettings

    return ProviderSettings(base_url="https://example.invalid")


def _creds():
    from in_lockstep.llm.interface import Credentials

    return Credentials()


async def _yields() -> None:
    """What a correct async client does: gives the loop up while it waits."""
    await asyncio.sleep(DELAY)


async def _blocks() -> None:
    """What a synchronous SDK client does inside `async def`: holds the loop."""
    time.sleep(DELAY)


def test_gate_async_4_the_claude_transport_does_not_block_the_loop() -> None:
    elapsed = asyncio.run(_run_concurrently(_FakeClaude(delay=_yields)))
    assert elapsed < CEILING, (
        f"{CONCURRENT} concurrent generate() calls took {elapsed:.2f}s; one takes {DELAY}s, so "
        f"anything near {DELAY * CONCURRENT:.2f}s means they ran one after another."
    )


def test_a_blocking_provider_fails_this_gate() -> None:
    """The negative control. A timing assertion that cannot fail is not an assertion.

    `_blocks` is the defect verbatim — a synchronous wait inside `async def`, which is what a
    non-async SDK client does — and it must be detected, or the test above proves only that
    asyncio exists.
    """
    elapsed = asyncio.run(_run_concurrently(_FakeClaude(delay=_blocks)))
    assert elapsed >= CEILING, (
        "a provider that blocks inside `async def` completed as if it had not, which means this "
        "gate would not notice the defect it exists for"
    )


# -- Ollama, end to end through its own httpx path ----------------------------------------------


class _SlowTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(DELAY)
        return httpx.Response(
            200,
            json={"message": {"content": "ok"}, "prompt_eval_count": 1, "eval_count": 1},
            request=request,
        )


def test_gate_async_4_the_ollama_provider_does_not_block_the_loop(monkeypatch) -> None:
    from in_lockstep.llm.providers import ollama as module

    original = httpx.AsyncClient

    def _slow_client(**kw):
        kw.pop("transport", None)
        return original(transport=_SlowTransport(), **kw)

    monkeypatch.setattr(module.httpx, "AsyncClient", _slow_client)
    provider = OllamaProvider(settings=_settings(), creds=_creds())
    elapsed = asyncio.run(_run_concurrently(provider))
    assert elapsed < CEILING, f"three concurrent Ollama calls took {elapsed:.2f}s"


@pytest.mark.parametrize("provider_module", ["anthropic", "bedrock", "vertex_claude"])
def test_every_claude_provider_shares_the_asserted_path(provider_module: str) -> None:
    """The test above covers three providers only because they share one `generate`.

    If one of them grows its own, this stops being true silently — and the concurrency claim would
    then cover a path that provider no longer takes.
    """
    import importlib

    module = importlib.import_module(f"in_lockstep.llm.providers.{provider_module}")
    subclasses = [
        obj
        for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, ClaudeTransport) and obj is not ClaudeTransport
    ]
    assert subclasses, f"{provider_module} no longer defines a ClaudeTransport subclass"
    for cls in subclasses:
        assert "generate" not in vars(cls), (
            f"{cls.__name__} overrides generate(), so it no longer shares the path this gate covers"
        )
