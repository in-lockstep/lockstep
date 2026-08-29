"""Gemini via Vertex AI."""

from __future__ import annotations

from typing import Any

from .._errors import classify
from ..interface import Credentials, LLMProvider, ProviderSettings
from ..types import LLMInput, LLMOutput, TokenUsage, ToolCall


class GoogleGeminiProvider(LLMProvider):
    def __init__(self, settings: ProviderSettings, creds: Credentials) -> None:
        try:
            from google import genai
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "the google provider needs its optional dependency. In your own project: "
                "uv add 'in-lockstep[google]'. Working inside the in-lockstep repository "
                "itself, where the package is the project: uv sync --extra google."
            ) from e

        self._settings = settings
        self._genai = genai
        self._client = genai.Client(vertexai=True, project=settings.project_id, location=settings.region)

    def name(self) -> str:
        return "google-gemini"

    def base_url(self) -> str:
        return self._settings.base_url

    async def generate(self, input: LLMInput) -> LLMOutput:
        from google.genai import types as gt

        contents: list[Any] = []
        for m in input.messages:
            if m.role == "assistant" and m.tool_calls:
                parts: list[Any] = []
                if m.content:
                    parts.append(gt.Part(text=m.content))
                for tc in m.tool_calls:
                    parts.append(gt.Part(function_call=gt.FunctionCall(name=tc.name, args=tc.input)))
                contents.append(gt.Content(role="model", parts=parts))
            elif m.role == "tool_result":
                contents.append(
                    gt.Content(
                        role="user",
                        parts=[
                            gt.Part(
                                function_response=gt.FunctionResponse(
                                    name=m.tool_name or "unknown",
                                    response={"result": m.content},
                                )
                            )
                        ],
                    )
                )
            else:
                role = "model" if m.role == "assistant" else "user"
                contents.append(gt.Content(role=role, parts=[gt.Part(text=m.content)]))

        config: dict[str, Any] = {
            "max_output_tokens": input.max_tokens,
            "temperature": input.temperature,
        }
        if input.system:
            config["system_instruction"] = input.system
        if input.tools:
            # Upstream built FunctionDeclaration(name=..., description=...) and DROPPED
            # `parameters` entirely, so the model was handed tools with no argument schema and
            # had to guess. Passing the schema is the whole point of a tool definition.
            config["tools"] = [
                gt.Tool(
                    function_declarations=[
                        gt.FunctionDeclaration(
                            name=t.name,
                            description=t.description,
                            parameters=t.parameters or None,
                        )
                        for t in input.tools
                    ]
                )
            ]

        try:
            response = await self._client.aio.models.generate_content(
                model=input.model,
                contents=contents,
                config=gt.GenerateContentConfig(**config),
            )
        except Exception as e:  # noqa: BLE001
            mapped = classify(e, provider="google-gemini")
            if mapped is not None:
                raise mapped from e
            raise

        content = ""
        tool_calls: list[ToolCall] = []
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            for i, part in enumerate(getattr(candidates[0].content, "parts", None) or []):
                if getattr(part, "text", None):
                    content += part.text
                fc = getattr(part, "function_call", None)
                if fc is not None:
                    # Gemini has no call ids. Upstream synthesized f"gemini-{name}", which
                    # collides when the model calls one tool twice in a turn — and the loop pairs
                    # tool_result by id. Index makes it unique within the turn.
                    tool_calls.append(
                        ToolCall(
                            id=f"gemini-{i}-{fc.name or 'call'}",
                            name=fc.name or "",
                            input=dict(fc.args or {}),
                        )
                    )

        raw = getattr(response, "usage_metadata", None)
        return LLMOutput(
            content=content,
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=getattr(raw, "prompt_token_count", 0) or 0,
                output_tokens=getattr(raw, "candidates_token_count", 0) or 0,
                cache_read_tokens=getattr(raw, "cached_content_token_count", 0) or 0,
            ),
            stop_reason=str(getattr(candidates[0], "finish_reason", "")) if candidates else "",
        )
