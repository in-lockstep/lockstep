"""Tests for OidcResolver wiring into the default Auth chain (ticket #89).

Each test captures one acceptance criterion:
  1. Default Auth chain includes OidcResolver before EnvResolver.
  2. OidcResolver respects request.keys — only answers id_token requests.
  3. Outside CI, env-var resolution is unchanged.
  4. Minted id_token is seeded into the redaction registry.
  5. The token value cannot reach a rendered string.
  6. No network is touched — the OIDC endpoint is faked.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from in_lockstep.ai.auth import Auth, AuthRequest, AuthTarget, EnvResolver, OidcResolver
from in_lockstep.llm.interface import Credentials, ProviderSettings
from in_lockstep.privileged.redact import Redact, SecretRegistry

# ---------------------------------------------------------------------------
# AC-1: default chain is [OidcResolver(), EnvResolver()]
# ---------------------------------------------------------------------------


def test_default_auth_chain_starts_with_oidc_resolver() -> None:
    """The default Auth() must place OidcResolver before EnvResolver."""
    auth = Auth()
    assert len(auth.resolvers) == 2, f"expected 2 resolvers, got {len(auth.resolvers)}"
    assert isinstance(auth.resolvers[0], OidcResolver), (
        f"first resolver should be OidcResolver, got {type(auth.resolvers[0]).__name__}"
    )
    assert isinstance(auth.resolvers[1], EnvResolver), (
        f"second resolver should be EnvResolver, got {type(auth.resolvers[1]).__name__}"
    )


# ---------------------------------------------------------------------------
# AC-2: OidcResolver respects request.keys — only answers id_token
# ---------------------------------------------------------------------------


def _fake_oidc_opener(token_value: str = "oidc-test-token-value-1234") -> Callable[..., BytesIO]:
    """Return a callable that fakes urllib.request.urlopen for the OIDC endpoint. A `BytesIO` is
    already the context manager `with urlopen(...) as resp` needs; the dunders this used to set
    on the instance were never consulted, because special-method lookup goes to the type."""

    def opener(req: object, *, timeout: float = 10) -> BytesIO:
        return BytesIO(json.dumps({"value": token_value}).encode())

    return opener


def test_oidc_resolver_ignores_api_key_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider wanting api_key must get {} so EnvResolver can answer instead."""
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://fake.actions.url/token")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "gha-request-token")

    resolver = OidcResolver()
    # Fake the network call — the resolver should never reach it for an api_key request,
    # but if it does the fake prevents real network access.
    monkeypatch.setattr("urllib.request.urlopen", _fake_oidc_opener())

    request = AuthRequest(target=AuthTarget.MODEL_PROVIDER, name="anthropic", keys=("api_key",))
    result = resolver.resolve(request)

    assert result == {}, f"OidcResolver should return {{}} for keys=('api_key',), got {result}"


def test_oidc_resolver_answers_id_token_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """When id_token is in request.keys and CI vars are set, the resolver should answer."""
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://fake.actions.url/token")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "gha-request-token")

    resolver = OidcResolver()
    monkeypatch.setattr("urllib.request.urlopen", _fake_oidc_opener("my-oidc-jwt-token-abcdef"))

    request = AuthRequest(target=AuthTarget.MODEL_PROVIDER, name="federation", keys=("id_token",))
    result = resolver.resolve(request)

    assert result == {"id_token": "my-oidc-jwt-token-abcdef"}


# ---------------------------------------------------------------------------
# AC-3: Outside CI, env-var resolution behaves exactly as before
# ---------------------------------------------------------------------------


def test_env_resolution_unchanged_outside_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without OIDC env vars, OidcResolver returns {} and EnvResolver resolves as before."""
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL", raising=False)
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-12345678")

    registry = SecretRegistry()
    auth = Auth(registry=registry)

    request = AuthRequest(target=AuthTarget.MODEL_PROVIDER, name="anthropic", keys=("api_key",))
    creds = auth.credentials_for(request)

    assert creds.get("api_key") == "sk-ant-test-key-12345678"
    assert creds.source == "EnvResolver:anthropic"


# ---------------------------------------------------------------------------
# AC-4: Minted id_token is seeded into the redaction registry
# ---------------------------------------------------------------------------


def test_oidc_token_seeded_into_redaction_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The id_token must appear in the registry before credentials_for returns."""
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://fake.actions.url/token")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "gha-request-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    token_value = "oidc-jwt-token-for-redaction-test"
    monkeypatch.setattr("urllib.request.urlopen", _fake_oidc_opener(token_value))

    registry = SecretRegistry()
    auth = Auth(registry=registry)

    request = AuthRequest(target=AuthTarget.MODEL_PROVIDER, name="federation", keys=("id_token",))
    creds = auth.credentials_for(request)

    assert token_value in registry.known(), "id_token must be seeded into the redaction registry"
    assert creds.get("id_token") == token_value


