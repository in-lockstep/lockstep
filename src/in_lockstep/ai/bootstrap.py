"""Wiring a provider registry from settings and credentials.

Registration is where `data_policy` and `endpoint` live, so residency keys on where the bytes
actually go rather than on which class was instantiated. Two registrations of the same
OpenAI-compatible transport, one at localhost and one at a hosted endpoint, are two different
answers to "may this repository send code there".
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ..llm.interface import Credentials, DataPolicy, LLMProvider, ProviderSettings
from ..llm.registry import Model, ModelCaps, ProviderRegistry

if TYPE_CHECKING:
    from .pricing import CostTable

# Re-exported deliberately, not incidentally. `ai` is the only layer permitted to reach the
# transport — a claim `test_layering.py` and this package's docstring both
# make — and it was false, because `cli` imported `Model` and `LLMProvider` straight from `llm`
# for a type annotation and a constructor. Naming them here is what makes the claim true.
__all__ = [
    "LLMProvider",
    "Model",
    "ModelCaps",
    "ProviderRegistry",
    "credentials_for",
    "default_registry",
    "table_for",
]
from .auth import Auth, AuthRequest, AuthTarget, OidcResolver

ANTHROPIC_ENDPOINT = "https://api.anthropic.com"


def bedrock_endpoint(region: str) -> str | None:
    """The host the Anthropic SDK's Bedrock client dials for a region, or None without one."""
    return f"https://bedrock-runtime.{region}.amazonaws.com" if region else None


def vertex_endpoint(region: str) -> str | None:
    """The host the Anthropic SDK's Vertex client dials for a region, or None without one.

    The SDK's own rule, restated so the declaration is what the client will dial: `global` and
    the two data-residency pseudo-regions have their own hosts.
    """
    if not region:
        return None
    if region == "global":
        return "https://aiplatform.googleapis.com"
    if region in ("us", "eu"):
        return f"https://aiplatform.{region}.rep.googleapis.com"
    return f"https://{region}-aiplatform.googleapis.com"


def _anthropic(settings: ProviderSettings, creds: Credentials) -> LLMProvider:
    from ..llm.providers.anthropic import AnthropicProvider

    return AnthropicProvider(settings, creds)


def _openai(settings: ProviderSettings, creds: Credentials) -> LLMProvider:
    from ..llm.providers.openai_compat import OpenAIProvider

    return OpenAIProvider(settings, creds)


def _ollama(settings: ProviderSettings, creds: Credentials) -> LLMProvider:
    from ..llm.providers.ollama import OllamaProvider

    return OllamaProvider(settings, creds)


def _bedrock(settings: ProviderSettings, creds: Credentials) -> LLMProvider:
    from ..llm.providers.bedrock import BedrockProvider

    return BedrockProvider(settings, creds)


def _vertex(settings: ProviderSettings, creds: Credentials) -> LLMProvider:
    from ..llm.providers.vertex_claude import VertexClaudeProvider

    return VertexClaudeProvider(settings, creds)


def _gemini(settings: ProviderSettings, creds: Credentials) -> LLMProvider:
    from ..llm.providers.google_gemini import GoogleGeminiProvider

    return GoogleGeminiProvider(settings, creds)


#: Workspace ids are tagged. The Anthropic SDK's own type says so: "Tagged workspace ID
#: (`wrkspc_...`)". Checked locally because the natural mistake is to use the workspace *name* —
#: "Default" is what the Console shows you — and paying a network round-trip to be told the
#: header is invalid teaches nothing about where the right value lives.
WORKSPACE_PREFIX = "wrkspc_"


