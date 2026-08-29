"""Shipped middleware.

Note what is NOT here: redaction, egress policy and residency enforcement. Those are privileged —
they run outside this chain, alongside the kill switch, because `--no-middleware` exists and a
debugging flag must not be able to switch off the thing that keeps credentials out of a
git-committed record.
"""

from .approval import ApprovalGate, ApprovalRequired, assert_gated
from .budget import CostBudget
from .otel import otel
from .retry import Retry

__all__ = ["ApprovalGate", "ApprovalRequired", "CostBudget", "Retry", "assert_gated", "otel"]
