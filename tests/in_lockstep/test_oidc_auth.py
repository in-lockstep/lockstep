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
from io import BytesIO

import pytest

from in_lockstep.ai.auth import Auth, AuthRequest, AuthTarget, EnvResolver, OidcResolver
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


def _fake_oidc_opener(token_value: str = "oidc-test-token-value-1234"):
    """Return a callable that fakes urllib.request.urlopen for the OIDC endpoint."""

    def opener(req, *, timeout=10):
        body = json.dumps({"value": token_value}).encode()
        resp = BytesIO(body)
        resp.read = resp.read  # already fine
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    return opener


def test_oidc_resolver_ignores_api_key_requests(monkeypatch) -> None:
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


def test_oidc_resolver_answers_id_token_requests(monkeypatch) -> None:
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


def test_env_resolution_unchanged_outside_ci(monkeypatch) -> None:
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


def test_oidc_token_seeded_into_redaction_registry(monkeypatch) -> None:
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


def test_oidc_token_is_redacted_in_rendered_text(monkeypatch) -> None:
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


def test_api_key_falls_through_to_env_on_ci(monkeypatch) -> None:
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


def _federation_env(monkeypatch) -> None:
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_FEDERATION_RULE_ID", "fdrl_test")
    monkeypatch.setenv("ANTHROPIC_ORGANIZATION_ID", "org-test")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://fake.actions.url/token?x=1")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "gha-request-token")


def test_federation_mints_a_token_with_the_rules_audience_when_no_key_exists(monkeypatch) -> None:
    """No ANTHROPIC_API_KEY, federation configured: `credentials_for` mints the GitHub JWT itself
    — through Auth, so it is seeded into redaction — and with the audience the federation rule
    validates, not the chain default."""
    from in_lockstep.ai.bootstrap import credentials_for

    _federation_env(monkeypatch)
    seen: dict[str, str] = {}

    def opener(req, *, timeout=10):
        seen["url"] = req.full_url
        body = json.dumps({"value": "gha-jwt-for-anthropic"}).encode()
        resp = BytesIO(body)
        resp.__enter__ = lambda s=resp: s
        resp.__exit__ = lambda s=resp, *a: None
        return resp

    monkeypatch.setattr("urllib.request.urlopen", opener)
    registry = SecretRegistry()
    creds = credentials_for(Auth(registry=registry), "anthropic")

    assert creds.get("id_token") == "gha-jwt-for-anthropic"
    assert "audience=https://api.anthropic.com" in seen["url"], (
        "the audience is part of what the federation rule validates; the chain default "
        "would be refused at the exchange"
    )
    assert "gha-jwt-for-anthropic" in registry.known(), "minted through Auth, so redaction saw it"


def test_an_operator_supplied_token_file_defers_to_the_sdk_chain(monkeypatch) -> None:
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


def test_a_static_key_still_wins_over_configured_federation(monkeypatch) -> None:
    """An explicitly set key is somebody meaning it — the same precedence the SDK documents."""
    from in_lockstep.ai.bootstrap import credentials_for

    _federation_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-static-key-11112222")
    creds = credentials_for(Auth(registry=SecretRegistry()), "anthropic")
    assert creds.get("api_key") == "sk-ant-static-key-11112222"
    assert creds.get("id_token") == ""


def test_the_refusal_now_names_the_federation_path(monkeypatch) -> None:
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


def _client_kwargs(monkeypatch, creds, settings=None) -> dict:
    anthropic = pytest.importorskip("anthropic", reason="client tests need the provider extra")

    from in_lockstep.llm.interface import ProviderSettings
    from in_lockstep.llm.providers.anthropic import AnthropicProvider

    settings = settings or ProviderSettings()
    captured: dict = {}

    class _Capture:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Capture)
    AnthropicProvider(settings, creds)._make_client(settings, creds)
    return captured


def test_the_client_gets_the_federation_credentials_object(monkeypatch) -> None:
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


def test_an_empty_credential_passes_no_credential_argument_at_all(monkeypatch) -> None:
    """An empty api_key is not nothing to the SDK — any explicit credential argument suppresses
    its env chain, and passing one is what kept federation unreachable."""
    from in_lockstep.llm.interface import Credentials

    kwargs = _client_kwargs(monkeypatch, Credentials.none())
    assert "api_key" not in kwargs and "credentials" not in kwargs


def test_a_static_key_reaches_the_client_as_before(monkeypatch) -> None:
    from in_lockstep.llm.interface import Credentials, SecretStr

    kwargs = _client_kwargs(monkeypatch, Credentials(values={"api_key": SecretStr("sk-ant-x-12345678")}))
    assert kwargs["api_key"] == "sk-ant-x-12345678"
    assert "credentials" not in kwargs


def test_a_service_account_name_is_refused_before_any_exchange(monkeypatch) -> None:
    """The name-vs-id trap, caught locally: the Console shows the NAME, the exchange wants the
    svac_-tagged id, and this run's own HTTP 400 is the round-trip this guard replaces."""
    from in_lockstep.ai.bootstrap import MissingCredential, default_registry

    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    monkeypatch.setenv("ANTHROPIC_SERVICE_ACCOUNT_ID", "in-lockstep-gh-sa")
    with pytest.raises(MissingCredential, match="svac_"):
        default_registry(Auth())

    monkeypatch.setenv("ANTHROPIC_SERVICE_ACCOUNT_ID", "svac_0123abc")
    default_registry(Auth())
