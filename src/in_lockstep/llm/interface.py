"""The provider seam.

`generate(LLMInput) -> LLMOutput` is the one method a provider must implement. It is the
substitution the pivot committed to. What changed is *construction* — a provider is built by the
ProviderRegistry from an explicit (ProviderSettings, Credentials) pair rather than reading an
ambient Config.

That is the credentials seam. Auth mints the Credentials, retains the secret values to seed Redact
before returning, and hands them to the factory. Providers may not read os.environ (GATE-AUTH-1);
without that rule Auth never observes the secret and §5.2's "Redact seeded with the resolver's
known secret values" is unsatisfiable — which is what an env-scraping fallback cannot fix for an
OIDC- or vault-derived token that was never in the environment.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum

from .types import LLMInput, LLMOutput


class SecretStr:
    """A string that does not render itself. `.reveal()` is the only accessor."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __str__(self) -> str:
        return "***"

    __repr__ = __str__


class DataPolicy(Enum):
    """Residency classification. Keyed on the resolved destination, not the provider class.

    An OpenAI-compatible provider pointed at http://localhost:1234/v1 and one pointed at a hosted
    endpoint are two registrations with two policies — not one class with one label.
    """

    INTERNAL = "internal"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Credentials:
    """What Auth mints, retains for Redact seeding, and hands to a provider factory."""

    values: Mapping[str, SecretStr] = field(default_factory=dict)
    source: str = ""  # "env:ANTHROPIC_API_KEY" | "oidc:..." | "keychain" | "none"
    #: Re-mints these values, for a credential that expires sooner than a run finishes.
    #:
    #: A GitHub OIDC identity token lives minutes, and an implementing session now runs longer than
    #: that — so the token resolved at client construction is expired by the time the SDK tries to
    #: exchange it again, and the run dies at `provider.authentication` with the work half done and
    #: the money already spent. `values` is a snapshot; this is how to take another one.
    #:
    #: It must go back through `Auth`, never around it: minting is the only moment a secret is
    #: visible before a client swallows it, and therefore the only moment redaction can be seeded.
    #: A refresh that resolved a token directly would put an unredacted JWT into every error string
    #: for the rest of the run.
    #:
    #: `None` means these values are all this credential will ever have, which is the right answer
    #: for a long-lived API key.
    refresh: Callable[[], Credentials] | None = None

    @classmethod
    def none(cls) -> Credentials:
        return cls(values={}, source="none")

    def fresh(self) -> Credentials:
        """These credentials, re-minted if they know how. Never raises: a refresh that fails
        returns the snapshot, so the caller's fallback is the behaviour that existed before there
        was a refresh at all, rather than a new way to lose a run."""
        if self.refresh is None:
            return self
        try:
            renewed = self.refresh()
        except Exception:  # noqa: BLE001 - a stale credential beats an exception from a getter
            return self
        return renewed if renewed.secret_values() else self

    def get(self, key: str) -> str:
        secret = self.values.get(key)
        return secret.reveal() if secret else ""

    def secret_values(self) -> frozenset[str]:
        """Exactly what Redact is seeded with."""
        return frozenset(s.reveal() for s in self.values.values() if s.reveal())


@dataclass(frozen=True)
class ProviderSettings:
    """Non-secret provider configuration. Everything secret travels in Credentials."""

    model_default: str = ""
    base_url: str = ""
    project_id: str = ""
    region: str = ""
    timeout_seconds: float = 600.0
    extra: Mapping[str, str] = field(default_factory=dict)


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    # Whether calling this provider can put bytes on the network. False for a cassette or a
    # canned answer, and that is not a detail: egress control is mandatory when a run carries
    # untrusted content, and demanding a firewall for a run that cannot transmit would train
    # people to disable the control locally, which is how a control dies.
    transmits: bool = True

    @abstractmethod
    async def generate(self, input: LLMInput) -> LLMOutput:
        """Send a prompt to the LLM and return the response."""
        ...

    @abstractmethod
    def name(self) -> str:
        """Return the provider name."""
        ...

    def supports_tools(self) -> bool:
        """Whether this provider supports tool/function calling."""
        return True

    def base_url(self) -> str:
        """The destination this provider actually dials.

        GATE-AUTH-2 asserts this equals the registered endpoint, so a registration's data_policy
        cannot silently diverge from where the bytes go.
        """
        return ""


class LLMError(Exception):
    """Base exception for LLM errors."""

    def __init__(
        self, message: str, *, status_code: int | None = None, provider: str = "", request_id: str = ""
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.provider = provider
        self.request_id = request_id

    @property
    def retryable(self) -> bool:
        """ERRORED-class only. Overridden per subclass; never inferred from message text."""
        return False


class AuthenticationError(LLMError):
    """Authentication failed."""


class RateLimitError(LLMError):
    """Rate limit exceeded."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kw: object) -> None:
        super().__init__(message, **kw)  # type: ignore[arg-type]
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return True


class ContextLengthError(LLMError):
    """Input exceeds model context window.

    Declared upstream but never raised by any provider. It is raised here, and routes to a repair
    path rather than a retry — re-sending an over-long prompt is guaranteed to fail again.
    """


class ModelNotFoundError(LLMError):
    """Requested model not available."""


class TransientError(LLMError):
    """5xx, connection reset, read timeout — the ERRORED class §4.3 says Retry targets.

    Upstream `with_retry` retried ONLY RateLimitError, so exactly these got zero retries.
    """

    @property
    def retryable(self) -> bool:
        return True
