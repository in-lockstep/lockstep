"""Human approval for dangerous capability.

Deliberately refuses at *container resolution* rather than at call time. A gate that fires when
the model has already decided to write is a gate that discovers the problem late; a binding that
grants write or execute tools without an approval path is a configuration error, and the right
moment to say so is when the configuration is read.

The human acts in the system of record — a review request, an environment approval — not in a
bespoke surface. Approval therefore inherits the host's authentication, authorization and audit,
and the framework exposes no inbound endpoint.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..core.middleware import ActionCall, Next, capabilities_for
from ..core.outcome import Finding, Outcome, Severity, Status
from ..core.verbs import Capability, capabilities_of

GATED = frozenset({Capability.WRITES_FILES, Capability.EXECUTES_CODE})


class ApprovalRequired(Exception):
    """A binding grants dangerous capability with no approval path configured."""


@dataclass
class ApprovalGate:
    """Blocks until a grant exists. The grant is external; this only refuses without one."""

    when: Callable[[ActionCall], bool] | None = None
    granted: Callable[[ActionCall], bool] | None = None

    async def __call__(self, ctx: object, call: ActionCall, next: Next) -> Outcome[object]:
        capabilities = capabilities_for(ctx, call)

        gated = bool(capabilities & GATED) or (self.when is not None and self.when(call))
        if not gated:
            return await next()

        if self.granted is not None and self.granted(call):
            return await next()

        granted = sorted(c.value for c in (capabilities & GATED))
        return Outcome(
            status=Status.BLOCKED,
            reason="approval.required",
            findings=(
                Finding(
                    id="approval.required",
                    message=(
                        f"{call!r} grants {', '.join(granted) or 'gated capability'} and no "
                        "approval was granted. Approve in the system of record — a review "
                        "request or an environment approval — rather than here."
                    ),
                    severity=Severity.ERROR,
                    blocking=True,
                ),
            ),
        )


def assert_gated(container: object, iface: type, *, name: str | None = None) -> None:
    """GATE-APPROVAL-1: refuse a dangerous binding at resolution, not at call time."""
    from ..core.container import Container

    if not isinstance(container, Container) or not container.has(iface, name):
        return
    capabilities = capabilities_of(container.resolve(iface, name))
    if capabilities & GATED:
        raise ApprovalRequired(
            f"{iface.__name__} is bound to an adapter granting "
            f"{sorted(c.value for c in (capabilities & GATED))} with no ApprovalGate in the "
            "middleware chain. Add one, or bind an adapter that does not need it."
        )
