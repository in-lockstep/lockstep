"""Backport, deterministic first.

A backport is mostly not an AI problem: the change already exists, reviewed and merged, and what a
release line needs is that exact change replayed onto it. So the primary path here is plain git —
`cherry-pick -x` in a throwaway worktree of the target — and it costs nothing, needs no key, no
budget and no approval, because no model chooses anything.

A model enters at exactly one point: a pick that CONFLICTS. That is the moment where somebody has
to write code that exists in neither parent, and a `ConflictResolver` — bound deliberately, never
by default — is handed the conflicted files with their markers and the commit being replayed, and
asked for the merged contents. `GitBackport` declares `SPENDS_BUDGET` and `WRITES_FILES` only when
a resolver is bound, so the framework's budget and approval gates engage for precisely the runs
where a model can author lines, and stay out of the way of the runs where git did all the work.

Nothing here touches the real working tree. The picks happen in a disposable worktree, the result
travels as a `ChangeSet` relative to the TARGET line, and `apply --base <target>` (or
`Scm.open_change(base=...)`) is what writes it — through the guard, like every other change.

Which commits: named explicitly, or discovered from the `Ticket:` trailer every workflow commit
carries — the read half of the trailer discipline `GitLocal.commits_between` documents. A ticket's
`fix_versions` says where a change must land; this verb is how it gets there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

from ..core.outcome import Cost, Finding, Outcome, Severity, Status
from ..core.types import ChangeSet, FileChange
from ..core.verbs import Capability, Verb
from ..platform.scm import GitLocal
from .worktree import WorktreeError, materialize

#: How much of the commit's own patch a resolver is shown. Bounded because a patch is model input
#: with a token budget, and the conflicted files — which travel in full — carry the local truth.
MAX_PATCH_CHARS = 20_000


class Backport:
    """The verb interface. Workflows ask for this; a binding decides what serves it."""


@dataclass(frozen=True)
class BackportSpec:
    """Which commits onto which line. Frozen: it is hashed for step identity.

    `commits` empty means "discover": every commit on `source` since it diverged from `target`
    whose `Ticket:` trailer names `ticket.key`. Explicit SHAs win outright and need no ticket.
    """

    #: The release line the change must land on — a bare branch name (`release-1.2`).
    target: str
    commits: tuple[str, ...] = ()
    #: The work item, when there is one: the discovery key, and traceability for the record.
    ticket: Any = None
    #: The line the commits live on. HEAD is the ordinary answer.
    source: str = "HEAD"


@dataclass(frozen=True)
class PickedCommit:
    sha: str
    subject: str


@dataclass(frozen=True)
class Conflict:
    """One pick that git could not complete: what was being replayed, and the state it left.

    `files` carry the conflicted contents WITH their markers — both sides, exactly as a person
    would see them — and `patch` is the commit being replayed, so a resolver knows the intent it
    is preserving rather than guessing from the markers alone.
    """

    commit: str
    subject: str
    paths: tuple[str, ...]
    files: tuple[FileChange, ...] = ()
    patch: str = ""
    detail: str = ""


@dataclass(frozen=True)
class BackportReport:
    """What was picked, onto what, and who wrote any line git could not."""

    changeset: ChangeSet = field(default_factory=ChangeSet)
    target: str = ""
    picked: tuple[PickedCommit, ...] = ()
    #: Paths whose merged contents a resolver authored. Empty on the wholly-deterministic path —
    #: which is the claim a reviewer most wants: git wrote all of this, a model wrote none.
    resolved: tuple[str, ...] = ()
    #: The conflict the run stopped on, when it stopped. None on success.
    conflict: Conflict | None = None
    summary: str = ""

    @property
    def empty(self) -> bool:
        return not self.changeset.changes


@runtime_checkable
class ConflictResolver(Protocol):
    """What resolves a conflicted pick: the merged contents for the conflicted paths, as an
    Outcome so a refusal, a cost and findings travel the same way every other answer does."""

    async def resolve(self, ctx: Any, conflict: Conflict) -> Outcome[tuple[FileChange, ...]]: ...


class GitBackport:
    """Cherry-pick onto a throwaway worktree of the target; escalate only what conflicts."""

    verb: ClassVar[Verb] = Verb.BACKPORT

    def __init__(self, repo_root: str = ".", *, resolver: ConflictResolver | None = None) -> None:
        self.repo_root = repo_root
        self.resolver = resolver
        # Declared per instance because the honest declaration depends on composition: without a
        # resolver nothing spends and nothing but git writes, and claiming otherwise would drag
        # budget and approval gates into a run they have nothing to protect. With one, a model can
        # author file contents, which is exactly the conjunction GATE-APPROVAL-1 fires on.
        self.capabilities: frozenset[Capability] = (
            frozenset({Capability.READS_REPO})
            if resolver is None
            else frozenset({Capability.READS_REPO, Capability.SPENDS_BUDGET, Capability.WRITES_FILES})
        )

    async def invoke(self, ctx: Any, inp: BackportSpec) -> Outcome[BackportReport]:
        # Option-confusion guards, as everywhere a ref becomes an argv token: none of these values
        # may steer git as a flag. `materialize` refuses the ref again — a rule enforced at one
        # point is enforced at none.
        for value in (inp.target, inp.source, *inp.commits):
            if str(value).startswith("-"):
                return _blocked(
                    "backport.option_confusion",
                    f"refusing a ref that looks like an option: {value!r}",
                )
        if not inp.target:
            return _blocked("backport.no_target", "a backport needs a target release line.")

        repo = GitLocal(self.repo_root)
        try:
            start = repo.start_point(inp.target)
        except RuntimeError as e:
            return _blocked("backport.option_confusion", str(e))
        except OSError as e:
            # A repo_root that does not exist fails in the subprocess launch, not in git.
            return _blocked("backport.no_worktree", f"cannot run git in {self.repo_root!r}: {e}")

        shas = tuple(inp.commits) or self._discover(repo, inp, start)
        if isinstance(shas, Outcome):  # discovery refused, with its own reason
            return shas
        if not shas:
            key = str(getattr(inp.ticket, "key", "") or "")
            return _blocked(
                "backport.nothing_to_pick",
                f"no commit on {inp.source} carries a `Ticket: {key}` trailer, and none was named "
                f"explicitly. Workflow commits carry the trailer; hand-made ones may need "
                f"`commits=` spelled out.",
            )

        picked: list[PickedCommit] = []
        resolved: list[str] = []
        findings: list[Finding] = []
        cost = Cost()

        try:
            return await self._pick(ctx, inp, start, shas, picked, resolved, findings, cost)
        except WorktreeError as e:
            # A directory that is not a repository, or a target that resolves to nothing: a setup
            # problem with one obvious remedy, refused rather than left as a traceback.
            return _blocked("backport.no_worktree", str(e))

    async def _pick(
        self,
        ctx: Any,
        inp: BackportSpec,
        start: str,
        shas: tuple[str, ...],
        picked: list[PickedCommit],
        resolved: list[str],
        findings: list[Finding],
        cost: Cost,
    ) -> Outcome[BackportReport]:
        repo = GitLocal(self.repo_root)
        async with materialize(self.repo_root, ChangeSet(), ref=start) as tree:
            work = GitLocal(tree)
            base = work.head()
            for sha in shas:
                subject = repo.git("log", "-1", "--format=%s", sha).strip() or sha[:12]
                try:
                    # `-x` records where each commit came from, as `GitLocal.cherry_pick` does.
                    # `--keep-redundant-commits` keeps a pick that is already on the target from
                    # stopping the run: it lands as an empty commit, the final diff is empty, and
                    # the outcome says "already present" — so a re-run is idempotent rather than
                    # an error asking a person about `--skip`.
                    work.git(
                        *work.identity(),
                        "cherry-pick",
                        "-x",
                        "--keep-redundant-commits",
                        sha,
                        check=True,
                    )
                except RuntimeError as e:
                    conflicted = _unmerged(work)
                    if not conflicted:
                        # git refused for a reason that is not a content conflict — a merge
                        # commit without -m, an unknown sha. A model has nothing to resolve.
                        return _failed(
                            "backport.cherry_pick_failed",
                            f"git could not pick {sha[:12]} ({subject}): {e}",
                            report=BackportReport(target=inp.target, picked=tuple(picked)),
                            cost=cost,
                        )
                    conflict = Conflict(
                        commit=sha,
                        subject=subject,
                        paths=conflicted,
                        files=_conflicted_files(tree, conflicted),
                        patch=repo.git("show", sha)[:MAX_PATCH_CHARS],
                        detail=str(e),
                    )
                    if self.resolver is None:
                        return _conflict_outcome(inp, conflict, picked, cost)
                    answer = await self.resolver.resolve(ctx, conflict)
                    cost = cost + answer.cost
                    findings.extend(answer.findings)
                    files = answer.value or ()
                    if answer.status is not Status.SUCCEEDED or not files:
                        outcome = _conflict_outcome(inp, conflict, picked, cost)
                        return Outcome(
                            status=outcome.status,
                            reason=answer.reason or outcome.reason,
                            value=outcome.value,
                            cost=cost,
                            findings=outcome.findings + tuple(findings),
                            decided=answer.decided,
                        )
                    problem = _write_resolutions(tree, conflict, files)
                    if problem is not None:
                        return _failed(
                            "backport.resolution_out_of_scope",
                            problem,
                            report=BackportReport(target=inp.target, picked=tuple(picked), conflict=conflict),
                            cost=cost,
                        )
                    try:
                        work.git("add", "-A", check=True)
                        # `--no-edit` is not enough here: `--continue` opens an editor for the
                        # message, and a backport must not block on one in CI.
                        work.git(
                            *work.identity(),
                            "-c",
                            "core.editor=true",
                            "cherry-pick",
                            "--continue",
                            check=True,
                        )
                    except RuntimeError as e:
                        return _failed(
                            "backport.resolution_rejected",
                            f"the resolved contents did not complete the pick of {sha[:12]}: {e}",
                            report=BackportReport(target=inp.target, picked=tuple(picked), conflict=conflict),
                            cost=cost,
                        )
                    resolved.extend(c.path for c in files)
                    findings.extend(
                        Finding(
                            id="backport.resolved_by_model",
                            message=f"the merged contents of {c.path} for {sha[:12]} were written "
                            f"by a model, not by git — review this file first.",
                            severity=Severity.WARNING,
                            path=c.path,
                        )
                        for c in files
                    )
                picked.append(PickedCommit(sha=sha, subject=subject))

            changeset = _changeset_between(work, base, inp)

        report = BackportReport(
            changeset=changeset,
            target=inp.target,
            picked=tuple(picked),
            resolved=tuple(resolved),
            summary=changeset.summary,
        )
        if report.empty:
            # Every pick applied and changed nothing: the commits are already on the target.
            # SUCCEEDED with nothing staged, said plainly, so a re-run is idempotent rather than
            # a fresh pull request proposing an empty diff.
            findings.append(
                Finding(
                    id="backport.already_present",
                    message=f"the picked commit(s) produce no change against {inp.target}; "
                    f"nothing to propose.",
                    severity=Severity.NOTE,
                )
            )
        return Outcome(
            status=Status.SUCCEEDED,
            value=report,
            cost=cost,
            findings=tuple(findings),
        )

    def _discover(
        self, repo: GitLocal, inp: BackportSpec, start: str
    ) -> tuple[str, ...] | Outcome[BackportReport]:
        """Commits on `source` since it diverged from the target, filtered by `Ticket:` trailer."""
        key = str(getattr(inp.ticket, "key", "") or "")
        if not key:
            return _blocked(
                "backport.no_commits",
                "name the commits, or a ticket whose `Ticket:` trailer finds them.",
            )
        try:
            merge_base = repo.merge_base(start, inp.source)
        except RuntimeError as e:
            return _blocked("backport.no_merge_base", str(e))
        return tuple(
            c.sha for c in repo.commits_between(merge_base, inp.source) if c.trailers.get("Ticket") == key
        )


def _unmerged(work: GitLocal) -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in work.git("diff", "--name-only", "--diff-filter=U").splitlines()
        if line.strip()
    )


def _conflicted_files(tree: str, paths: tuple[str, ...]) -> tuple[FileChange, ...]:
    """The conflicted contents, markers and all. A path git deleted on one side may be absent —
    represented as a deletion rather than invented as empty."""
    out: list[FileChange] = []
    for path in paths:
        candidate = Path(tree) / path
        out.append(
            FileChange(path=path, contents=candidate.read_text(errors="replace"))
            if candidate.is_file()
            else FileChange(path=path, contents=None)
        )
    return tuple(out)


def _write_resolutions(tree: str, conflict: Conflict, files: tuple[FileChange, ...]) -> str | None:
    """Land the resolver's answer in the worktree, or say why not.

    A resolution may only touch the conflicted paths: a resolver that writes elsewhere is not
    resolving, it is implementing, and that is a different verb with a different gate. The write
    itself goes through the worktree materialiser's `_apply_change`, so the containment and
    symlink rules are enforced in one place rather than re-derived here — a rule enforced at two
    points drifts into two rules.
    """
    from .worktree import _apply_change

    allowed = set(conflict.paths)
    for change in files:
        if change.path not in allowed:
            return (
                f"the resolution writes {change.path!r}, which is not one of the conflicted "
                f"paths {sorted(allowed)}. Resolving a conflict may only merge the files that "
                f"conflict."
            )
        try:
            _apply_change(Path(tree), change)
        except WorktreeError as e:
            return str(e)
    return None


def _changeset_between(work: GitLocal, base: str, inp: BackportSpec) -> ChangeSet:
    """The picks as one ChangeSet relative to the target line.

    `--no-renames` on purpose: a rename becomes a delete and an add, which are the two operations
    `FileChange` can say. Contents are read text-oriented (`errors="replace"`), the same choice
    `head_state` documents — a ChangeSet cannot carry bytes.
    """
    key = str(getattr(inp.ticket, "key", "") or "")
    changes: list[FileChange] = []
    listing = work.git("diff", "--name-status", "--no-renames", f"{base}..HEAD")
    for line in listing.splitlines():
        status, sep, path = line.partition("\t")
        if not sep:
            continue
        if status.strip().startswith("D"):
            changes.append(FileChange(path=path, contents=None))
        else:
            contents = (Path(work.root) / path).read_text(errors="replace")
            changes.append(FileChange(path=path, contents=contents))
    subjects = work.git("log", "--format=%s", f"{base}..HEAD").splitlines()
    first = subjects[-1] if subjects else "changes"
    summary = f"{first} (backport to {inp.target})"
    return ChangeSet(changes=tuple(changes), summary=summary, ticket=key)


def _conflict_outcome(
    inp: BackportSpec, conflict: Conflict, picked: list[PickedCommit], cost: Cost
) -> Outcome[BackportReport]:
    """The honest stop: which pick, which files, and the exact commands a person runs.

    FAILED rather than BLOCKED — no control refused anything; git met a conflict, which is the
    ordinary hazard of the operation.
    """
    shas = " ".join(p.sha for p in picked) + (" " if picked else "") + conflict.commit
    manual = f"git checkout -b backport-{conflict.commit[:8]} {inp.target} && git cherry-pick -x {shas}"
    return Outcome(
        status=Status.FAILED,
        reason="backport.conflict",
        value=BackportReport(target=inp.target, picked=tuple(picked), conflict=conflict),
        cost=cost,
        findings=(
            Finding(
                id="backport.conflict",
                message=f"picking {conflict.commit[:12]} ({conflict.subject}) onto {inp.target} "
                f"conflicts in {len(conflict.paths)} file(s). Resolve by hand:  {manual}  — or "
                f"re-run with a resolver bound (`backport --resolve`).",
                severity=Severity.ERROR,
                blocking=True,
            ),
            *(
                Finding(
                    id="backport.conflicted_path",
                    message=f"{path} conflicts when picking {conflict.commit[:12]}",
                    severity=Severity.WARNING,
                    path=path,
                )
                for path in conflict.paths
            ),
        ),
    )


def _blocked(reason: str, message: str) -> Outcome[BackportReport]:
    return Outcome.blocked_by(
        reason,
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )


def _failed(reason: str, message: str, *, report: BackportReport, cost: Cost) -> Outcome[BackportReport]:
    return Outcome(
        status=Status.FAILED,
        reason=reason,
        value=report,
        cost=cost,
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )
