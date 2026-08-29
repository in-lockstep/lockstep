"""Source control.

Host features layer over plain git rather than replacing it, so `GitHubScm` contains a `GitLocal`
instead of reimplementing diffing and blame against an API.

The write discipline is the important part: nothing pushes to a shared branch. Every write goes
through `open_change` on a run-scoped branch, which also serialises concurrent runs without a lock
service. Direct push exists only by binding an adapter whose name says what it is.
"""

from .base import (
    RUN_BRANCH_PREFIX,
    ChangeRequest,
    Diff,
    DirectPushRefused,
    GitLocal,
    GuardRefused,
    Ref,
    Scm,
    branch_for,
)
from .github import GitHubScm

__all__ = [
    "RUN_BRANCH_PREFIX",
    "ChangeRequest",
    "Diff",
    "DirectPushRefused",
    "GitHubScm",
    "GuardRefused",
    "GitLocal",
    "Ref",
    "Scm",
    "branch_for",
]
