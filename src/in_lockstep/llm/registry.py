"""ProviderRegistry — the replacement for `resolver.get_provider(config)`.

`get_provider` selected ONE provider per process from an ambient `config.llm_provider`, and
`LLMInput.model` is an unqualified string with no provider prefix. The design's own headline
example needs two providers live at once:

    lockstep.models.route(Verb.TRIAGE,    "local:qwen3-8b")
    lockstep.models.route(Verb.IMPLEMENT, "anthropic:claude-sonnet-4-6")

That is inexpressible with a process-wide singleton, so the resolver is dropped and per-verb
routing becomes ModelRouter -> Model -> registry.provider_for(model).

Registration — not the provider class — carries `data_policy` and `endpoint`. One
OpenAI-compatible provider pointed at localhost and one pointed at a hosted endpoint are two
registrations with two policies; keying residency on the class would let an env var defeat it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .interface import Credentials, DataPolicy, LLMProvider, ProviderSettings

ProviderFactory = Callable[[ProviderSettings, Credentials], LLMProvider]


class ProviderRegistrationError(Exception):
    """A registration is malformed, duplicated, or names an unknown provider."""


@dataclass(frozen=True)
class Model:
    """A routable model. `id` is qualified: "anthropic:claude-sonnet-4-6", "local:qwen3-8b"."""

    id: str

    @property
    def provider(self) -> str:
        return self.id.split(":", 1)[0] if ":" in self.id else ""

    @property
    def name(self) -> str:
        return self.id.split(":", 1)[1] if ":" in self.id else self.id


@dataclass(frozen=True)
class ModelCaps:
    """What a registration declares its models can do, read where a call needs it.

    Declarations, not measurements: the operator who registers a provider says what its models
    honour, the way `data_policy` says where the bytes go and `free` says what they cost. Two of
    these are consulted before a call is made (#274, `GATE-MODEL-1`). `tool_use` is checked when a
    session hands the model tools, because a loop over tool calls cannot run against a model that
    does not make them. `structured_output` is checked when a call asks for its answer in a
    schema, which every shipped AI verb does: the framework puts the schema in the prompt and
    repairs the reply once rather than using a provider's native mode, so what the flag declares
    is not "has JSON mode" but "answers a schema when asked" -- and a registration that says it
    does not is refused by name before anything is sent, instead of being charged twice to find
    out.

    Both default to capable, deliberately. A registration that says nothing has not declared an
    incapacity, and a default of `False` would refuse every shipped verb for every adopter who
    registers a gateway without reading this class. `False` is a statement the operator makes on
    purpose, and it earns a refusal that names the model and the capability.

    `context_window` and `vision` are read by nothing yet, and are left declared rather than
    removed because the curator's token budget is the obvious reader of the first; the honest
    status is stated here rather than implied by their presence.
    """

    context_window: int = 0
    tool_use: bool = True
    structured_output: bool = True
    vision: bool = False


@dataclass
class Registration:
    name: str
    factory: ProviderFactory
    settings: ProviderSettings
    data_policy: DataPolicy
    #: Where the bytes go, as the operator declares it. `None` is the one other shape allowed: a
    #: destination the registration genuinely cannot state, with `endpoint_reason` saying why,
    #: which residency and the egress manifest then treat as unknown by name. An empty string is
    #: neither, and is refused at `register`: three shipped registrations carried `""` for as
    #: long as it was accepted, and the endpoint comparison had nothing to compare (#309).
    endpoint: str | None
    auth_target: str
    caps: ModelCaps = field(default_factory=ModelCaps)
    #: The operator's declaration that this destination bills nothing — a local runtime,
    #: typically. Pricing refuses to *guess* what a model costs, and this is not a guess: zero is
    #: the one rate the operator can state exactly by knowing where the bytes go. It lives on the
    #: registration for the same reason `data_policy` does — an env var pointing "local" at a
    #: hosted endpoint must not be able to make hosted tokens read as free.
    free: bool = False
    endpoint_reason: str = ""


class ProviderRegistry:
    """Name -> provider, constructed lazily and cached per (name, credential fingerprint)."""

    def __init__(self) -> None:
        self._registrations: dict[str, Registration] = {}
        self._cache: dict[tuple[str, int], LLMProvider] = {}

    def register(
        self,
        name: str,
        factory: ProviderFactory,
        *,
        settings: ProviderSettings,
        data_policy: DataPolicy,
        endpoint: str | None,
        auth_target: str = "",
        caps: ModelCaps | None = None,
        free: bool = False,
        endpoint_reason: str = "",
    ) -> None:
        if name in self._registrations:
            raise ProviderRegistrationError(f"provider {name!r} is already registered")
        if endpoint == "":
            raise ProviderRegistrationError(
                f"provider {name!r} registers an empty endpoint; declare where the bytes go, or pass "
                f"endpoint=None with endpoint_reason saying why the destination cannot be stated"
            )
        if endpoint is None and not endpoint_reason.strip():
            raise ProviderRegistrationError(
                f"provider {name!r} registers no endpoint and no reason; endpoint=None needs "
                f"endpoint_reason, so residency can refuse the unknown destination by name"
            )
        self._registrations[name] = Registration(
            name=name,
            factory=factory,
            settings=settings,
            data_policy=data_policy,
            endpoint=endpoint,
            auth_target=auth_target,
            caps=caps or ModelCaps(),
            free=free,
            endpoint_reason=endpoint_reason.strip() if endpoint is None else "",
        )

    def registration_for(self, model: Model) -> Registration:
        name = model.provider
        if not name:
            raise ProviderRegistrationError(f"model id {model.id!r} is unqualified; use '<provider>:<model>'")
        try:
            return self._registrations[name]
        except KeyError:
            known = ", ".join(sorted(self._registrations)) or "(none registered)"
            raise ProviderRegistrationError(
                f"no provider registered as {name!r}; registered: {known}"
            ) from None

    def data_policy_for(self, model: Model) -> DataPolicy:
        return self.registration_for(model).data_policy

    def provider_for(self, model: Model, creds: Credentials | None = None) -> LLMProvider:
        registration = self.registration_for(model)
        credentials = creds if creds is not None else Credentials.none()
        key = (registration.name, hash(credentials.secret_values()))
        provider = self._cache.get(key)
        if provider is None:
            provider = registration.factory(registration.settings, credentials)
            self._assert_endpoint_matches(registration, provider)
            self._cache[key] = provider
        return provider

    @staticmethod
    def _assert_endpoint_matches(registration: Registration, provider: LLMProvider) -> None:
        """GATE-AUTH-2 — a declaration must not silently diverge from the destination.

        `endpoint` is what residency policy keys on. If the constructed client dials somewhere
        else, the policy is describing a different system than the one being used.

        Compared for every registration that states an endpoint and every provider that reports
        a base URL. A registration with `endpoint=None` has declared it cannot state one, and is
        refused elsewhere by that reason; a provider whose `base_url()` is empty is a custom
        transport that never overrode it, and is left uncompared rather than refused, because an
        adopter's provider must not fail on a method the docs never asked them to write. The
        shipped transports all report what their client will dial (#309).
        """
        actual = provider.base_url()
        if actual and registration.endpoint and actual.rstrip("/") != registration.endpoint.rstrip("/"):
            raise ProviderRegistrationError(
                f"provider {registration.name!r} registered endpoint "
                f"{registration.endpoint!r} but dials {actual!r}"
            )

    def names(self) -> list[str]:
        return sorted(self._registrations)

    def endpoints(self) -> tuple[str, ...]:
        """Every registered destination, for the egress manifest when no routes narrow it."""
        return tuple(sorted({r.endpoint for r in self._registrations.values() if r.endpoint}))

    def unknown_endpoints(self) -> dict[str, str]:
        """The registrations that could not state a destination, by name, with their reason --
        so a manifest says which hosts it does not list rather than listing fewer (#309)."""
        return {
            name: r.endpoint_reason for name, r in sorted(self._registrations.items()) if r.endpoint is None
        }
