"""Credential resolution.

`Auth` is the sole issuer, and that is load-bearing rather than tidy: it is the only point that
sees a secret value before a client swallows it, so it is the only point that can seed redaction.
Providers are forbidden from reading the environment for exactly this reason.

Resolvers are tried in order and the first non-empty answer wins, so a CI-native federated token
beats a long-lived environment variable without either knowing about the other.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from ..llm.interface import Credentials, SecretStr
from ..privileged.redact import SecretRegistry, redact_registry


class AuthTarget(Enum):
    """What is being authenticated to.

    MCP servers are here because stdio servers launch with secret-bearing environment blocks. If
    they resolve outside Auth, those secrets are never seeded into redaction, and MCP tool error
    text goes to a git-committed ledger unredacted.
    """

    MODEL_PROVIDER = "model_provider"
    SCM = "scm"
    TICKET_SOURCE = "ticket_source"
    MCP_SERVER = "mcp_server"
    NOTIFIER = "notifier"


@dataclass(frozen=True)
class AuthRequest:
    target: AuthTarget
    name: str  # "anthropic", "github", "mcp:git"
    keys: tuple[str, ...] = ("api_key",)


@runtime_checkable
class Resolver(Protocol):
    def resolve(self, request: AuthRequest) -> dict[str, str]: ...


@dataclass
class EnvResolver:
    """Explicit environment variables, mapped by name. The plainest source."""

    mapping: dict[tuple[str, str], str] = field(default_factory=dict)

    def resolve(self, request: AuthRequest) -> dict[str, str]:
        found: dict[str, str] = {}
        for key in request.keys:
            var = self.mapping.get((request.name, key))
            if var is None:
                var = f"{request.name.upper().replace('-', '_')}_{key.upper()}"
            value = os.environ.get(var, "")
            if value:
                found[key] = value
        return found


@dataclass
class OidcResolver:
    """CI-native federated identity, tried before long-lived environment variables.

    A short-lived token minted per run beats a secret sitting in repository settings, and this is
    the resolver that makes "prefer federation" a default rather than advice. It resolves nothing
    outside CI, so the chain simply falls through on a laptop.

    Note the interaction with redaction: a federated token never appears in the environment, so an
    env-scraping redactor could not see it. It is redactable only because Auth mints it.
    """

    audience: str = "in-lockstep"

    def resolve(self, request: AuthRequest) -> dict[str, str]:
        request_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
        request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
        if not (request_url and request_token):
            return {}
        try:
            import json
            import urllib.request

            url = f"{request_url}&audience={self.audience}"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {request_token}"})
            with urllib.request.urlopen(req, timeout=10) as response:  # noqa: S310
                payload = json.loads(response.read().decode())
        except Exception:  # noqa: BLE001 - a resolver that fails falls through to the next
            return {}
        token = str(payload.get("value") or "")
        return {"id_token": token} if token else {}


@dataclass
class StaticResolver:
    """For tests and for explicit wiring. Never reads ambient state."""

    values: dict[tuple[str, str], str] = field(default_factory=dict)

    def resolve(self, request: AuthRequest) -> dict[str, str]:
        return {
            key: self.values[(request.name, key)]
            for key in request.keys
            if (request.name, key) in self.values
        }


class Auth:
    """A chain of resolvers, and the only thing that mints Credentials."""

    def __init__(
        self,
        resolvers: list[Resolver] | None = None,
        *,
        registry: SecretRegistry | None = None,
    ) -> None:
        self.resolvers = resolvers or [EnvResolver()]
        self.registry = registry or redact_registry

    @classmethod
    def chain(cls, *resolvers: Resolver, registry: SecretRegistry | None = None) -> Auth:
        return cls(list(resolvers), registry=registry)

    def credentials_for(self, request: AuthRequest) -> Credentials:
        """Resolve, seed redaction, then return. The ordering is the contract.

        Seeding happens before the value is handed anywhere, so there is no window in which a
        credential exists in the process and the redactor does not know about it.
        """
        found: dict[str, str] = {}
        source = "none"
        for resolver in self.resolvers:
            answer = resolver.resolve(request)
            if answer:
                found = answer
                source = type(resolver).__name__
                break

        if not found:
            return Credentials.none()

        for value in found.values():
            self.registry.add(value)

        return Credentials(
            values={k: SecretStr(v) for k, v in found.items()},
            source=f"{source}:{request.name}",
        )
