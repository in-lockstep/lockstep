"""Plain git, and the interface host adapters extend."""

from __future__ import annotations

import re
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

# Conventional Commits. Every commit a workflow creates has to be one, so squash-merge titles,
# changelog generation and semver tooling can read it — a model's free-prose summary cannot be
# trusted to be. Commits that predate a workflow are untouched; this only shapes what the framework
# writes from here on.
CONVENTIONAL_TYPES = (
    "feat",
    "fix",
    "docs",
    "style",
    "refactor",
    "perf",
    "test",
    "build",
    "ci",
    "chore",
    "revert",
)
_CONVENTIONAL = re.compile(r"^(?:" + "|".join(CONVENTIONAL_TYPES) + r")(?:\([^)]+\))?!?: .")
# What type a workflow's change is, when its summary does not already declare one. A conservative
# map: implementing a ticket is a feature, fixing and backporting are fixes, and anything unnamed
# is a chore rather than a guessed feature.
_TYPE_FOR_WORKFLOW = {"implement": "feat", "fix": "fix", "backport": "fix", "rfe": "feat"}


def is_conventional(subject: str) -> bool:
    """Whether a commit subject already follows Conventional Commits (`type: description`)."""
    return bool(_CONVENTIONAL.match(subject.strip()))


def conventional_subject(subject: str, *, workflow: str) -> str:
    """`subject` in Conventional Commits form for a `workflow` commit.

    If the summary already declares a type it is kept — a model that wrote `fix: …` is taken at its
    word — otherwise the workflow's mapped type is prefixed. Only the first line (the subject) is
    shaped; a body is carried through unchanged.
    """
    subject = subject.strip() or "changes"
    first, sep, rest = subject.partition("\n")
    if is_conventional(first):
        return subject
    prefixed = f"{_TYPE_FOR_WORKFLOW.get(workflow, 'chore')}: {first}"
    return f"{prefixed}{sep}{rest}" if sep else prefixed


#: The longest title a host will accept on a change request. GitHub refuses at 256 with a GraphQL
#: error rather than truncating, and it refuses at the very end — after the branch is pushed, after
#: the model has been paid for. GitLab's ceiling is higher, so the smallest wins: one number means a
#: change that opens on one host opens on the other.
MAX_TITLE_CHARS = 256


def title_line(subject: str) -> str:
    """One line, host-safe, for a change request title.

    Separate from `conventional_subject` because a commit message may have a body and a title may
    not — and the same string was being used for both. A summary with newlines became a multi-line
    title, and a long one became an HTTP 400.

    Run 33578430422 died exactly here. The model had done the work and the suite was green (1631
    passed), and then the title — a thousand characters of the model's own running commentary, taken
    from `changeset.summary` — was refused by the API. The work survived only because the changeset
    was in the run's artifact.

    Clamping is the floor and not the fix. A title should come from the ticket, which a person
    wrote; the workflows do that now, and this stands behind them because a repository can bind any
    strategy and a summary is model output whatever it is used for.
    """
    first = " ".join(subject.strip().split("\n", 1)[0].split())
    if not first:
        return "changes"
    if len(first) <= MAX_TITLE_CHARS:
        return first
    # An ellipsis rather than a hard cut, so a reader can see the title was clipped rather than
    # wondering why it stops mid-word.
    return first[: MAX_TITLE_CHARS - 1].rstrip() + "…"


def change_body(body: str, trailers: dict[str, str]) -> str:
    """The rendered half a human reads, plus a machine-readable block.

    Both, deliberately: a reviewer should not have to parse JSON, and a later run should not have
    to parse prose. Shared by every host adapter, so a change request reads the same on GitHub and
    GitLab and a tool that parses the block needs one parser.
    """
    import json

    block = json.dumps(trailers, indent=2, sort_keys=True)
    return f"{body}\n\n<details><summary>in-lockstep</summary>\n\n```json\n{block}\n```\n\n</details>"


#: A ticket key as a branch segment can hold it: digits (`218`), or a tracker key (`PROJ-123`).
#: Used only to decide whether a branch segment IS a ticket, never to validate one — `branch_for`
#: accepts whatever a tracker calls a key and sanitises it.
_LOOKS_LIKE_A_KEY = re.compile(r"^(?:\d+|[A-Za-z][A-Za-z0-9]*-\d+)$")