def _is_local(url: str) -> bool:
    """Whether a URL's host is genuinely this machine — what makes a `local` registration `free`.

    The address decides, so an env var cannot launder a hosted endpoint into a zero rate. Loopback
    is asked of `ipaddress`, not a hand list: `127.0.0.1` is in it and so is the rest of
    `127.0.0.0/8` and `::1`, while `0.0.0.0` — the wildcard *bind* address, which a hosted service
    can answer on and is not loopback to reach — is correctly excluded. A hostname that is not an
    IP is local only when it is `localhost` or ends in `.localhost`.
    """
    import ipaddress
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _anthropic_workspace() -> str:
    """The configured workspace id, refused early if it is plainly a name.

    Narrow on purpose: it rejects only a value that does not carry the documented prefix, so a
    future format is a one-line change here rather than a mystery. Absent is fine — a key that is
    not identity-linked needs no workspace and gets no header.
    """
    value = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    if not value or value.startswith(WORKSPACE_PREFIX):
        return value
    raise MissingCredential(
        f"ANTHROPIC_WORKSPACE_ID is {value!r}, which looks like a workspace name rather than its "
        f"id. The id is tagged {WORKSPACE_PREFIX}… and appears in the Console URL when you open "
        f"the workspace: Settings -> Workspaces -> (pick one), then take the {WORKSPACE_PREFIX}… "
        f"segment from the address bar. Unset it entirely if your key is not identity-linked. "
        f"Nothing was sent and nothing was charged."
    )


#: Service-account ids are tagged, like workspace ids. Checked locally for the same reason:
#: the natural mistake is the account's NAME — which is what the Console displays — and the
#: token exchange refuses it with an HTTP 400 after a network round-trip that teaches nothing
#: about where the right value lives. This run proved it: `in-lockstep-gh-sa` went to the
#: exchange and came back `service_account_id: does not have prefix 'svac_'`.
SERVICE_ACCOUNT_PREFIX = "svac_"


def _anthropic_service_account() -> str:
    value = os.environ.get("ANTHROPIC_SERVICE_ACCOUNT_ID", "").strip()
    if not value or value.startswith(SERVICE_ACCOUNT_PREFIX):
        return value
    raise MissingCredential(
        f"ANTHROPIC_SERVICE_ACCOUNT_ID is {value!r}, which looks like a service-account name "
        f"rather than its id. The id is tagged {SERVICE_ACCOUNT_PREFIX}… and appears in the "
        f"Console when you open the service account. Unset it to let the federation rule "
        f"decide, or set the tagged id. Nothing was sent and nothing was charged."
    )


