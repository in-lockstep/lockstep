"""Redaction, applied at every sink rather than at an enumerated few.

The seeding problem first. Redaction only works if something knows the secret values, and an
env-scraping heuristic structurally cannot see an OIDC- or vault-derived token that was never in
the environment — which is exactly the kind of credential the design prefers. So `Auth` registers
what it mints, here, before handing it to anything. Env scraping is kept as a second source
because a secret can still arrive that way, but it is the fallback, not the mechanism.

The sink problem second. An enumerated list of sinks missed five of them the first time it was
written: stdout and stderr (public CI logs), OTel span attributes, checkpoint files, the artifact
handed between trampoline jobs, and notification bodies. So the rule is inverted — every writer
that leaves the process wraps `Redact.text`, and a test enumerates the writers and fails on any
unwrapped one.

Matching is on the literal value and on a few structural framings of it, because a provider that
base64s a key into an error message has still leaked it.
"""

from __future__ import annotations

import base64
import os
import re
import urllib.parse

MASK = "***"

# Structural patterns, applied whatever the seeded values are. Deliberately conservative: a
# false positive costs a masked log line, a false negative costs a leaked credential.
_STRUCTURAL: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*[\"']?([A-Za-z0-9._\-]{12,})"),
    re.compile(r"\bsk-[A-Za-z0-9\-_]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # A fine-grained GitHub token has its own prefix; the classic `gh?_` line above misses it.
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
    # GitLab personal/project/CI tokens — this framework speaks GitLab, so its logs will too.
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"),
    # Slack bot/app/user/refresh tokens, the usual notification-sink credential.
    re.compile(r"\bxox[abeprs]-[A-Za-z0-9\-]{10,}"),
    # A signed JWT: three base64url segments, the first always encoding '{"'. This is the shape
    # of the OIDC identity token federation mints — seeded by Auth when minted here, but a token
    # an operator exported or a provider echoed back never went through Auth.
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{8,}"),
    # PEM private-key material. The body is masked and the armour kept, so a log still says a
    # key was there — the same rule `_mask_match` applies to `api_key=`.
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----([A-Za-z0-9+/=\s]+?)-----END [A-Z0-9 ]*PRIVATE KEY-----"
    ),
)

_ENV_SUFFIXES = ("PASSWORD", "TOKEN", "SECRET", "API_KEY", "CREDENTIALS", "PRIVATE_KEY")

# Below this length a "secret" is more likely to be a common substring than a credential, and
# masking it would corrupt unrelated output.
_MIN_SECRET_LENGTH = 8


class SecretRegistry:
    """What the redactor knows. Written by Auth at mint time, read at every sink."""

    def __init__(self) -> None:
        self._values: set[str] = set()

    def add(self, value: str) -> None:
        if value and len(value) >= _MIN_SECRET_LENGTH:
            self._values.add(value)

    def add_all(self, values: frozenset[str] | set[str]) -> None:
        for value in values:
            self.add(value)

    def seed_from_environment(self) -> None:
        """Fallback source. Cannot see a credential that never entered the environment."""
        for key, value in os.environ.items():
            if any(key.upper().endswith(suffix) for suffix in _ENV_SUFFIXES):
                self.add(value)

    def known(self) -> frozenset[str]:
        return frozenset(self._values)

    def clear(self) -> None:
        self._values.clear()


redact_registry = SecretRegistry()


def _framings(secret: str) -> list[str]:
    """The same secret, as a provider or transport might have reshaped it before printing it."""
    out = [secret]
    try:
        out.append(base64.b64encode(secret.encode()).decode())
    except (ValueError, UnicodeEncodeError):  # pragma: no cover - defensive
        pass
    quoted = urllib.parse.quote(secret, safe="")
    if quoted != secret:
        out.append(quoted)
    return out


class Redact:
    """Applies the registry to anything leaving the process."""

    def __init__(self, registry: SecretRegistry | None = None) -> None:
        self.registry = registry or redact_registry

    def text(self, value: str) -> str:
        if not value:
            return value
        redacted = value
        for secret in sorted(self.registry.known(), key=len, reverse=True):
            for framing in _framings(secret):
                if framing and framing in redacted:
                    redacted = redacted.replace(framing, MASK)
        for pattern in _STRUCTURAL:
            redacted = pattern.sub(lambda m: _mask_match(m), redacted)
        return redacted

    def value(self, value: object) -> object:
        """Recursively redact a structure bound for a sink."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            rebuilt = [self.value(v) for v in value]
            return type(value)(rebuilt) if isinstance(value, list) else tuple(rebuilt)
        return value

    def exception(self, exc: BaseException) -> str:
        """Provider errors carry raw response bodies, and the ledger is committed to git."""
        return self.text(str(exc))


def _mask_match(match: re.Match[str]) -> str:
    """Mask the credential, keep the framing, so a log still says what kind of thing was there."""
    whole = match.group(0)
    if match.groups():
        secret = match.group(1)
        return whole.replace(secret, MASK)
    prefix = whole.split()[0] if " " in whole else ""
    return f"{prefix} {MASK}".strip() if prefix else MASK
