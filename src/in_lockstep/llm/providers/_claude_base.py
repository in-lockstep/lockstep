"""Shared base for the three Claude transports (direct, Bedrock, Vertex).

All three differ only in which async client they construct; the request/response mapping and the
error classification are identical, so they live here once.
"""

from __future__ import annotations

from typing import Any

from .._claude import claude_kwargs, claude_output
from .._errors import classify
from ..interface import Credentials, LLMProvider, ProviderSettings
from ..types import LLMInput, LLMOutput, ToolCall


class ClaudeTransport(LLMProvider):
    _name = "claude"

    def __init__(self, settings: ProviderSettings, creds: Credentials) -> None:
        self._settings = settings
        self._creds = creds
        self._client: Any = self._make_client(settings, creds)

    def _make_client(self, settings: ProviderSettings, creds: Credentials) -> Any:
        raise NotImplementedError

    def name(self) -> str:
        return self._name

    def base_url(self) -> str:
        """What the constructed client will dial, read off the client rather than the settings.

        The SDK resolves the destination itself for the cloud transports -- the region decides
        Bedrock's and Vertex's host -- and, for the direct one, honours a base URL the operator
        never passed through us. Reporting the settings back would compare a declaration with
        itself; this is what makes GATE-AUTH-2's comparison real for these three (#309).
        """
        url = getattr(self._client, "base_url", None)
        return str(url).rstrip("/") if url else self._settings.base_url

    async def generate(self, input: LLMInput) -> LLMOutput:
        try:
            response = await self._client.messages.create(**claude_kwargs(input))
        except Exception as e:  # noqa: BLE001 - classified, then re-raised
            mapped = classify(e, provider=self._name)
            if mapped is not None:
                raise mapped from e
            raise

        content, tool_calls, usage, stop_reason = claude_output(response)
        return LLMOutput(
            content=content,
            tool_calls=[tc for tc in tool_calls if isinstance(tc, ToolCall)],
            usage=usage,  # type: ignore[arg-type]
            stop_reason=stop_reason,
        )
