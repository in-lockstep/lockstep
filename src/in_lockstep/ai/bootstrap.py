"""Wiring a provider registry from settings and credentials.

Registration is where `data_policy` and `endpoint` live, so residency keys on where the bytes
actually go rather than on which class was instantiated. Two registrations of the same
OpenAI-compatible transport, one at localhost and one at a hosted endpoint, are two different
answers to "may this repository send code there".
"""

from __future__ import annotations

import os
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
    registry.register(
        "anthropic",
        lambda s, c: _anthropic(s, c),
        settings=ProviderSettings(
            base_url="",
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
        caps=ModelCaps(tool_use=True, structured_output=False),
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
    registry.register(
        "bedrock",
        lambda s, c: _bedrock(s, c),
        settings=ProviderSettings(
            region=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "")
        ),
        data_policy=DataPolicy.EXTERNAL,
        endpoint="",
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
    registry.register(
        "vertex",
        lambda s, c: _vertex(s, c),
        settings=ProviderSettings(project_id=gcp_project, region=gcp_region),
        data_policy=DataPolicy.EXTERNAL,
        endpoint="",
        auth_target=AuthTarget.MODEL_PROVIDER.value,
        caps=ModelCaps(context_window=200_000, tool_use=True, structured_output=True),
    )
    registry.register(
        "gemini",
        lambda s, c: _gemini(s, c),
        settings=ProviderSettings(project_id=gcp_project, region=gcp_region),
        data_policy=DataPolicy.EXTERNAL,
        endpoint="",
        auth_target=AuthTarget.MODEL_PROVIDER.value,
        caps=ModelCaps(context_window=1_000_000, tool_use=True, structured_output=True),
    )

    return registry


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
    return minting.credentials_for(
        AuthRequest(target=AuthTarget.MODEL_PROVIDER, name=provider, keys=("id_token",))
    )


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
        )

    return build
