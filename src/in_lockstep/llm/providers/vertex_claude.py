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
            raise ImportError(
                "the google provider needs its optional dependency. In your own project: "
                "uv add 'in-lockstep[google]'. Working inside the in-lockstep repository "
                "itself, where the package is the project: uv sync --extra google."
            ) from e

        # Omit region/project when unset rather than passing "", the way `BedrockProvider` omits an
        # empty region. The SDK treats "" as an explicit value — it skips its own CLOUD_ML_REGION
        # fallback and its clean "No region was given" error, and builds a malformed
        # `-aiplatform.googleapis.com` host that fails later with an opaque connection error. Left
        # unset, the SDK reads its own environment and raises an actionable message if it cannot.
        kwargs: dict[str, Any] = {"timeout": settings.timeout_seconds, "max_retries": 0}
        if settings.region:
            kwargs["region"] = settings.region
        if settings.project_id:
            kwargs["project_id"] = settings.project_id
        return AsyncAnthropicVertex(**kwargs)