def trailers_from(body: str) -> dict[str, str]:
    """Read back the machine-readable block `change_body` wrote, or an empty dict.

    The pair to `change_body`, and here for the same reason `branch_key` is its own function: two
    spellings of one format is one of them drifting. A change request records the ticket it was
    opened for, and that record is what lets a comment left on the pull request resolve to the
    work it is about instead of to the pull request's own number.
    """
    import json

    match = re.search(r"<details><summary>in-lockstep</summary>.*?```json\s*(\{.*?\})\s*```", body, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def ticket_from_branch(branch: str) -> str:
    """The ticket key `branch_for` put in a run branch, or empty when it did not put one there.

    A fallback for a change request whose body somebody edited, and deliberately a cautious one.
    `in-lockstep/<workflow>/<ticket>/<run-id>` cannot be read positionally on its own, because the
    workflow segment may itself contain a slash — `in-lockstep/fix/from-ticket/run-9` and
    `in-lockstep/fix/218/run-9` have the same shape and only one of them names a ticket.

    So the candidate has to LOOK like a key. That is a heuristic and it is the honest kind: it can
    only decline to resolve a real ticket, never resolve the wrong one, because a workflow segment
    shaped like `218` or `PROJ-1` would have to be a deliberate collision with a key format.
    """
    parts = branch.split("/")
    if len(parts) < 4 or parts[0] != RUN_BRANCH_PREFIX:
        return ""
    candidate = parts[-2]
    return candidate if _LOOKS_LIKE_A_KEY.match(candidate) else ""


class DirectPushRefused(Exception):
    """A write was attempted outside the run-scoped namespace."""


class GuardRefused(Exception):
    """A change touches a protected path."""

    def __init__(self, refusals: list[object]) -> None:
        super().__init__(f"{len(refusals)} protected path(s) refused")
        self.refusals = refusals


def branch_key(ticket: str) -> str:
    """A ticket key as it appears in a branch name. Empty when there is no key.

    Its own function because two things depend on producing the identical string: `branch_for`
    writes it, and `is_run_branch_for` reads it back to find the change requests opened for a
    ticket. Two spellings of one sanitisation is one of them drifting, and the failure would be
    silent — a run that simply never finds its own pull request.
    """
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in ticket.lstrip("#")).strip("-")


def workflow_slug(workflow: str) -> str:
    """A workflow name as it appears in a run branch.

    Its own function for exactly the reason `branch_key` is: `branch_for` writes this and
    `is_run_branch_of` reads it back, and two spellings of one sanitisation is one of them
    drifting. The failure would be silent in the worst way here — a ceiling that counts the open
    proposals of a workflow whose branches it can no longer recognise, and therefore always
    counts zero.
    """
    return "".join(c if c.isalnum() or c in "-_/" else "-" for c in workflow)


def branch_for(workflow: str, run_id: str, *, ticket: str = "") -> str:
    """`in-lockstep/<workflow>/<ticket>/<run-id>`, the ticket segment omitted when there is none.

    The run id is what keeps two concurrent runs on the same ticket from colliding — that
    uniqueness is the design's whole concurrency story — so the ticket joins the name for the
    humans scanning a branch list (`git branch --list 'in-lockstep/*/59/*'`) and never replaces
    it. The ticket is a hierarchy segment of its own so that glob works; a leading `#` is
    stripped because shells treat it as a comment even though git would accept it.
    """
    safe = workflow_slug(workflow)
    key = branch_key(ticket)
    middle = f"{safe}/{key}" if key else safe
    return f"{RUN_BRANCH_PREFIX}/{middle}/{run_id}"


def is_run_branch(branch: str) -> bool:
    """Whether `branch` is one this framework opened, for any ticket.

    Split out of `is_run_branch_for` rather than spelled again beside it, because two readings of
    `branch_for`'s layout is one of them drifting — the argument `branch_key` already makes. The
    delivery metrics need this one: "every pull request we opened" has no ticket to key on, and
    matching on a title or a body would count a pull request somebody else wrote about our work.
    """
    return branch.startswith(f"{RUN_BRANCH_PREFIX}/")


def is_run_branch_for(branch: str, ticket: str) -> bool:
    """Whether `branch` is one this framework opened for `ticket`.

    Matched against `branch_for`'s layout rather than against text anywhere else, which is what
    makes it safe to feed the result into a prompt: a pull request that merely *mentions* the
    ticket — including one a stranger opened saying "fixes #218" — is not one of ours and its
    conversation is not gathered as though it were.
    """
    key = branch_key(ticket)
    if not key or not is_run_branch(branch):
        return False
    return f"/{key}/" in branch[len(RUN_BRANCH_PREFIX) :]


def is_run_branch_of(branch: str, workflow: str) -> bool:
    """Whether `branch` is one this framework opened FOR `workflow`.

    A prefix test rather than a positional read, and `ticket_from_branch` above says why: the
    workflow segment may itself contain a slash, so `parts[1]` is not the workflow.

    One direction is exact and the other cannot be. `implement/from-ticket` never matches a branch
    `implement` opened, which is the direction that matters — one workflow's ceiling must not be
    spent by another's work. But `implement` DOES match `implement/from-ticket`'s branches, because
    `in-lockstep/implement/218/run-2` and `in-lockstep/implement/from-ticket/run-2` are the same
    shape and no rule can separate them. Given that, a shallower name counts the family, which is
    the safe side for a ceiling: refusing a run that could have proceeded costs a person one
    command, and letting one through because a branch went unrecognised is the failure the number
    exists to prevent.
    """
    slug = workflow_slug(workflow)
    return bool(slug) and branch.startswith(f"{RUN_BRANCH_PREFIX}/{slug}/")


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


