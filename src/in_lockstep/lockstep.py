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
from .core.context import Approval, RepoInfo, RunContext
from .core.middleware import Middleware, provides_approval
from .core.policy import Policy, PolicyStack
from .core.spend import Budget, Spend, UndeclaredBudget
from .core.verbs import NEEDS_APPROVAL, Capability, UngatedAgency, capabilities_of


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

    def context(self, run_id: str, *, approval: Approval | None = None) -> RunContext:
        self._refuse_undeclared_budget()
        self._refuse_ungated_agency()
        return RunContext(
            run_id=run_id,
            repo=self.repo,
            container=self.container,
            spend=Spend(budget=self.budget),
            middleware=list(self.middleware),
            approval=approval or Approval(),
        )

    def declared_ceiling(self) -> Budget:
        """Every ceiling this lifecycle declares, merged.

        `lockstep.budget` is one way to say it and `CostBudget` middleware is another — the
        scaffold uses the second — so a check reading only the first would refuse a run that is
        perfectly well bounded, and teach people that the refusal is noise.
        """
        ceiling = self.budget
        for layer in self.middleware:
            declared = getattr(layer, "budget", None)
            if isinstance(declared, Budget):
                ceiling = ceiling.merge(declared)
        return ceiling

    def spenders(self) -> list[str]:
        """Bound adapters that declare they spend money."""
        return sorted(
            type(b.impl).__name__ if not isinstance(b.impl, type) else b.impl.__name__
            for b in self.container.resolved()
            if Capability.SPENDS_BUDGET in capabilities_of(b.impl)
        )

    def _refuse_ungated_agency(self) -> None:
        """GATE-APPROVAL-1. At startup, not at call time.

        A scoping decision, stated because it narrows the gate's literal wording. Written as "a
        `ToolSet` granting WRITES_FILES/EXECUTES_CODE", it reads as though any dangerous
        capability needs an approval path — but `PytestTest` declares `EXECUTES_CODE` and means
        it, so that reading refuses every repository that runs its own tests, and a control
        everybody has to switch off is not a control.

        What makes write-or-execute need a human is **agency**: a model choosing to do it.
        `Sandbox` is the answer for a deterministic adapter that executes code; approval is the
        answer for an adapter that both spends money and can write. So the trigger is the
        conjunction, and it fires for nothing shipped today — `AiReview` is read-only — which is
        the correct answer for a framework that ships no write verb, not a hole.
        """
        gated = sorted(
            type(b.impl).__name__ if not isinstance(b.impl, type) else b.impl.__name__
            for b in self.container.resolved()
            if Capability.SPENDS_BUDGET in capabilities_of(b.impl)
            and capabilities_of(b.impl) & NEEDS_APPROVAL
        )
        if not gated:
            return
        if any(provides_approval(layer) for layer in self.middleware):
            return
        raise UngatedAgency(
            f"{', '.join(gated)} lets a model write or execute, and no ApprovalGate is in the "
            f"middleware chain. Add one:\n\n    lockstep.middleware += [ApprovalGate()]\n\n"
            f"The human acts in your system of record — a review request, an environment "
            f"approval — so the gate refuses without a grant rather than prompting."
        )

    def _refuse_undeclared_budget(self) -> None:
        """GATE-BUDGET-1. A refusal, not a warning, and not for every run.

        Here rather than in `doctor` because the control it replaces was a compile-time error, and
        an advisory check is a suggestion wearing its name. Here rather than at the first model
        call because "refused at startup" is the property: a run that gets halfway and then stops
        has already spent whatever it spent.

        Scoped to lifecycles that can actually spend. A repository binding only Test and Validate
        needs no ceiling, and demanding one would teach people to write `Budget(usd=999)` to make
        the framework be quiet — which is worse than no check, because it looks like a decision.
        """
        if self.declared_ceiling().declared:
            return
        spenders = self.spenders()
        if not spenders:
            return
        raise UndeclaredBudget(
            f"{', '.join(spenders)} spends money and no budget is declared. Add a ceiling to "
            f"lockstep.py:\n\n    lockstep.budget = Budget(usd=2.00, wall_seconds=900)\n\n"
            f"An unbounded agent is the failure this refuses to let you ship. `in-lockstep ls` "
            f"still works without one, so you can see what is bound."
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
