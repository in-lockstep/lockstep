"""Model transport. Every type here originates in this repository.

`LLMProvider.generate(LLMInput) -> LLMOutput` is the seam the whole AI layer is built on: one
method, one input type, one output type. Cassettes record at it, which is why a tape taken against
one provider replays against another; `ProviderRegistry` resolves to it per verb, which is what
makes per-verb model routing expressible.

A note on history, because the docstrings here used to lead with it and that was the wrong way
round. The shape of this package was informed by an earlier transport layer in another project of
the same author's, and the defect list that shaped it is recorded in ADR 0001 — blocking SDK calls
inside `async def`, retry classification by substring, an unpriced model silently charged at
another model's rate. Those are the reasons the code looks the way it does. They are not a
provenance claim: this is first-party code, held to this repository's formatting and to strict
mypy like everything else, and nothing here is kept in step with anything outside this tree.
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