# ---------------------------------------------------------------------------
# AC-5: Token value cannot reach a rendered string
# ---------------------------------------------------------------------------


def test_oidc_token_is_redacted_in_rendered_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """A minted OIDC token must be masked by Redact — it never appears in rendered output."""
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://fake.actions.url/token")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "gha-request-token")

    token_value = "oidc-jwt-token-must-not-leak-ever"
    monkeypatch.setattr("urllib.request.urlopen", _fake_oidc_opener(token_value))

    registry = SecretRegistry()
    auth = Auth(registry=registry)

    request = AuthRequest(target=AuthTarget.MODEL_PROVIDER, name="federation", keys=("id_token",))
    auth.credentials_for(request)

    redact = Redact(registry)
    rendered = redact.text(f"the token is {token_value} and should not appear")
    assert token_value not in rendered, "token must be masked by the redactor"
    assert "***" in rendered


# ---------------------------------------------------------------------------
# AC-2 (supplement): on CI, api_key request falls through to EnvResolver
# ---------------------------------------------------------------------------


def test_api_key_falls_through_to_env_on_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even on CI with OIDC vars set, an api_key request must be resolved by EnvResolver."""
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://fake.actions.url/token")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "gha-request-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-ci-key-87654321")

    monkeypatch.setattr("urllib.request.urlopen", _fake_oidc_opener())

    registry = SecretRegistry()
    auth = Auth(registry=registry)

    request = AuthRequest(target=AuthTarget.MODEL_PROVIDER, name="anthropic", keys=("api_key",))
    creds = auth.credentials_for(request)

    assert creds.get("api_key") == "sk-ant-ci-key-87654321"
    assert creds.source == "EnvResolver:anthropic"


# ---------------------------------------------------------------------------
# Anthropic workload identity federation: no ANTHROPIC_API_KEY needed at all
# ---------------------------------------------------------------------------


def _federation_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_FEDERATION_RULE_ID", "fdrl_test")
    monkeypatch.setenv("ANTHROPIC_ORGANIZATION_ID", "org-test")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://fake.actions.url/token?x=1")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "gha-request-token")


def test_federation_mints_a_token_with_the_rules_audience_when_no_key_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ANTHROPIC_API_KEY, federation configured: `credentials_for` mints the GitHub JWT itself
    — through Auth, so it is seeded into redaction — and with the audience the federation rule
    validates, not the chain default."""
    from in_lockstep.ai.bootstrap import credentials_for

    _federation_env(monkeypatch)
    seen: dict[str, str] = {}

    def opener(req: Any, *, timeout: float = 10) -> BytesIO:
        seen["url"] = req.full_url
        return BytesIO(json.dumps({"value": "gha-jwt-for-anthropic"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", opener)
    registry = SecretRegistry()
    creds = credentials_for(Auth(registry=registry), "anthropic")

    assert creds.get("id_token") == "gha-jwt-for-anthropic"
    assert "audience=https://api.anthropic.com" in seen["url"], (
        "the audience is part of what the federation rule validates; the chain default "
        "would be refused at the exchange"
    )
    assert "gha-jwt-for-anthropic" in registry.known(), "minted through Auth, so redaction saw it"


def test_an_operator_supplied_token_file_defers_to_the_sdk_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """ANTHROPIC_IDENTITY_TOKEN_FILE already set means somebody wired their own supply; the
    framework mints nothing and returns the same empty-is-ambient signal the cloud providers
    use, so the SDK's documented chain does the reading, the exchange and the caching."""
    from in_lockstep.ai.bootstrap import credentials_for

    _federation_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_IDENTITY_TOKEN_FILE", "/tmp/gha-jwt")

    def opener(req, *, timeout=10):  # pragma: no cover - must not be reached
        raise AssertionError("nothing should be minted when the operator supplies the token")

    monkeypatch.setattr("urllib.request.urlopen", opener)
    creds = credentials_for(Auth(registry=SecretRegistry()), "anthropic")
    assert not creds.secret_values()


def test_a_static_key_still_wins_over_configured_federation(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicitly set key is somebody meaning it — the same precedence the SDK documents."""
    from in_lockstep.ai.bootstrap import credentials_for

    _federation_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static-key-11112222")
    creds = credentials_for(Auth(registry=SecretRegistry()), "anthropic")
    assert creds.get("api_key") == "sk-ant-static-key-11112222"
    assert creds.get("id_token") == ""


def test_the_refusal_now_names_the_federation_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from in_lockstep.ai.bootstrap import MissingCredential, credentials_for

    for var in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(MissingCredential, match="ANTHROPIC_FEDERATION_RULE_ID"):
        credentials_for(Auth(registry=SecretRegistry()), "anthropic")


# ---------------------------------------------------------------------------
# The provider hands the minted token to the SDK's exchange — and passes NO
# credential argument when it has none, so the SDK's own chain can engage
# ---------------------------------------------------------------------------


def _client_kwargs(
    monkeypatch: pytest.MonkeyPatch, creds: Credentials, settings: ProviderSettings | None = None
) -> dict[str, Any]:
    anthropic = pytest.importorskip("anthropic", reason="client tests need the provider extra")

    from in_lockstep.llm.interface import ProviderSettings
    from in_lockstep.llm.providers.anthropic import AnthropicProvider

    settings = settings or ProviderSettings()
    captured: dict[str, Any] = {}

    class _Capture:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Capture)
    AnthropicProvider(settings, creds)._make_client(settings, creds)
    return captured


def test_the_client_gets_the_federation_credentials_object(monkeypatch: pytest.MonkeyPatch) -> None:
    """The identifiers arrive through settings — the provider reads no environment
    (GATE-AUTH-1) — and parameterise the SDK's own token exchange."""
    credentials_lib = pytest.importorskip("anthropic.lib.credentials")
    WorkloadIdentityCredentials = credentials_lib.WorkloadIdentityCredentials

    from in_lockstep.llm.interface import Credentials, ProviderSettings, SecretStr

    creds = Credentials(values={"id_token": SecretStr("gha-jwt-abc")})
    settings = ProviderSettings(
        extra={"federation-rule-id": "fdrl_test", "federation-organization-id": "org-test"}
    )
    kwargs = _client_kwargs(monkeypatch, creds, settings)
    assert "api_key" not in kwargs
    provider = kwargs["credentials"]
    assert isinstance(provider, WorkloadIdentityCredentials)
    assert provider._federation_rule_id == "fdrl_test"
    assert provider._identity_token_provider() == "gha-jwt-abc"
    headers = kwargs.get("default_headers", {})
    assert "federation-rule-id" not in headers, "an exchange parameter is not a request header"


def test_an_empty_credential_suppresses_the_sdks_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """GATE-AUTH-1 — an empty credential must suppress the SDK's ambient chain.

    The previous test (`test_an_empty_credential_passes_no_credential_argument_at_all`) asserted
    that no credential kwarg reached the SDK at all, which is what ALLOWED it to read
    ANTHROPIC_API_KEY and ~/.anthropic from the environment.  This test asserts the opposite: an
    explicit `credentials` provider that holds nothing MUST be passed, so the SDK's
    `has_explicit_credential` is true and neither `os.environ` nor `default_credentials()` is
    consulted.  The credential provider must name what was missing when called.
    """
    from in_lockstep.llm.interface import Credentials

    kwargs = _client_kwargs(monkeypatch, Credentials.none())
    # An explicit `credentials` kwarg must be present — it is what suppresses the chain.
    assert "api_key" not in kwargs, "no api_key should be passed for an empty credential"
    assert "credentials" in kwargs, (
        "an empty credential must pass an explicit `credentials` provider to suppress "
        "the SDK's ambient chain (ANTHROPIC_API_KEY, ~/.anthropic config)"
    )
    # The provider must fail naming what is missing when called.
    provider = kwargs["credentials"]
    with pytest.raises(RuntimeError, match="(?i)credential"):
        provider(force_refresh=False)


def test_a_planted_env_key_does_not_reach_a_client_built_with_no_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GATE-AUTH-1 — the concrete defect.

    On main today, constructing with Credentials.none() lets the SDK fall through to
    ANTHROPIC_API_KEY in the environment.  After the fix, the client must hold no key even when
    one is planted.
    """
    pytest.importorskip("anthropic", reason="needs the SDK to construct a real client")
    from in_lockstep.llm.interface import Credentials, ProviderSettings
    from in_lockstep.llm.providers.anthropic import AnthropicProvider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-planted-by-the-environment")
    settings = ProviderSettings()
    client = AnthropicProvider(settings, Credentials.none())._make_client(settings, Credentials.none())
    assert client.api_key is None, (
        f"the client holds {client.api_key!r}: construction with Credentials.none() must suppress "
        f"the SDK's chain so the provider never reaches past its arguments"
    )


def test_a_static_key_reaches_the_client_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    from in_lockstep.llm.interface import Credentials, SecretStr

    kwargs = _client_kwargs(monkeypatch, Credentials(values={"api_key": SecretStr("sk-ant-x-12345678")}))
    assert kwargs["api_key"] == "sk-ant-x-12345678"
    assert "credentials" not in kwargs


def test_a_service_account_name_is_refused_before_any_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    """The name-vs-id trap, caught locally: the Console shows the NAME, the exchange wants the
    svac_-tagged id, and this run's own HTTP 400 is the round-trip this guard replaces."""
    from in_lockstep.ai.bootstrap import MissingCredential, default_registry

    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    monkeypatch.setenv("ANTHROPIC_SERVICE_ACCOUNT_ID", "in-lockstep-gh-sa")
    with pytest.raises(MissingCredential, match="svac_"):
        default_registry(Auth())

    monkeypatch.setenv("ANTHROPIC_SERVICE_ACCOUNT_ID", "svac_0123abc")
    default_registry(Auth())


# -- a token that outlives its own run ---------------------------------------------------------


def test_the_identity_token_is_minted_again_rather_than_replayed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK re-runs the exchange as its access token nears expiry, and a GitHub OIDC JWT lives
    minutes — so what `identity_token_provider` returns the second time has to be a NEW token.

    It was `lambda: id_token`, closed over one string resolved at client construction. Every
    refresh replayed the same, by-then-expired JWT and came back 401. Run 33569602761 died there
    at eight minutes in with $33.80 spent, and the error text — "Ensure your federation rule
    matches your identity token" — sends you to look at the rule, which was fine.
    """
    from in_lockstep.llm.interface import Credentials, ProviderSettings, SecretStr

    minted = iter(["jwt-first", "jwt-second", "jwt-third"])

    def mint() -> Credentials:
        return Credentials(values={"id_token": SecretStr(next(minted))}, source="oidc:anthropic")

    creds = Credentials(values={"id_token": SecretStr("jwt-zeroth")}, source="oidc:anthropic", refresh=mint)
    settings = ProviderSettings(extra={"federation-rule-id": "fdrl_test"})

    provider = _client_kwargs(monkeypatch, creds, settings)["credentials"]
    assert provider._identity_token_provider() == "jwt-first"
    assert provider._identity_token_provider() == "jwt-second", "each exchange gets its own token"


def test_a_refresh_that_fails_serves_the_token_it_already_had(monkeypatch: pytest.MonkeyPatch) -> None:
    """A getter the SDK calls is the wrong place to raise from: the old behaviour was to present a
    stale token, and a stale token at least has a chance. Losing the run to an exception inside a
    callback would be a new failure introduced by the fix for an old one."""
    from in_lockstep.llm.interface import Credentials, ProviderSettings, SecretStr

    def explode() -> Credentials:
        raise RuntimeError("the metadata endpoint is having a day")

    creds = Credentials(values={"id_token": SecretStr("jwt-cached")}, source="oidc", refresh=explode)
    provider = _client_kwargs(monkeypatch, creds, ProviderSettings(extra={"federation-rule-id": "f"}))[
        "credentials"
    ]
    assert provider._identity_token_provider() == "jwt-cached"


def test_an_empty_refresh_does_not_blank_the_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """`OidcResolver` returns nothing at all when the CI variables are missing, so a refresh can
    legitimately come back empty. Handing "" to the exchange would turn a working run into an
    authentication error for the rest of its life."""
    from in_lockstep.llm.interface import Credentials, ProviderSettings, SecretStr

    creds = Credentials(
        values={"id_token": SecretStr("jwt-cached")},
        source="oidc",
        refresh=lambda: Credentials.none(),
    )
    provider = _client_kwargs(monkeypatch, creds, ProviderSettings(extra={"federation-rule-id": "f"}))[
        "credentials"
    ]
    assert provider._identity_token_provider() == "jwt-cached"


def test_every_minted_token_is_seeded_into_redaction(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The reason the refresh goes back through `Auth` instead of calling the resolver directly.

    Minting is the only moment a secret is visible before a client swallows it, so it is the only
    moment redaction can learn about it. A second, third and fourth JWT that `Redact` had never
    seen would be four unredacted credentials in every error string the run produced after that.
    """
    from in_lockstep.ai.auth import Auth, AuthRequest, AuthTarget, StaticResolver
    from in_lockstep.privileged.redact import SecretRegistry

    registry = SecretRegistry()
    request = AuthRequest(target=AuthTarget.MODEL_PROVIDER, name="anthropic", keys=("id_token",))

    # Long enough to clear `_MIN_SECRET_LENGTH`; a real JWT is hundreds of characters.
    tokens = ("jwt-first-token-value", "jwt-second-token-value")
    for token in tokens:
        Auth.chain(
            StaticResolver(values={("anthropic", "id_token"): token}), registry=registry
        ).credentials_for(request)

    assert set(tokens) <= registry.known(), "a JWT Redact never saw is a JWT in the next log line"