#: How many change requests one ticket's conversation is gathered from, and how much of each is
#: read. The same shape of cap `TicketSource` puts on issue comments, for the same reason: this
#: text goes into a prompt, and a prompt whose size is set by how talkative a pull request got is
#: a prompt with no ceiling.
MAX_CHANGES_READ = 3
MAX_REMARKS = 40
MAX_REMARK_CHARS = 4_000


@dataclass(frozen=True)
class Remark:
    """One thing somebody said on a change request.

    Three kinds, because a reviewer says different things in different places and flattening them
    loses the part that carries the most instruction. `comment` is the conversation thread.
    `review` is the summary attached to an approval or a request for changes — the verdict.
    `line` is a note pinned to a file and a line, which is the most specific thing a reviewer ever
    says and the one a model most needs located rather than paraphrased.

    Untrusted, like every other word a person can write at the framework. Nothing here is a
    command; it is evidence of what a human asked for, and it is tagged as such on the way in.
    """

    author: str
    body: str
    kind: str = "comment"
    #: For a `line` remark. Empty otherwise.
    path: str = ""
    line: int | None = None
    #: For a `review` remark: the verdict the host recorded — APPROVED, CHANGES_REQUESTED,
    #: COMMENTED. Carried because "this was a request for changes" is not recoverable from prose.
    state: str = ""

    def as_text(self, *, where: str = "") -> str:
        """One block for a prompt: who said it, where, and what.

        The location leads, because a reviewer writing "iterate the entries instead" on line 29 of
        one file has said something precise, and a model handed the sentence without the line has
        been handed an opinion.
        """
        at = f" on {where}" if where else ""
        if self.kind == "line" and self.path:
            spot = f"{self.path}:{self.line}" if self.line is not None else self.path
            head = f"{self.author or 'someone'} reviewed {spot}{at}"
        elif self.kind == "review":
            verdict = {"APPROVED": "approved", "CHANGES_REQUESTED": "requested changes"}.get(
                self.state.upper(), "reviewed"
            )
            head = f"{self.author or 'someone'} {verdict}{at}"
        else:
            head = f"{self.author or 'someone'} commented{at}"
        return f"{head}:\n{self.body.strip()}" if self.body.strip() else f"{head}."


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

    def rebase_onto(self, base: Ref) -> str:
        """Replay HEAD's commits onto `base`. Returns the new HEAD.

        The deterministic half of bringing a stale change forward, and the same claim
        `adapters/backport.py` makes about a cherry-pick: most rebases are git succeeding, for free,
        and a model is needed at exactly one point — a conflict.

        `start_point` because a CI checkout has the target only as `origin/main`; `identity()`
        because a rebase writes commits and an unconfigured runner has no author.

        A conflict raises with git's own message and leaves the tree mid-rebase, deliberately —
        `cherry_pick` gives the reason directly above: resolving one is a decision, and cleaning up
        silently here would discard exactly what a person needs in order to make it.
        `abort_rebase` is the honest retreat.
        """
        self.git(*self.identity(), "rebase", self.start_point(base), check=True)
        return self.head()

    def unmerged_paths(self) -> tuple[str, ...]:
        """Paths git reports as conflicted right now, sorted. Empty when the tree is clean.

        Sorted rather than in git's order, because this is read to describe a state to a person or
        to a resolver, and two reads of one tree should not differ by however the index happened to
        be walked. `adapters/backport.py` has its own unsorted reader for the same information;
        de-duplicating them is deliberately NOT done here — it returns git's order, and changing
        that inside a module with its own suite is a separate change with its own argument.
        """
        return tuple(
            sorted(
                line.strip()
                for line in self.git("diff", "--name-only", "--diff-filter=U").splitlines()
                if line.strip()
            )
        )

    def abort_rebase(self) -> None:
        """`git rebase --abort` — the caller's honest retreat from a conflicted tree.

        Not `check=True`: aborting when no rebase is in progress is a caller being careful rather
        than a caller being wrong, and raising there would make the safe spelling the awkward one.
        """
        self.git("rebase", "--abort")

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
                f"{RUN_BRANCH_PREFIX}/<workflow>/[<ticket>/]<run-id> only. Binding DirectPushScm is the "
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
        branch = branch_for(workflow or "change", run_id or "local", ticket=ticket)
        self.assert_run_scoped(branch)
        # Conventional Commits: this commit is created by a workflow, so its subject must be one.
        title = conventional_subject(title, workflow=workflow)
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
