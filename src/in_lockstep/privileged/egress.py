"""Egress control.

This replaces a firewall. Under the previous arrangement a model ran inside a sandbox with a
proxy holding an explicit domain allowlist, enforced by the substrate rather than by the agent's
own process. Moving invocation in-process deletes that, and nothing in the invoke policy replaces
it — an allowed-providers list allowlists *provider objects*, not destinations.

Two decisions shape what is here.

**When it is mandatory.** Capability alone is the wrong trigger. "Read-only" describes mutation,
not transmission: a fetch or search tool mutates nothing and is an egress channel. And the case
that matters most — a read-only review of a fork's diff, running unattended — is exactly the one a
capability-only rule exempts. So the trigger is write or execute or network capability, OR a
restricted repository, OR any untrusted content in the context package.

**Verified, not attested.** A mode the operator merely asserts is a checkbox. `ENFORCED_*` probes
a host that must be unreachable, and refuses to start if the probe succeeds, because "fail-closed"
that can be satisfied by a lie is not fail-closed.

This is privileged: it runs outside the middleware chain, so `--no-middleware` cannot reach it.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse

from ..core.verbs import Capability

ENV_MODE = "IN_LOCKSTEP_EGRESS"
# The classification, not a control by itself. Setting it makes egress enforcement mandatory
# for every invocation (see `required`), and makes GATE-RESIDENCY-1 refuse a model whose
# registration says the bytes leave (`AiInvoker` performs that check, because the data policy
# lives on the provider registration, which this module cannot see without inverting the layers).
ENV_RESTRICTED = "IN_LOCKSTEP_RESTRICTED"
# A host that must not resolve or connect if egress is genuinely constrained.
PROBE_HOST = "example.com"
PROBE_PORT = 443
PROBE_TIMEOUT = 2.0


class EgressMode(Enum):
    NONE = "none"
    ENFORCED_EXTERNAL = "enforced_external"  # the host provides it; we verify
    ENFORCED_CONTAINER = "enforced_container"  # we provide it


class EgressRefused(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class EgressPolicy:
    mode: EgressMode = EgressMode.NONE
    allow: tuple[str, ...] = ()
    restricted_repo: bool = False
    # Set once a probe has run, so a long session does not re-probe on every invocation.
    _verified: bool | None = field(default=None, repr=False)

    @classmethod
    def detect(cls, *, allow: tuple[str, ...] = (), restricted_repo: bool = False) -> EgressPolicy:
        raw = os.environ.get(ENV_MODE, "").strip().lower()
        mode = {
            "enforced": EgressMode.ENFORCED_EXTERNAL,
            "enforced_external": EgressMode.ENFORCED_EXTERNAL,
            "container": EgressMode.ENFORCED_CONTAINER,
            "enforced_container": EgressMode.ENFORCED_CONTAINER,
        }.get(raw, EgressMode.NONE)
        restricted = restricted_repo or _truthy(os.environ.get(ENV_RESTRICTED))
        return cls(mode=mode, allow=allow, restricted_repo=restricted)

    def required(
        self,
        *,
        capabilities: frozenset[Capability],
        untrusted_context: bool,
        transmits: bool = True,
    ) -> str | None:
        """Why enforcement is mandatory here, or None if it is not.

        `transmits` is False when the model call is served from a cassette or a canned answer.
        It suppresses the untrusted-content trigger and *only* that one: a tool that writes,
        executes or reaches the network is an egress channel whether or not the model call is
        real, so those triggers do not care. Without this split, `--offline` and `--dry-run` —
        the two paths that exist so this can be run with no key and no spend — would demand a
        firewall for a run that cannot put a byte on the wire.
        """
        dangerous = {
            Capability.WRITES_FILES,
            Capability.EXECUTES_CODE,
            Capability.REACHES_NETWORK,
        }
        if capabilities & dangerous:
            granted = sorted(c.value for c in (capabilities & dangerous))
            return f"the tool set grants {', '.join(granted)}"
        if self.restricted_repo:
            return "this repository is classified restricted"
        if untrusted_context and transmits:
            # The one a capability-only rule misses, and the one that actually happens.
            return "the context package contains untrusted external content"
        return None

    def check(
        self,
        *,
        capabilities: frozenset[Capability],
        untrusted_context: bool,
        transmits: bool = True,
    ) -> None:
        """Raise unless the environment provides what this invocation requires."""
        why = self.required(
            capabilities=capabilities, untrusted_context=untrusted_context, transmits=transmits
        )
        if why is None:
            return
        if self.mode is EgressMode.NONE:
            raise EgressRefused(
                "egress.unenforced",
                f"egress control is mandatory here because {why}, and none is in effect. "
                f"Run under a host that constrains egress and set {ENV_MODE}=enforced, or bind "
                f"UnsandboxedEgress deliberately.",
            )
        if not self.verify():
            raise EgressRefused(
                "egress.probe_failed",
                f"{ENV_MODE} claims {self.mode.value}, but a connection to "
                f"{PROBE_HOST}:{PROBE_PORT} succeeded. An asserted mode that a probe disproves is "
                "worse than no mode, because it reads as a control.",
            )

    def verify(self) -> bool:
        """Probe a host that must be unreachable. Cached for the session."""
        if self.mode is EgressMode.NONE:
            return False
        if self._verified is not None:
            return self._verified
        self._verified = not _can_connect(PROBE_HOST, PROBE_PORT)
        return self._verified

    def manifest(self, endpoints: Iterable[str]) -> tuple[str, ...]:
        """The hosts a run may dial, for the operator configuring the proxy this class verifies.

        `ENFORCED_EXTERNAL` means the host provides the firewall; this is the list to feed it.
        The framework never enforces destinations itself — an in-process allowlist would be a
        checkbox, the same lie `verify()` exists to catch — so the manifest is where `allow`
        earns its keep: `endpoints` are the resolved provider registrations, `allow` is what the
        operator declared beyond them (an SCM host, a package registry decided on deliberately).
        """
        hosts = set(self.allow)
        for endpoint in endpoints:
            parsed = urlparse(endpoint if "//" in endpoint else f"//{endpoint}")
            if parsed.hostname:
                hosts.add(parsed.hostname)
        return tuple(sorted(hosts))


class UnsandboxedEgress(EgressPolicy):
    """The explicit opt-out. Named after what it does, so it is greppable and reviewable."""

    def check(
        self,
        *,
        capabilities: frozenset[Capability],
        untrusted_context: bool,
        transmits: bool = True,
    ) -> None:
        return None

    def verify(self) -> bool:
        return True


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def _can_connect(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT):
            return True
    except OSError:
        return False
