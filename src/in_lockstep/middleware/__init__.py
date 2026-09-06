"""Shipped middleware.

Note what is NOT here: redaction, egress policy and residency enforcement. Those are privileged —
they run outside this chain, alongside the kill switch, because `--no-middleware` exists and a
debugging flag must not be able to switch off the thing that keeps credentials out of a
git-committed record.

Nor is there a `Retry`. One shipped here for the whole pivot and was constructed by nothing --
not this repository's module, not the chain `init` scaffolds, not an example -- because what it
did was not wanted: it refused to re-invoke any action that spends budget, which is every AI verb,
and said so with a NOTE finding on every paid outcome; and it retried the deterministic verbs on
`ERRORED`, where a missing tool does not appear on the third attempt and a timed-out suite is
re-paid in wall clock. Retry belongs at the transport, where one HTTP attempt is one HTTP attempt,
and `ai/retry.py` is that layer, bound inside every `AiInvoker` (#265, `GATE-RETRY-5`).
"""

from .approval import ApprovalGate, ApprovalRequired, assert_gated
from .budget import CostBudget
from .otel import otel

__all__ = ["ApprovalGate", "ApprovalRequired", "CostBudget", "assert_gated", "otel"]
