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
    #: Whether the pull request was opened as a draft — not yet asking for human review. An AI
    #: change starts here by default and is marked ready once its tests pass and the workflow wants
    #: a human to look. Always False for a host with no draft concept (local git).
    draft: bool = False


@dataclass(frozen=True)
class Commit:
    """One commit, with its trailers read back.

    The framework has always WRITTEN `In-Lockstep-Run` and `Ticket` trailers; this is the shape
    that lets something read them again — a backport picking commits for a ticket, a report
    joining a release to the runs that built it.
    """

    sha: str
    subject: str
    trailers: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class Scm(Protocol):
    """The host-agnostic shape. `base` is committed now, before third parties implement:
    retrofitting a parameter onto a Protocol others implement is a breaking change, and a change
    request that can only ever target the default branch cannot serve a backport."""

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
        base: Ref = "",
        draft: bool = False,
    ) -> ChangeRequest: ...

    async def mark_ready(self, change: ChangeRequest) -> None:
        """Take a draft change request out of draft — it is now asking for human review. A no-op on
        a host with no draft concept, so a caller can always call it after a green run."""
        ...


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

    def merge_base(self, a: Ref, b: Ref) -> str:
        return self.git("merge-base", a, b, check=True).strip()

    def start_point(self, ref: Ref) -> Ref:
        """A spelling of `ref` that `git checkout` can branch from.

        A CI checkout has the release line only as `origin/release-1.0` — a detached HEAD with no
        local branches — so `git checkout -b b release-1.0` exits 128 while `origin/release-1.0`
        works. The host branch name a pull request targets is the bare one, so the two spellings
        cannot be the same value: this resolves the git start-point, and `open_change` keeps the
        bare name for the API. Same bare-then-remote fallback the trusted-config ref uses.
        """
        # Option-confusion guard: `base` becomes a git checkout start-point and a `gh --base` value,
        # and a backport can take it from a ticket's target — so a `-`-leading ref that git or gh
        # would read as a flag is refused here, the same way `materialize` guards its ref. Not
        # injection (no shell), but a ref never legitimately begins with a dash.
        if ref.startswith("-"):
            raise RuntimeError(f"refusing a base ref that looks like an option: {ref!r}")
        if "/" in ref:
            return ref
        for candidate in (ref, f"origin/{ref}"):
            if self.git("rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}").strip():
                return candidate
        return ref  # unresolvable: let the checkout fail with git's own message

    def cherry_pick(self, *commits: str) -> str:
        """Apply commits onto HEAD, `-x` so each records where it came from. Returns new HEAD.

        A conflict raises with git's own message and leaves the tree mid-pick — deliberately.
        Resolving one is a decision, and `git cherry-pick --abort` is the caller's honest retreat;
        cleaning up silently here would discard the information a person needs to decide.
        """
        self.git(*self.identity(), "cherry-pick", "-x", *commits, check=True)
        return self.head()

    def tag(self, name: str, *, message: str = "") -> None:
        if message:
            self.git(*self.identity(), "tag", "-a", name, "-m", message, check=True)
        else:
            self.git("tag", name, check=True)

    def commits_between(self, base: Ref, head: Ref = "HEAD") -> tuple[Commit, ...]:
        """Oldest first, trailers parsed. The read half of the trailer discipline: `commit`
        writes `In-Lockstep-Run` and `Ticket`, and until this existed nothing could get them
        back without shelling out by hand."""
        out = self.git(
            "log", "--reverse", f"{base}..{head}", "--format=%H%x00%s%x00%(trailers:only,unfold)%x1e"
        )
        commits = []
        for record in out.split("\x1e"):
            record = record.strip("\n")
            if not record.strip():
                continue
            sha, _, rest = record.partition("\x00")
            subject, _, block = rest.partition("\x00")
            trailers = {}
            for line in block.splitlines():
                key, sep, value = line.partition(": ")
                if sep:
                    trailers[key.strip()] = value.strip()
            commits.append(Commit(sha=sha.strip(), subject=subject, trailers=trailers))
        return tuple(commits)

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
        base: Ref = "",
        draft: bool = False,
    ) -> ChangeRequest:
        """Local git has no pull requests; it makes the branch and stops there.

        `base` starts the branch somewhere other than HEAD — a release line, for a backport.
        Empty keeps the old behaviour: the branch grows from wherever the tree stands. `draft` has
        no meaning without a host, so the returned request reports `draft=False`: a local branch is
        as ready as it gets.
        """
        branch = branch_for(workflow or "change", run_id or "local")
        self.assert_run_scoped(branch)
        if base:
            self.git("checkout", "-b", branch, self.start_point(base), check=True)
        else:
            self.git("checkout", "-b", branch)
        self.apply(cs, workflow_id=workflow)
        trailers = {"In-Lockstep-Run": run_id}
        if ticket:
            trailers["Ticket"] = ticket
        self.commit(title, trailers=trailers)
        return ChangeRequest(id=branch, url="", branch=branch, title=title, trailers=trailers)

    async def mark_ready(self, change: ChangeRequest) -> None:
        """No-op: local git has no draft state to leave."""
        return None
