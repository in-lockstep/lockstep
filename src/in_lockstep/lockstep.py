"""The `Lockstep` facade — what a `.lockstep/lockstep.py` actually touches.

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
from .core.context import Approval, RepoFacts, RepoInfo, RunContext
from .core.middleware import Middleware, provides_approval
from .core.policy import Policy, PolicyStack
from .core.spend import Budget, DailySpendExceeded, Spend, UndeclaredBudget
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
        # How many times an automated fix may be re-attempted before a human is asked. When a run
        # fails its tests it opens an `ai-generated` bug issue, which an agent may pick up and try
        # again; this bounds that loop. The repo owner raises or lowers it in `lockstep.py`.
        self.max_attempts = 3
        # Where this configuration came from, in the loader's words — "trusted ref X", "local
        # working tree", "none (detected defaults)". Set by whoever loaded the module; recorded
        # into every ledger record, because which lockstep.py constrained a run is part of the
        # run's evidence.
        self.config_source = ""

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
        self._refuse_exhausted_daily_ceiling()
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

    def _refuse_exhausted_daily_ceiling(self) -> None:
        """The rolling per-repository spend window, rebuilt from the ledger (item 18).

        A pre-run refusal like the two above it, and equally out of `--no-middleware`'s reach:
        `context` is where a run begins, so nothing that runs can precede this check. Opt-in via
        IN_LOCKSTEP_DAILY_LIMIT — advisory-first was the resolved tension, and an org states the
        variable in CI the same way it states the doctor baseline.

        Honest about its coverage: the sum is what THIS clone's ledger has seen. On the orphan
        branch that is every pushed record the checkout carries; on a runner that never fetched
        it, less. That is deliberately weaker than the substrate's partition and said so in the
        crosswalk — the durable answer is the provider-side org limit, and the SHARED-store
        compare-and-set stays the declared upgrade path.
        """
        from datetime import UTC, datetime

        raw = os.environ.get("IN_LOCKSTEP_DAILY_LIMIT", "").strip()
        if not raw:
            return
        # Scoped like GATE-BUDGET-1: a run that cannot spend cannot push past a spend ceiling,
        # and refusing a free selfcheck because yesterday's agent runs were expensive teaches
        # people the refusal is noise.
        if not self.spenders():
            return
        try:
            limit = float(raw)
        except ValueError:
            # A malformed ceiling must not silently mean "no ceiling" — but refusing every run
            # over a typo is a different footgun. Loud, then unenforced, is the honest middle.
            print(f"ledger    IN_LOCKSTEP_DAILY_LIMIT is {raw!r}, not a number; ceiling not enforced")
            return

        from .platform.ledger import spent_in_window, store_for

        store = store_for(self.container, self.repo.root)
        reader = getattr(store, "records", None)
        records = reader() if callable(reader) else []
        spent = spent_in_window(records, now=datetime.now(UTC))
        if spent < limit:
            return
        raise DailySpendExceeded(
            f"this repository's recorded runs have spent ${spent:.2f} in the last 24h, at or "
            f"over the IN_LOCKSTEP_DAILY_LIMIT of ${limit:.2f}; refusing to start another. "
            f"The window rolls — retry later, raise the limit deliberately, or read "
            f"`in-lockstep report` to see where it went. This sums the local ledger only; the "
            f"provider-side organisation limit remains the durable backstop."
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
        facts=_detect_facts(Path(root)),
    )


def _detect_facts(root: Path) -> RepoFacts:
    """Read the marker files, so a drop-in default fits the repository rather than assuming Python.

    Deliberately conservative: every signal here is a file that exists or a string that appears in
    one, never a heuristic that guesses. A repository this misreads binds the wrong default, and a
    wrong default that runs is worse than an honest absence — so when in doubt this reports nothing
    for that slot and the scaffold leaves a commented stub.
    """
    import json as _json

    def has(*names: str) -> bool:
        return any((root / n).exists() for n in names)

    def read(name: str) -> str:
        try:
            return (root / name).read_text()
        except OSError:
            return ""

    pyproject = read("pyproject.toml")
    package_json_raw = read("package.json")
    package: dict[str, Any] = {}
    if package_json_raw:
        try:
            loaded = _json.loads(package_json_raw)
            package = loaded if isinstance(loaded, dict) else {}
        except ValueError:
            package = {}

    python = bool(pyproject) or has("setup.py", "setup.cfg") or has("requirements.txt")
    node = bool(package)
    stack = "python" if python else ("node" if node else "")

    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    # A pytest-specific marker only, never a bare `tests/` directory: a Django or stdlib-unittest
    # repository keeps its tests in `tests/` and does not run pytest, so binding PytestTest from
    # the directory name is the guess this function's docstring says it will not make — and a wrong
    # default that runs (`python -m pytest` erroring, or collecting under the wrong runner) is worse
    # than an honest absence the scaffold leaves as a stub.
    pytest = python and (
        has("pytest.ini", "conftest.py") or "[tool.pytest" in pyproject or "[pytest]" in read("tox.ini")
    )
    test_command: tuple[str, ...] = ()
    if not pytest and isinstance(scripts, dict) and "test" in scripts:
        test_command = ("npm", "test")

    ruff = "[tool.ruff" in pyproject or has("ruff.toml", ".ruff.toml")
    eslint = (
        has(".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.yml", ".eslintrc.cjs")
        # ESLint 9's flat config, the default since 2024 — a modern Node repo has only these.
        or has("eslint.config.js", "eslint.config.mjs", "eslint.config.cjs")
        or isinstance(package.get("eslintConfig"), dict)
    )
    lint_command: tuple[str, ...] = ("npx", "eslint", ".") if (eslint and not ruff) else ()

    make_targets: tuple[str, ...] = ()
    makefile = has("Makefile", "makefile")
    if makefile:
        make_targets = _make_targets(read("Makefile") or read("makefile"))

    coverage = (
        has(".coveragerc", ".coverage-floor")
        or "[tool.coverage" in pyproject
        or "[coverage:" in read("setup.cfg")
    )

    ci_host = (
        "github" if (root / ".github" / "workflows").is_dir() else ("gitlab" if has(".gitlab-ci.yml") else "")
    )

    agent_instructions = tuple(n for n in ("CLAUDE.md", "AGENTS.md", ".cursorrules") if (root / n).exists())

    return RepoFacts(
        stack=stack,
        pytest=pytest,
        test_command=test_command,
        ruff=ruff,
        eslint=eslint,
        lint_command=lint_command,
        dockerfile=has("Dockerfile", "Containerfile"),
        makefile=makefile,
        make_targets=make_targets,
        coverage=coverage,
        ci_host=ci_host,
        readme=has("README.md", "README.rst", "README.txt", "README"),
        docs=(root / "docs").is_dir(),
        agent_instructions=agent_instructions,
    )


def _make_targets(text: str) -> tuple[str, ...]:
    """The named targets in a Makefile, first few, ignoring pattern rules and `.PHONY`.

    The `::?(?!=)` is what keeps a simply-expanded variable assignment out: `CC:=gcc` and the
    double-colon `X::=y` both put an `=` right after the colon(s), where a real target
    (`check:` or a double-colon rule `x::`) does not, so the lookahead excludes the assignments a
    bare `:` would have captured as targets.
    """
    import re

    seen: list[str] = []
    for match in re.finditer(r"(?m)^([A-Za-z][A-Za-z0-9_-]*)::?(?!=)", text):
        name = match.group(1)
        if name not in seen and not name.startswith("."):
            seen.append(name)
    return tuple(seen[:8])
