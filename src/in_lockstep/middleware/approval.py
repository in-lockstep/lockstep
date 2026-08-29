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

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import ClassVar

from ..core.middleware import ActionCall, Next, capabilities_for, provides_approval
from ..core.outcome import Finding, Outcome, Severity, Status
from ..core.verbs import NEEDS_APPROVAL, capabilities_of

#: Re-exported from core, where the vocabulary belongs. Kept as a name here because
#: this module's readers look for it here.
GATED = NEEDS_APPROVAL


class ApprovalRequired(Exception):
    """A binding grants dangerous capability with no approval path configured."""


@dataclass
class ApprovalGate:
    """Blocks until a grant exists. The grant is external; this only refuses without one."""

    #: Read by `core.middleware.provides_approval`. Declared rather than inferred from the class,
    #: so a house gate routing approvals elsewhere satisfies the startup check by saying so.
    provides_approval: ClassVar[bool] = True

    when: Callable[[ActionCall], bool] | None = None
    granted: Callable[[ActionCall], bool] | None = None

    async def __call__(self, ctx: object, call: ActionCall, next: Next) -> Outcome[object]:
        capabilities = capabilities_for(ctx, call)

        gated = bool(capabilities & GATED) or (self.when is not None and self.when(call))
        if not gated:
            return await next()

        if self.granted is not None and self.granted(call):
            return await next()

        # The grant the run was started with. Read from the context rather than from the
        # environment, so `--approve` at a terminal and `--approved-by` from a verified CI actor
        # reach this the same way — which is what lets one process serve a project before and
        # after it moves to hosted triggers.
        approval = getattr(ctx, "approval", None)
        if approval is not None and getattr(approval, "granted", False):
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
                        "approval was granted. Locally, `--approve` says you are the human "
                        "watching this run. Unattended, `--approved-by <who>` records the person "
                        "a gate verified — and the stronger answer is an approval in your system "
                        "of record, a review request or a CI environment approval."
                    ),
                    severity=Severity.ERROR,
                    blocking=True,
                ),
            ),
        )


def assert_gated(
    container: object,
    iface: type,
    *,
    name: str | None = None,
    middleware: Sequence[object] = (),
) -> None:
    """GATE-APPROVAL-1: refuse a dangerous binding at resolution, not at call time.

    `middleware` was missing, and its absence was the defect. The refusal message has always said
    "with no ApprovalGate in the middleware chain" while nothing here looked at a chain — so this
    could only ever have refused unconditionally, which is why calling it anywhere would have made
    every repository that binds a test runner unusable. It is the shape of an unwired control:
    correct-looking, never invoked, and wrong the first time it would have been.
    """
    from ..core.container import Container

    if any(provides_approval(layer) for layer in middleware):
        return
    if not isinstance(container, Container) or not container.has(iface, name):
        return
    capabilities = capabilities_of(container.resolve(iface, name))
    if capabilities & GATED:
        raise ApprovalRequired(
            f"{iface.__name__} is bound to an adapter granting "
            f"{sorted(c.value for c in (capabilities & GATED))} with no ApprovalGate in the "
            "middleware chain. Add one, or bind an adapter that does not need it."
        )
