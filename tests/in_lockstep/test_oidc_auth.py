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
