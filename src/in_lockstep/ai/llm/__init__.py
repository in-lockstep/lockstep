"""Model transport, vendored one-way from pipeline-framework `src/llm/` at 6ac3cde.

`LLMProvider.generate(LLMInput) -> LLMOutput` is preserved byte-identically — that signature is the
substitution this pivot committed to. Everything above it (Model, ModelCaps, CostTable,
ModelRouter, Credentials, DataPolicy, ProviderRegistry) is lockstep-owned, because the vendored
resolver bound one provider per process and the design needs per-verb routing.

See VENDORED.md for provenance and the list of defects fixed on the way in.
"""

from __future__ import annotations

from .interface import (
    AuthenticationError,
    ContextLengthError,
    Credentials,
    DataPolicy,
    LLMError,
    LLMProvider,
    ModelNotFoundError,
    ProviderSettings,
    RateLimitError,
    SecretStr,
    TransientError,
)
from .registry import (
    Model,
    ModelCaps,
    ProviderFactory,
    ProviderRegistrationError,
    ProviderRegistry,
    Registration,
)
from .types import LLMInput, LLMOutput, Message, TokenUsage, ToolCall, ToolDefinition

__all__ = [
    "AuthenticationError",
    "ContextLengthError",
    "Credentials",
    "DataPolicy",
    "LLMError",
    "LLMInput",
    "LLMOutput",
    "LLMProvider",
    "Message",
    "Model",
    "ModelCaps",
    "ModelNotFoundError",
    "ProviderFactory",
    "ProviderRegistrationError",
    "ProviderRegistry",
    "ProviderSettings",
    "RateLimitError",
    "Registration",
    "SecretStr",
    "TokenUsage",
    "ToolCall",
    "ToolDefinition",
    "TransientError",
]
