"""Plain git, and the interface host adapters extend."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ...core.changes import ChangeGuard
from ...core.types import ChangeSet

Ref = str

# Every framework-authored branch lives under this prefix. Two properties follow: a protected
# branch is never a push target, and two concurrent runs cannot collide, because a run id is in
# the name. That is why there is no lock service anywhere in this design.
RUN_BRANCH_PREFIX = "in-lockstep"


class DirectPushRefused(Exception):
    """A write was attempted outside the run-scoped namespace."""


class GuardRefused(Exception):
    """A change touches a protected path."""

    def __init__(self, refusals: list[object]) -> None:
        super().__init__(f"{len(refusals)} protected path(s) refused")
        self.refusals = refusals


def branch_for(workflow: str, run_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_/" else "-" for c in workflow)
    return f"{RUN_BRANCH_PREFIX}/{safe}/{run_id}"


@dataclass(frozen=True)
class Diff:
    text: str
    base: Ref
    head: Ref

    @property
    def paths(self) -> tuple[str, ...]:
        out: list[str] = []
        for line in self.text.splitlines():
            if line.startswith("+++ b/"):
                out.append(line[6:])
        return tuple(out)


@dataclass(frozen=True)
class ChangeRequest:
    id: str
    url: str
    branch: str
    title: str
    number: int | None = None
    trailers: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class Scm(Protocol):
    def diff(self, base: Ref, head: Ref) -> Diff: ...

    async def open_change(
        self,
        cs: ChangeSet,
        *,
        title: str,
        body: str = "",
        ticket: str = "",
        workflow: str = "",
        run_id: str = "",
    ) -> ChangeRequest: ...


class GitLocal:
    """Pure git. Always available, needs no host API and no token."""

    def __init__(self, root: str | Path = ".", *, guard: ChangeGuard | None = None) -> None:
        self.root = Path(root)
        self.guard = guard or ChangeGuard()

    def git(self, *args: str, check: bool = False) -> str:
        result = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, timeout=120)
        if check and result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").strip()

    def current_branch(self) -> str:
        return self.git("rev-parse", "--abbrev-ref", "HEAD").strip()

    def diff(self, base: Ref, head: Ref = "HEAD") -> Diff:
        return Diff(text=self.git("diff", f"{base}...{head}"), base=base, head=head)

    def blame(self, path: str, line: int) -> str:
        return self.git("blame", "-L", f"{line},{line}", "--", path)

    def assert_run_scoped(self, branch: str) -> None:
        """Refused here rather than relying on a token's scope.

        The apply job holds an ambient repository token that can write any branch, so this is
        the framework-level half of keeping protected branches unreachable. Branch protection is
        the other half, and `doctor` fails without it.
        """
        if not branch.startswith(f"{RUN_BRANCH_PREFIX}/"):
            raise DirectPushRefused(
                f"refusing to write to {branch!r}: framework writes go to "
                f"{RUN_BRANCH_PREFIX}/<workflow>/<run-id> only. Binding DirectPushScm is the "
                "deliberate, greppable way to do otherwise."
            )

    def apply(self, cs: ChangeSet, *, workflow_id: str = "") -> list[str]:
        """Write a changeset to the working tree, guard first."""
        refusals = self.guard.check(cs, workflow_id=workflow_id)
        if refusals:
            raise GuardRefused(list(refusals))

        written: list[str] = []
        for change in cs.changes:
            target = self.root / change.path
            if change.deleted:
                if target.exists():
                    target.unlink()
                written.append(change.path)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.contents or "")
            written.append(change.path)
        return written

    def commit(self, message: str, *, trailers: dict[str, str] | None = None) -> str:
        """Commit with trailers.

        Trailers are the most portable of the traceability layers: greppable forever, and they
        survive any migration that keeps the git history.
        """
        body = message
        if trailers:
            body += "\n\n" + "\n".join(f"{k}: {v}" for k, v in sorted(trailers.items()))
        self.git("add", "-A", check=True)
        self.git(*self.identity(), "commit", "-m", body, check=True)
        return self.head()

    def identity(self) -> list[str]:
        """`-c` overrides, only when the repository has no identity of its own.

        `git commit` refuses without one, and a fresh CI runner has none configured — which is the
        environment `apply` exists for, so without this the privileged half of the trampoline fails
        on an adopter's first run. Where a person HAS an identity, theirs is used: the commit is a
        record of a change made on their behalf.
        """
        configured = subprocess.run(
            ["git", "config", "user.email"], cwd=self.root, capture_output=True, text=True
        )
        if configured.returncode == 0 and configured.stdout.strip():
            return []
        return [
            "-c",
            "user.name=in-lockstep",
            "-c",
            "user.email=in-lockstep@users.noreply.github.com",
        ]

    async def open_change(
        self,
        cs: ChangeSet,
        *,
        title: str,
        body: str = "",
        ticket: str = "",
        workflow: str = "",
        run_id: str = "",
    ) -> ChangeRequest:
        """Local git has no pull requests; it makes the branch and stops there."""
        branch = branch_for(workflow or "change", run_id or "local")
        self.assert_run_scoped(branch)
        self.git("checkout", "-b", branch)
        self.apply(cs, workflow_id=workflow)
        trailers = {"In-Lockstep-Run": run_id}
        if ticket:
            trailers["Ticket"] = ticket
        self.commit(title, trailers=trailers)
        return ChangeRequest(id=branch, url="", branch=branch, title=title, trailers=trailers)
