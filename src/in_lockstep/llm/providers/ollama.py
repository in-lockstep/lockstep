"""Local models via Ollama's native /api/chat.

The only provider that was genuinely async upstream, and the only one with NO error mapping at all
— httpx.HTTPStatusError propagated raw, so an Ollama 429 was invisible to the retry layer forever.
It also discarded tool-call structure, sending role="tool_result" to Ollama verbatim as an invalid
role, and declared supports_tools() == False while parsing tool calls out of responses.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .._errors import classify
from ..interface import Credentials, LLMProvider, ProviderSettings
from ..types import LLMInput, LLMOutput, TokenUsage, ToolCall

DEFAULT_BASE_URL = "http://localhost:11434"


class OllamaProvider(LLMProvider):
    def __init__(self, settings: ProviderSettings, creds: Credentials) -> None:
        self._settings = settings
        self._base_url = (settings.base_url or DEFAULT_BASE_URL).rstrip("/")
        self._timeout = settings.timeout_seconds

    def name(self) -> str:
        return "ollama"

    def base_url(self) -> str:
        return self._base_url

    def supports_tools(self) -> bool:
        # Ollama does support tools on models that declare them, and this provider now preserves
        # tool structure in both directions. Upstream returned False here while simultaneously
        # parsing message.tool_calls out of responses.
        return True

    async def generate(self, input: LLMInput) -> LLMOutput:
        messages: list[dict[str, Any]] = []
        if input.system:
            messages.append({"role": "system", "content": input.system})
        for m in input.messages:
            if m.role == "assistant" and m.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": m.content,
                        "tool_calls": [
                            {"function": {"name": tc.name, "arguments": tc.input}} for tc in m.tool_calls
                        ],
                    }
                )
            elif m.role == "tool_result":
                # "tool_result" is not a role Ollama accepts; upstream sent it verbatim.
                messages.append({"role": "tool", "content": m.content})
            else:
                messages.append({"role": m.role, "content": m.content})

        payload: dict[str, Any] = {
            "model": input.model,
            "messages": messages,
            "stream": False,
            "options": {"num_predict": input.max_tokens},
        }
        if input.temperature > 0:
            payload["options"]["temperature"] = input.temperature
        if input.tools:
            payload["tools"] = [
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
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(f"{self._base_url}/api/chat", json=payload)
                response.raise_for_status()
                data = response.json()
        except Exception as e:  # noqa: BLE001
            mapped = classify(e, provider="ollama")
            if mapped is not None:
                raise mapped from e
            raise

        message = data.get("message") or {}
        tool_calls: list[ToolCall] = []
        for i, tc in enumerate(message.get("tool_calls") or []):
            fn = tc.get("function") or {}
            arguments = fn.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            # Ollama assigns no ids, so the loop could not pair tool_result at all.
            tool_calls.append(ToolCall(id=f"ollama-{i}", name=fn.get("name", ""), input=arguments or {}))

        return LLMOutput(
            content=message.get("content", "") or "",
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=data.get("prompt_eval_count", 0) or 0,
                output_tokens=data.get("eval_count", 0) or 0,
            ),
            stop_reason=data.get("done_reason", "") or "",
        )
