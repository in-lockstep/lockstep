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
# vendored transport — a claim `test_layering.py`, this package's docstring and VENDORED.md all
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

    registry.register(
        "anthropic",
        lambda s, c: _anthropic(s, c),
        settings=ProviderSettings(base_url="", timeout_seconds=600.0),
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


def credentials_for(auth: Auth, provider: str) -> Credentials:
    keys = ("api_key",)
    if provider == "local":
        return Credentials.none()
    return auth.credentials_for(AuthRequest(target=AuthTarget.MODEL_PROVIDER, name=provider, keys=keys))
