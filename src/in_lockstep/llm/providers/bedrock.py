from __future__ import annotations

from typing import Any

from ..interface import Credentials, ProviderSettings
from ._claude_base import ClaudeTransport


class BedrockProvider(ClaudeTransport):
    """Claude via AWS Bedrock.

    Rides AsyncAnthropicBedrock rather than aioboto3 — the anthropic SDK ships the async Bedrock
    client, so the async rewrite costs a constructor swap here too.
    """

    _name = "bedrock"

    def _make_client(self, settings: ProviderSettings, creds: Credentials) -> Any:
        try:
            from anthropic import AsyncAnthropicBedrock
        except ImportError as e:  # pragma: no cover
            raise ImportError("Install the bedrock extra: uv add 'in-lockstep[bedrock]'") from e

        kwargs: dict[str, Any] = {"timeout": settings.timeout_seconds, "max_retries": 0}
        # Upstream constructed AnthropicBedrock() with no arguments at all and ignored config
        # entirely, so region and credentials came from the ambient AWS chain and Auth never saw
        # them. Passed explicitly when supplied, so the credentials seam holds (GATE-AUTH-1).
        if settings.region:
            kwargs["aws_region"] = settings.region
        if creds.get("access_key_id"):
            kwargs["aws_access_key"] = creds.get("access_key_id")
            kwargs["aws_secret_key"] = creds.get("secret_access_key")
            if creds.get("session_token"):
                kwargs["aws_session_token"] = creds.get("session_token")
        return AsyncAnthropicBedrock(**kwargs)
