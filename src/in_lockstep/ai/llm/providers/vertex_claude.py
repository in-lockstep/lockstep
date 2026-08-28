from __future__ import annotations

from typing import Any

from ..interface import Credentials, ProviderSettings
from ._claude_base import ClaudeTransport


class VertexClaudeProvider(ClaudeTransport):
    """Claude via Google Cloud Vertex AI."""

    _name = "vertex-claude"

    def _make_client(self, settings: ProviderSettings, creds: Credentials) -> Any:
        try:
            from anthropic import AsyncAnthropicVertex
        except ImportError as e:  # pragma: no cover
            raise ImportError("Install the google extra: uv add 'in-lockstep[google]'") from e

        return AsyncAnthropicVertex(
            project_id=settings.project_id,
            region=settings.region,
            timeout=settings.timeout_seconds,
            max_retries=0,
        )