def default_registry(auth: Auth | None = None) -> ProviderRegistry:
    """The zero-config set. A repository re-registers any of these in its own module."""
    auth = auth or Auth()
    registry = ProviderRegistry()

    # An identity-linked API key acts in a workspace, and the API requires the id. Read here
    # rather than demanded in `lockstep.py`, because it is per-developer rather than per-project:
    # two people on one repository authenticate into different workspaces.
    workspace = _anthropic_workspace()
    # Workload identity federation identifiers travel the same way — in settings, read HERE,
    # because a provider never reads the environment (GATE-AUTH-1: credentials arrive through
    # the constructor, or Redact cannot be seeded; identifiers follow the same road so the rule
    # has no exceptions to remember). The `federation-` prefix keeps them out of the
    # `anthropic-*` header filter: they parameterise the token exchange, they are not headers.
    federation = {
        key: value
        for key, value in (
            ("federation-rule-id", os.environ.get("ANTHROPIC_FEDERATION_RULE_ID", "")),
            ("federation-organization-id", os.environ.get("ANTHROPIC_ORGANIZATION_ID", "")),
            ("federation-service-account-id", _anthropic_service_account()),
        )
        if value
    }
    # The base URL is resolved HERE, where the environment is already read, and handed to the
    # settings -- never left for the SDK to read `ANTHROPIC_BASE_URL` itself (GATE-AUTH-1). With
    # it left blank the SDK dialled whatever that variable said while the registration, residency
    # and the egress manifest all said `api.anthropic.com`, and the endpoint comparison had
    # nothing to compare. Now a proxy set through the environment is refused by name at
    # `provider_for`, and the way to use one is a registration that declares it (#309).
    anthropic_base = os.environ.get("ANTHROPIC_BASE_URL", "").strip() or ANTHROPIC_ENDPOINT
    registry.register(
        "anthropic",
        lambda s, c: _anthropic(s, c),
        settings=ProviderSettings(
            base_url=anthropic_base,
            timeout_seconds=600.0,
            extra={
                **({"anthropic-workspace-id": workspace} if workspace else {}),
                **federation,
            },
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
        # `structured_output` was `False` here for as long as the field meant a native JSON mode
        # and was read by nothing. It is read now, and it means "answers a schema when asked" --
        # which every shipped verb asks, `triage` included, and this repository routes its own
        # triage here (`local:qwen3-8b`, the $0 path `docs/getting-started.md` shows). A refusal
        # keyed on the old value would have refused the documented route. The registration covers
        # every Ollama model, so it cannot know which of them honour a schema; an operator who
        # knows theirs does not registers it under a name of their own with `False`.
        caps=ModelCaps(tool_use=True, structured_output=True),
        # Free only when the endpoint is genuinely local. `free` lets `--model local:qwen3-8b`
        # run without a cost-table entry — but pointing OLLAMA_URL at a hosted endpoint must not
        # make hosted tokens read as free, which is the exact invariant `Registration.free`
        # states. So the flag follows the address, not the provider name.
        free=_is_local(local_url),
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

    # Bedrock, Vertex and Gemini ship as provider classes but were reachable through no blessed
    # path — nothing registered them, so a route to `bedrock:…` refused as an unknown provider.
    # Registered here so a route resolves; the SDK is imported lazily at first use, so a repo that
    # never routes to one pays nothing and never needs its optional extra. Region and project come
    # from the cloud's own environment variables, the way those SDKs already expect them.
    #
    # A cloud model id is the cloud's, not the Anthropic API's (`us.anthropic.claude-…` on Bedrock,
    # an `@version` on Vertex), and pricing keys on the id — so such a route is unpriced until the
    # repository states its rate, which `doctor` refuses before the run spends anything. See
    # docs/extending.md. Gemini is the exception only because `gemini-2.5-pro` is both a Vertex id
    # and a shipped rate.
    # Each cloud endpoint is derived from the region the SDK will use, so residency and the
    # egress manifest name the host the bytes reach. Without a region there is no host to
    # name, and the registration says so rather than carrying an empty string the comparison
    # would skip: a restricted repository refuses the route by that reason (#309).
    aws_region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "")
    registry.register(
        "bedrock",
        lambda s, c: _bedrock(s, c),
        settings=ProviderSettings(region=aws_region),
        data_policy=DataPolicy.EXTERNAL,
        endpoint=bedrock_endpoint(aws_region),
        endpoint_reason="no AWS region is set (AWS_REGION or AWS_DEFAULT_REGION), so the Bedrock host "
        "cannot be stated",
        auth_target=AuthTarget.MODEL_PROVIDER.value,
        caps=ModelCaps(context_window=200_000, tool_use=True, structured_output=True),
    )
    gcp_project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    # Every spelling GCP tooling uses for the region, google-genai's own `GOOGLE_CLOUD_LOCATION`
    # included — a repository that set the documented variable must not silently get an empty one.
    gcp_region = (
        os.environ.get("GOOGLE_CLOUD_REGION")
        or os.environ.get("GOOGLE_CLOUD_LOCATION")
        or os.environ.get("CLOUD_ML_REGION", "")
    )
    gcp_reason = (
        "no GCP region is set (GOOGLE_CLOUD_REGION, GOOGLE_CLOUD_LOCATION or CLOUD_ML_REGION), so the "
        "Vertex host cannot be stated"
    )
    registry.register(
        "vertex",
        lambda s, c: _vertex(s, c),
        settings=ProviderSettings(project_id=gcp_project, region=gcp_region),
        data_policy=DataPolicy.EXTERNAL,
        endpoint=vertex_endpoint(gcp_region),
        endpoint_reason=gcp_reason,
        auth_target=AuthTarget.MODEL_PROVIDER.value,
        caps=ModelCaps(context_window=200_000, tool_use=True, structured_output=True),
    )
    # Gemini's client derives its host from the same region; google-genai does not expose the
    # URL it built, so the declaration is passed as the settings' base URL and reported back by
    # the provider. Declared, then, not read off the client -- the row says so.
    gemini_endpoint = vertex_endpoint(gcp_region)
    registry.register(
        "gemini",
        lambda s, c: _gemini(s, c),
        settings=ProviderSettings(project_id=gcp_project, region=gcp_region, base_url=gemini_endpoint or ""),
        data_policy=DataPolicy.EXTERNAL,
        endpoint=gemini_endpoint,
        endpoint_reason=gcp_reason,
        auth_target=AuthTarget.MODEL_PROVIDER.value,
        caps=ModelCaps(context_window=1_000_000, tool_use=True, structured_output=True),
    )

    return registry


def caps_for(registry: ProviderRegistry, model: Model) -> ModelCaps | None:
    """What the model's registration declares it can do, or `None` when nothing registers it.

    `None` rather than a refusal, for the same reason `table_for` tolerates the same absence one
    function down: an unregistered provider is refused where a provider is needed -- at
    `provider_for`, on a real call -- and a dry run or a replay that never asks for one must not
    fail on a lookup made for a check the invoker then skips. An undeclared capability is
    unchecked, and `AiInvoker.caps` says why that is not a control failing open.
    """
    from ..llm.registry import ProviderRegistrationError

    try:
        return registry.registration_for(model).caps
    except ProviderRegistrationError:
        return None


def table_for(registry: ProviderRegistry, model: Model, table: CostTable | None = None) -> CostTable:
    """The cost table for a run: the shipped rates, with any repository-bound rates layered over
    them, plus a zero rate for a model whose registration declares itself free.

    A bound table EXTENDS the default rather than replacing it. `default_table`'s docstring says a
    repository overrides rates "like any other binding", and a repository that binds a partial
    table means to add or change a few rates, not to unprice every shipped model — replacing the
    map would turn `--model anthropic:claude-opus-4-6` into an Unpriced refusal the moment a team
    priced one local finetune.

    Zero is the only rate this will ever invent. `CostTable.rate_for` keeps refusing any model
    nobody priced, because a guessed rate records a fabricated cost — but a `free` registration
    is not a guess, it is the operator stating where the bytes go. The tokens still land in
    `billed_tokens`, so a free run reads as free rather than as unmeasured.

    The result is always a fresh table: a table bound in the container is somebody's declaration,
    and pricing must not mutate it as a side effect of routing one verb to a local model.
    """
    from ..llm.registry import ProviderRegistrationError
    from .pricing import Rate, default_table

    merged = default_table()
    if table is not None:
        merged.rates.update(table.rates)  # bound rates win over shipped ones; the rest survive
    try:
        registration = registry.registration_for(model)
    except ProviderRegistrationError:
        # Not this function's error to raise. A dry run or a replay never constructs the
        # provider, and a live run fails at `provider_for` with the message that names the fix —
        # failing here instead would make pricing the thing that refuses an unknown provider.
        return merged
    if registration.free and not merged.knows(model.name):
        merged.add(model.name, Rate(0.0, 0.0, cache_read_per_m=0.0, cache_write_per_m=0.0))
    return merged


class MissingCredential(Exception):
    """No credential could be resolved for a provider that needs one.

    Refused here rather than left to the SDK. Anthropic's client raises a `TypeError` reading
    "Could not resolve authentication method" from inside `messages.create` — accurate, and
    arriving as a forty-line traceback from a library the user did not call, after the budget
    check has already passed and the run looks like it is working. This is a setup step with one
    obvious remedy, and it should read like one.
    """


#: The credential keys each provider takes through `Auth`. An empty tuple means it authenticates
#: entirely through its cloud's ambient chain — `local` needs nothing on-host, Vertex and Gemini
#: ride GCP application-default credentials. Bedrock lists AWS keys, because supplying them through
#: `Auth` is what seeds `Redact` and reaches the provider's explicit-key path; absent, the empty
#: credential falls it back to the ambient AWS chain, which the framework cannot see or redact —
#: the documented caveat of cloud ambient auth. A provider not named here takes an API key.
_CLOUD_KEYS: dict[str, tuple[str, ...]] = {
    "local": (),
    "vertex": (),
    "gemini": (),
    "bedrock": ("access_key_id", "secret_access_key", "session_token"),
}


#: The audience the GitHub JWT is minted for when it will be exchanged at Anthropic's token
#: endpoint. Has to match what the federation rule in the Console expects; overridable for a
#: deployment whose rule was configured against a different value.
ANTHROPIC_FEDERATION_AUDIENCE = "https://api.anthropic.com"


def _anthropic_federation_configured() -> bool:
    """Whether workload identity federation is set up for the Anthropic provider.

    The rule id and organisation id are the SDK's required pair (its credential chain returns
    None without both), and they are identifiers rather than secrets — which is why they live in
    plain CI env, and why checking them here leaks nothing.
    """
    return bool(
        os.environ.get("ANTHROPIC_FEDERATION_RULE_ID") and os.environ.get("ANTHROPIC_ORGANIZATION_ID")
    )


def _federation_credentials(auth: Auth, provider: str) -> Credentials:
    """A short-lived GitHub OIDC token for the SDK to exchange, or the signal to let the SDK's
    own chain do everything.

    An operator who already supplies `ANTHROPIC_IDENTITY_TOKEN[_FILE]` gets `Credentials.none()`
    — the same empty-is-ambient signal the cloud providers use — and the SDK reads, exchanges
    and caches on its own. Otherwise the token is minted here, through the same `OidcResolver`
    the default chain carries, but with the audience the federation rule expects rather than the
    chain default: an audience is part of what the rule validates, and a token minted for the
    wrong one is refused at the exchange. Minted through `Auth`, not around it, so the JWT is
    seeded into redaction before anything can render it.
    """
    if os.environ.get("ANTHROPIC_IDENTITY_TOKEN") or os.environ.get("ANTHROPIC_IDENTITY_TOKEN_FILE"):
        return Credentials.none()
    minting = Auth.chain(OidcResolver(audience=ANTHROPIC_FEDERATION_AUDIENCE), registry=auth.registry)
    request = AuthRequest(target=AuthTarget.MODEL_PROVIDER, name=provider, keys=("id_token",))

    def mint() -> Credentials:
        """A fresh JWT from GitHub, seeded into redaction on the way past.

        `ACTIONS_ID_TOKEN_REQUEST_URL` stays callable for the life of the job, so this is a real
        source of new tokens rather than a cache read — which is what the SDK's
        `identity_token_provider` needs it to be. The token itself lives minutes; a session now
        outlives it, and the run that discovered that had spent $33.80 by the time the exchange
        was refused.
        """
        return minting.credentials_for(request)

    # `refresh=mint` rather than `mint` being called once and captured: the point is that the
    # provider can ask again later, not that this function has a helper.
    return replace(mint(), refresh=mint)


def credentials_for(auth: Auth, provider: str) -> Credentials:
    if provider in _CLOUD_KEYS:
        keys = _CLOUD_KEYS[provider]
        if not keys:
            return Credentials.none()
        # No `MissingCredential`: an empty result is not an error here, it is the signal to use the
        # cloud's ambient chain. Where the keys ARE wired through Auth, they come back seeded.
        return auth.credentials_for(AuthRequest(target=AuthTarget.MODEL_PROVIDER, name=provider, keys=keys))
    creds = auth.credentials_for(
        AuthRequest(target=AuthTarget.MODEL_PROVIDER, name=provider, keys=("api_key",))
    )
    if not creds.secret_values() and provider == "anthropic" and _anthropic_federation_configured():
        # No long-lived key, but federation is configured: a short-lived token minted per run is
        # the arrangement this framework prefers, so its absence-of-a-key path runs BEFORE the
        # refusal. A static ANTHROPIC_API_KEY still wins when both are present — same precedence
        # the SDK documents — because an explicitly set key is somebody meaning it.
        return _federation_credentials(auth, provider)
    if not creds.secret_values():
        var = f"{provider.upper().replace('-', '_')}_API_KEY"
        raise MissingCredential(
            f"no credential for provider {provider!r}. Set {var}, or bind a resolver that can "
            f"mint one — `Auth.chain` takes an OIDC resolver ahead of the environment, which is "
            f"the arrangement this framework prefers because a federated token is short-lived. "
            f"For Anthropic, workload identity federation also works: set "
            f"ANTHROPIC_FEDERATION_RULE_ID and ANTHROPIC_ORGANIZATION_ID (plus id-token: write "
            f"on GitHub Actions) and no key is needed at all. "
            f"Nothing was sent and nothing was charged."
        )
    return creds


class MissingModelRoute(LookupError):
    """An AI adapter ran with no explicit invoker and no model routed for its verb."""


def recorded(provider: Any, log: Any) -> Any:
    """`provider`, wrapped to keep what it is paid for. Unchanged when the run keeps nothing.

    O4's sentence is *every* model call, and this is the one function that makes a call kept. It
    is called from two places for one reason: the seam below covers a provider the framework
    built, and `resolve_invoker` covers the one an adapter built for itself with its own
    `invoker_factory=`. Two wrap sites, one rule — a second spelling of the rule is how the first
    hole got there.

    Idempotent, because both callers can be on the path for one run: the CLI hands some verbs a
    factory that already wrapped, and wrapping twice would write every inference to the tape
    twice and tell the ledger a run made double the calls it made.
    """
    if log is None:
        return provider
    from .replay import Cassette, RecordingProvider

    if isinstance(provider, RecordingProvider):
        return provider
    if getattr(provider, "transmits", True) is False:
        # A replay has nothing to record. Recording one would write a tape identical to the tape
        # being read and tell the ledger that inferences were kept when none were made -- the
        # fabrication `_one_provider` refuses one layer up, at the flag.
        raise ValueError(
            f"{provider.name()} serves from a recording, so there is nothing to record. "
            f"Drop --record, or drop the provider this module binds."
        )
    if not isinstance(log, Cassette):  # pragma: no cover - defensive, `run` builds it
        raise TypeError(f"a recording must be a Cassette, not {type(log).__name__}")
    return RecordingProvider(provider, log)


def routed_model(models: Mapping[str, str], verb: str, aspect: str = "") -> str:
    """The model id a route table names for a verb, or for one lens of it. Empty when neither.

    `review/security` before `review`: a route keyed on the lens is the more specific declaration
    and wins, and a lens with no route of its own takes the verb's, so per-lens routing costs a
    repository nothing until it writes a line (#204). One function for both readers -- the CLI's
    `--model` default and `routed_invoker` below -- because two spellings of "which key wins" is
    how a repository's `ls` and its run come to disagree about the model.

    The key is `verb/aspect` and not a second table, so `ls` prints lens routes beside verb routes
    under one heading and can flag a route to a lens nothing binds the same way it flags a route
    to a verb nothing serves.
    """
    if aspect:
        specific = str(models.get(f"{verb}/{aspect}", "") or "")
        if specific:
            return specific
    return str(models.get(verb, "") or "")


def routed_invoker(verb: Any, *, aspect: str = "") -> Any:
    """A `Callable[[ctx], AiInvoker]` that reads the model route off the run context.

    The default every AI adapter falls back to when no `invoker_factory=` was passed: the model
    comes from `lockstep.models.route(<verb>, ...)` — snapshotted onto `RunContext.models` at
    `context()` time, so route lines may appear before or after the bind — and egress from the
    bound `EgressPolicy`, via `invoker_factory`'s own lazy resolution. An explicit
    `invoker_factory=` remains the seam for a custom `ProviderRegistry` or provider.

    `aspect` names the lens when the verb has them, and `routed_model` says which key wins.
    """
    key = getattr(verb, "value", str(verb))

    def build(ctx: Any) -> Any:
        model_id = routed_model(getattr(ctx, "models", None) or {}, key, aspect)
        if not model_id:
            keys = f'"{key}/{aspect}" or "{key}"' if aspect else f'"{key}"'
            raise MissingModelRoute(
                f"no model routed for {key!r}: add `lockstep.models.route({keys}, ...)` to "
                f"lockstep.py, or pass `invoker_factory=` to the adapter. Nothing was sent and "
                f"nothing was charged."
            )
        return invoker_factory(model_id)(ctx)

    return build


def invoker_factory(
    model_id: str,
    *,
    egress: Any = None,
    cost_table: Any = None,
    auth: Auth | None = None,
    provider: Any = None,
    redact: Any = None,
    registry: ProviderRegistry | None = None,
) -> Any:
    """A `Callable[[ctx], AiInvoker]`, which is what every AI adapter takes.

    This exists because binding an AI verb in `lockstep.py` otherwise meant hand-assembling `Auth`,
    a `ProviderRegistry`, a `CostTable`, a `Model` and an `AiInvoker` — about thirty lines of
    construction in the file whose whole purpose is being readable. Configuration that costs thirty
    lines of boilerplate is configuration people move back into YAML, which is the failure this
    framework exists on the other side of.

    `provider` overrides the resolved one, which is how `--offline`, `--record` and `--dry-run`
    swap in a cassette without a second construction path.

    `registry` is the seam for a repository that runs its own gateway or a provider the default set
    does not ship: build one with `default_registry()`, register into it — a gateway with
    `DataPolicy.INTERNAL` stated *in code* rather than inferred from an env var, say — and pass it
    here. Without it the default set is used, which now reaches Bedrock, Vertex and Gemini too.

    The credential is resolved per call rather than at construction: a factory built at import time
    in `lockstep.py` must not read a secret while the module is merely being inspected by `ls`.
    """
    from ..privileged.egress import EgressPolicy
    from ..privileged.redact import Redact
    from .invoker import AiInvoker

    issuer = auth or Auth()
    registry = registry if registry is not None else default_registry(issuer)
    selected = Model(model_id)
    table = table_for(registry, selected, cost_table)

    def build(ctx: Any) -> AiInvoker:
        # Egress is resolved at build time, not at construction, so a module can bind
        # `EgressPolicy` AFTER calling `invoker_factory` and still have the binding reach the
        # adapter — which is what the scaffold's commented-out `UnsandboxedEgress` opt-out relies
        # on. An explicit `egress=` still wins (the dogfood passes the object it also binds), and
        # a run with neither falls back to the environment, refusing when it is unenforced.
        if egress is not None:
            policy = egress
        else:
            container = getattr(ctx, "container", None)
            policy = (
                container.resolve(EgressPolicy)
                if container is not None and container.has(EgressPolicy)
                else EgressPolicy.detect()
            )
        chosen = provider
        if chosen is None:
            chosen = registry.provider_for(selected, credentials_for(issuer, selected.provider))
        # Recording, when the run asked for it. Attached HERE and not in the CLI, because an
        # adapter reached through `in-lockstep run` builds its own invoker — `routed_invoker` calls
        # this factory with no `provider=`, so the CLI has no object to wrap. That is why
        # `implement` and `fix` recorded nothing while `review` recorded: not a policy, a seam.
        #
        # The run context carries it for the same reason it carries the transcript writer four
        # lines down: one sink for the whole run, keyed on the run rather than on whichever
        # invoker happened to be built first.
        chosen = recorded(chosen, getattr(ctx, "recording", None))
        # A per-turn transcript for every session this run makes. Keyed on the run id, so a
        # failed session's evidence is findable from its ledger record; absent when the context
        # has no run id, which is a test's hand-built context rather than a real run.
        from .transcript import TranscriptWriter

        run_id = str(getattr(ctx, "run_id", "") or "")
        return AiInvoker(
            chosen,
            model=selected.name,
            cost_table=table,
            spend=ctx.spend,
            redact=redact or Redact(),
            egress=policy,
            transcript=TranscriptWriter(run_id) if run_id else None,
            # From the registration, which is where residency keys on where the bytes go
            # (GATE-RESIDENCY-1). Resolved per model, so a local Ollama route stays INTERNAL
            # while a hosted route through the same lockstep.py is EXTERNAL.
            data_policy=registry.data_policy_for(selected),
            # And what the registration says its models can do, so a call that needs a schema or
            # hands tools is refused by name before the first turn (GATE-MODEL-1).
            caps=caps_for(registry, selected),
            # A registration that could not state its destination says why, and a restricted
            # repository refuses the route by that reason rather than reading absence as fine.
            destination_unknown=registry.registration_for(selected).endpoint_reason,
        )

    return build
