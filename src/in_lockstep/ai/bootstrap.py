"""Wiring a provider registry from settings and credentials.

Registration is where `data_policy` and `endpoint` live, so residency keys on where the bytes
actually go rather than on which class was instantiated. Two registrations of the same
OpenAI-compatible transport, one at localhost and one at a hosted endpoint, are two different
answers to "may this repository send code there".
"""

from __future__ import annotations

import os

from ..llm.interface import Credentials, DataPolicy, LLMProvider, ProviderSettings
from ..llm.registry import Model, ModelCaps, ProviderRegistry

# Re-exported deliberately, not incidentally. `ai` is the only layer permitted to reach the
# transport — a claim `test_layering.py` and this package's docstring both
# make — and it was false, because `cli` imported `Model` and `LLMProvider` straight from `llm`
# for a type annotation and a constructor. Naming them here is what makes the claim true.
__all__ = ["LLMProvider", "Model", "ModelCaps", "ProviderRegistry", "credentials_for", "default_registry"]
from .auth import Auth, AuthRequest, AuthTarget

ANTHROPIC_ENDPOINT = "https://api.anthropic.com"


def _anthropic(settings: ProviderSettings, creds: Credentials) -> LLMProvider:
    from ..llm.providers.anthropic import AnthropicProvider

    return AnthropicProvider(settings, creds)


def _openai(settings: ProviderSettings, creds: Credentials) -> LLMProvider:
    from ..llm.providers.openai_compat import OpenAIProvider

    return OpenAIProvider(settings, creds)


def _ollama(settings: ProviderSettings, creds: Credentials) -> LLMProvider:
    from ..llm.providers.ollama import OllamaProvider

    return OllamaProvider(settings, creds)


def default_registry(auth: Auth | None = None) -> ProviderRegistry:
    """The zero-config set. A repository re-registers any of these in its own module."""
    auth = auth or Auth()
    registry = ProviderRegistry()

    # An identity-linked API key acts in a workspace, and the API requires the id. Read here
    # rather than demanded in `lockstep.py`, because it is per-developer rather than per-project:
    # two people on one repository authenticate into different workspaces.
    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID", "")
    registry.register(
        "anthropic",
        lambda s, c: _anthropic(s, c),
        settings=ProviderSettings(
            base_url="",
            timeout_seconds=600.0,
            extra={"anthropic-workspace-id": workspace} if workspace else {},
        ),
        data_policy=DataPolicy.EXTERNAL,
        endpoint=ANTHROPIC_ENDPOINT,
        auth_target=AuthTarget.MODEL_PROVIDER.value,
        caps=ModelCaps(context_window=200_000, tool_use=True, structured_output=True),
    )

    local_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    registry.register(
        "local",
        lambda s, c: _ollama(s, c),
        settings=ProviderSettings(base_url=local_url),
        data_policy=DataPolicy.INTERNAL,
        endpoint=local_url,
        auth_target=AuthTarget.MODEL_PROVIDER.value,
        caps=ModelCaps(tool_use=True, structured_output=False),
    )

    gateway = os.environ.get("OPENAI_API_URL", "")
    if gateway:
        registry.register(
            "gateway",
            lambda s, c: _openai(s, c),
            settings=ProviderSettings(base_url=gateway),
            # A gateway is only internal if the operator says its destination is. The endpoint is
            # recorded so the claim is at least auditable.
            data_policy=DataPolicy.UNKNOWN,
            endpoint=gateway,
            auth_target=AuthTarget.MODEL_PROVIDER.value,
        )

    return registry


class MissingCredential(Exception):
    """No credential could be resolved for a provider that needs one.

    Refused here rather than left to the SDK. Anthropic's client raises a `TypeError` reading
    "Could not resolve authentication method" from inside `messages.create` — accurate, and
    arriving as a forty-line traceback from a library the user did not call, after the budget
    check has already passed and the run looks like it is working. This is a setup step with one
    obvious remedy, and it should read like one.
    """


def credentials_for(auth: Auth, provider: str) -> Credentials:
    keys = ("api_key",)
    if provider == "local":
        return Credentials.none()
    creds = auth.credentials_for(
        AuthRequest(target=AuthTarget.MODEL_PROVIDER, name=provider, keys=keys)
    )
    if not creds.secret_values():
        var = f"{provider.upper().replace('-', '_')}_API_KEY"
        raise MissingCredential(
            f"no credential for provider {provider!r}. Set {var}, or bind a resolver that can "
            f"mint one — `Auth.chain` takes an OIDC resolver ahead of the environment, which is "
            f"the arrangement this framework prefers because a federated token is short-lived. "
            f"Nothing was sent and nothing was charged."
        )
    return creds
