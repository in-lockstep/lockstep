"""Any OpenAI-compatible server: hosted OpenAI, vLLM, LM Studio, Ollama's compat port, gateways."""

from __future__ import annotations

import json
from typing import Any

from .._errors import classify
from ..interface import Credentials, LLMProvider, ProviderSettings
from ..types import LLMInput, LLMOutput, Message, TokenUsage, ToolCall


def format_openai_messages(system: str, messages: list[Message]) -> list[dict[str, object]]:
    """Claude keeps `system` as a top-level field; OpenAI wants it as the first message."""
    result: list[dict[str, object]] = []
    if system:
        result.append({"role": "system", "content": system})

    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            result.append(
                {
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.input)},
                        }
                        for tc in m.tool_calls
                    ],
                }
            )
        elif m.role == "tool_result":
            result.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        else:
            result.append({"role": m.role, "content": m.content})
    return result


class OpenAIProvider(LLMProvider):
    """OpenAI-compatible chat completions."""

    def __init__(self, settings: ProviderSettings, creds: Credentials) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "the openai provider needs its optional dependency. In your own project: "
                "uv add 'in-lockstep[openai]'. Working inside the in-lockstep repository "
                "itself, where the package is the project: uv sync --extra openai."
            ) from e

        self._settings = settings
        kwargs: dict[str, Any] = {
            "api_key": creds.get("api_key") or "not-needed",
            "timeout": settings.timeout_seconds,
            "max_retries": 0,
        }
        if settings.base_url:
            kwargs["base_url"] = settings.base_url
        self._client = AsyncOpenAI(**kwargs)

    def name(self) -> str:
        return "openai"

    def base_url(self) -> str:
        return self._settings.base_url

    async def generate(self, input: LLMInput) -> LLMOutput:
        kwargs: dict[str, Any] = {
            "model": input.model,
            "messages": format_openai_messages(input.system, input.messages),
            "max_tokens": input.max_tokens,
        }
        if input.temperature > 0:
            kwargs["temperature"] = input.temperature
        if input.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in input.tools
            ]

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            mapped = classify(e, provider="openai")
            if mapped is not None:
                raise mapped from e
            raise

        message = response.choices[0].message
        tool_calls: list[ToolCall] = []
        for tc in message.tool_calls or []:
            try:
                arguments = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, input=arguments))

        raw = response.usage
        return LLMOutput(
            content=message.content or "",
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=getattr(raw, "prompt_tokens", 0) or 0,
                output_tokens=getattr(raw, "completion_tokens", 0) or 0,
            ),
            stop_reason=response.choices[0].finish_reason or "",
        )
