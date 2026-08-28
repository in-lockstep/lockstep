"""The `Lockstep` facade — what a `lockstep.py` at your repo root actually touches.

Construction is pure: this may build objects and bind them, but must not perform IO at import.
That is what makes a lifecycle definition safe to introspect, and it is why `detect()` reads the
environment rather than the network.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .core.changes import ChangeGuard, PathPolicy
from .core.container import Container, Scope, Tier
from .core.context import RepoInfo, RunContext
from .core.middleware import Middleware
from .core.policy import Policy, PolicyStack
from .core.spend import Budget, Spend


@dataclass
class Models:
    """Per-verb model routing. Populated in phase 2, declared here so `ls` has something to print."""

    routes: dict[str, str] = field(default_factory=dict)

    def route(self, verb: Any, model_id: str) -> None:
        key = getattr(verb, "value", str(verb))
        self.routes[key] = model_id


class Lockstep:
    def __init__(
        self,
        *,
        repo: RepoInfo | None = None,
        container: Container | None = None,
    ) -> None:
        self.container = container or Container()
        self.repo = repo or RepoInfo(root=str(Path.cwd()))
        self.middleware: list[Middleware] = []
        self.policy = PolicyStack()
        self.models = Models()
        self.guard = ChangeGuard(PathPolicy())
        self.budget = Budget()

    # -- configuration -------------------------------------------------------------

    def bind(
        self,
        iface: type[Any],
        impl: Any,
        *,
        name: str | None = None,
        scope: Scope = Scope.SINGLETON,
        tier: Tier = Tier.EXPLICIT,
    ) -> None:
        self.container.bind(iface, impl, name=name, scope=scope, tier=tier)

    def contribute(self, policy: Policy) -> None:
        self.policy.contribute(policy)

    # -- detection -----------------------------------------------------------------

    @classmethod
    def detect(cls, root: str | Path | None = None) -> Lockstep:
        """Sniff the repository and CI environment. A default, not magic — always overridable."""
        path = Path(root or os.environ.get("GITHUB_WORKSPACE") or Path.cwd())
        return cls(repo=_detect_repo(path))

    # -- running -------------------------------------------------------------------

    def context(self, run_id: str) -> RunContext:
        return RunContext(
            run_id=run_id,
            repo=self.repo,
            container=self.container,
            spend=Spend(budget=self.budget),
            middleware=list(self.middleware),
        )


def _detect_repo(path: Path) -> RepoInfo:
    def git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=path, capture_output=True, text=True, timeout=5
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
            return ""

    root = git("rev-parse", "--show-toplevel") or str(path)
    return RepoInfo(
        root=root,
        head=git("rev-parse", "HEAD"),
        branch=git("rev-parse", "--abbrev-ref", "HEAD"),
        dirty=bool(git("status", "--porcelain")),
    )
