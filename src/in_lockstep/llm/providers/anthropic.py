from __future__ import annotations

from typing import Any

from ..interface import Credentials, ProviderSettings
from ._claude_base import ClaudeTransport


class AnthropicProvider(ClaudeTransport):
    """Claude via the direct Anthropic API."""

    _name = "anthropic"

    def _make_client(self, settings: ProviderSettings, creds: Credentials) -> Any:
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - exercised by the extras story
            raise ImportError(
                "the anthropic provider needs its optional dependency. In your own project: "
                "uv add 'in-lockstep[anthropic]'. Working inside the in-lockstep repository "
                "itself, where the package is the project: uv sync --extra anthropic."
            ) from e

        kwargs: dict[str, Any] = {
            "api_key": creds.get("api_key"),
            "timeout": settings.timeout_seconds,
            # One retry layer only. The SDK ships DEFAULT_MAX_RETRIES = 2, which composed with the
            # upstream with_retry(max_retries=3) and a Retry middleware to ~48 HTTP attempts per
            # logical call. RetryPolicy owns retry; the SDK does not. (GATE-RETRY-2)
            "max_retries": 0,
        }
        if settings.base_url:
            kwargs["base_url"] = settings.base_url

        # An identity-linked key is scoped to a workspace, and the API refuses a request that does
        # not say which — with a 400 naming the header, which is better than most. It is an
        # identifier rather than a secret, so it travels in `ProviderSettings.extra` and not in
        # `Credentials`: putting it there would seed `Redact` with a workspace id and mask it out
        # of the error messages that mention it.
        headers = {
            name: value
            for name, value in settings.extra.items()
            if name.startswith("anthropic-") and value
        }
        if headers:
            kwargs["default_headers"] = headers
        return anthropic.AsyncAnthropic(**kwargs)
