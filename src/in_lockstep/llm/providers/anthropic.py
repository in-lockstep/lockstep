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
            "timeout": settings.timeout_seconds,
            # One retry layer only. The SDK ships DEFAULT_MAX_RETRIES = 2, which composed with the
            # upstream with_retry(max_retries=3) and a Retry middleware to ~48 HTTP attempts per
            # logical call. RetryPolicy owns retry; the SDK does not. (GATE-RETRY-2)
            "max_retries": 0,
        }
        # Which credential, in the same precedence `bootstrap.credentials_for` resolved: a static
        # key when one exists; a framework-minted OIDC token handed to the SDK's own jwt-bearer
        # exchange when federation is configured; and NOTHING when neither — an empty `api_key`
        # is not nothing to the SDK (any explicit credential argument suppresses its env chain),
        # and passing one here is what kept workload identity federation unreachable.
        api_key = creds.get("api_key")
        id_token = creds.get("id_token")
        if api_key:
            kwargs["api_key"] = api_key
        elif id_token:
            from anthropic.lib.credentials import WorkloadIdentityCredentials

            # The identifiers arrive through settings, never the environment (GATE-AUTH-1):
            # bootstrap read them at registration, the same road the workspace id travels.
            kwargs["credentials"] = WorkloadIdentityCredentials(
                identity_token_provider=lambda: id_token,
                federation_rule_id=settings.extra.get("federation-rule-id", ""),
                organization_id=settings.extra.get("federation-organization-id", ""),
                service_account_id=settings.extra.get("federation-service-account-id") or None,
                workspace_id=settings.extra.get("anthropic-workspace-id") or None,
            )
        # else: no credential argument at all, so the SDK's documented chain engages —
        # ANTHROPIC_API_KEY, then a profile, then ANTHROPIC_IDENTITY_TOKEN[_FILE] federation.
        if settings.base_url:
            kwargs["base_url"] = settings.base_url

        # An identity-linked key is scoped to a workspace, and the API refuses a request that does
        # not say which — with a 400 naming the header, which is better than most. It is an
        # identifier rather than a secret, so it travels in `ProviderSettings.extra` and not in
        # `Credentials`: putting it there would seed `Redact` with a workspace id and mask it out
        # of the error messages that mention it.
        headers = {
            name: value for name, value in settings.extra.items() if name.startswith("anthropic-") and value
        }
        if headers:
            kwargs["default_headers"] = headers
        return anthropic.AsyncAnthropic(**kwargs)
