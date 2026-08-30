"""The data types between verbs.

These are the contract that makes workflows composable: Diagnosis -> Fix -> ChangeSet ->
Test -> TestReport. All frozen; all serialize losslessly, because they are checkpointed and
written to the ledger.

A verb request (`Test`, `Validate`, ...) is both the payload and the dispatch key: workflows do
`ctx.do(Test(...))`, and the request's type is what the container resolves an adapter for.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True)
class Test:
    """The Test request. Workflows do `ctx.do(Test(...))`; a binding decides what runs it."""

    paths: tuple[str, ...] = ()
    selector: str = ""
    args: tuple[str, ...] = ()
    # A reproducer that does not fail proves nothing. A pipeline asserts the failure before the
    # fix and the pass after it; a test passing both times has said nothing about the bug.
    expect: str = "pass"  # "pass" | "fail"
    # Where to run, when it is not the live working tree. A staged `ChangeSet` cannot be tested in
    # place — the change is not on disk yet, and running the suite over the unchanged tree would
    # measure the wrong thing. `materialize` writes HEAD plus the change into a throwaway worktree
    # and names it here, so `ctx.do(Test(root=that))` runs the suite against the change
    # without touching the real tree. Empty keeps the adapter's own default (`ctx.repo.root`).
    root: str = ""


@dataclass(frozen=True)
class TestCase:
    id: str
    outcome: str  # "passed" | "failed" | "skipped" | "error"
    duration_seconds: float = 0.0
    message: str = ""


@dataclass(frozen=True)
class TestReport:
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    cases: tuple[TestCase, ...] = ()
    # First-class, from 1.0, empty until a retry-detection adapter fills it. It is a field on a
    # frozen type that is checkpointed and ledgered, so adding it later would change a serialized
    # layout — and its absence is what lets a flaky suite send a repair loop into a cycle, or
    # worse, teach a fix strategy that deleting the test is what "green" means.
    flaky: tuple[str, ...] = ()
    coverage_percent: float | None = None
    duration_seconds: float = 0.0

    @property
    def red(self) -> bool:
        return self.failed > 0


@dataclass(frozen=True)
class TestVerdict:
    """Whether a staged change's suite ran, and how it came out.

    Carried across the trampoline's job split. `implement/from-ticket` runs the suite against the
    materialised change and records this beside the ChangeSet; `implement/propose` — a job that
    never held the run's Outcome — reads it back and says in the PR body whether the change was
    tested and passed. Its *absence* (no verdict alongside the ChangeSet) is the honest third state:
    no Test was bound, so nothing was checked. `decided=False` is the fourth: the suite ran but
    collected nothing, which is neither red nor green.

    Counts and a status, never file contents — so it serialises on the redacted-metadata side of
    the artifact without the masking a source file would need.
    """

    status: str = ""  # the Test Outcome's status, e.g. "succeeded" | "failed"
    decided: bool = False
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def green(self) -> bool:
        return self.decided and self.status == "succeeded" and self.failed == 0

    @property
    def red(self) -> bool:
        """The suite ran, and the change made it fail.

        Deliberately NOT `not green`, and the difference is what a caller does about it. An
        ERRORED run — no interpreter resolved, a container refused, the runner never started —
        carries `decided=True` and is green in no sense, but it learned nothing about the change.
        A propose step that escalates on `not green` turns a broken runner into a bug report filed
        against code that may be perfectly fine, and then spends the loop's attempts on it.

        Three states, and each wants a different answer: red escalates, green is ready for review,
        and everything else (errored, nothing collected, no verb bound) is a draft for a human,
        because what happened is that nobody knows yet.
        """
        return self.decided and self.status == "failed"

    @classmethod
    def of(cls, status: str, decided: bool, report: TestReport) -> TestVerdict:
        """From a Test Outcome's status/decided flags and its report. Kept to primitives so `core`
        need not import `Outcome` — the caller unpacks `outcome.status.value` and `outcome.decided`."""
        return cls(
            status=status,
            decided=decided,
            total=report.total,
            passed=report.passed,
            failed=report.failed,
            skipped=report.skipped,
        )


@dataclass(frozen=True)
class Validate:
    """The Validate request. Workflows do `ctx.do(Validate(...))`; a binding decides what runs it."""

    paths: tuple[str, ...] = ()
    rules: tuple[str, ...] = ()
    fix: bool = False


@dataclass(frozen=True)
class ValidationFinding:
    rule: str
    message: str
    path: str = ""
    line: int | None = None


@dataclass(frozen=True)
class ValidationReport:
    findings: tuple[ValidationFinding, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.findings


class ChangeAuthor(Enum):
    """Who wrote a file change.

    The guard runs over agent-authored entries only. Without this the framework's own ledger
    write — which lands under a path the guard denies absolutely — would be refused by its own
    policy.
    """

    AGENT = "agent"
    FRAMEWORK = "framework"


@dataclass(frozen=True)
class FileChange:
    path: str
    contents: str | None = None  # None means deletion
    author: ChangeAuthor = ChangeAuthor.AGENT
    mode: int | None = None
    symlink_target: str | None = None

    @property
    def deleted(self) -> bool:
        return self.contents is None


@dataclass(frozen=True)
class ChangeSet:
    changes: tuple[FileChange, ...] = ()
    summary: str = ""
    ticket: str = ""

    def paths(self) -> tuple[str, ...]:
        return tuple(c.path for c in self.changes)

    def by_author(self, author: ChangeAuthor) -> tuple[FileChange, ...]:
        return tuple(c for c in self.changes if c.author is author)

    def inverse(self, before: Mapping[str, FileChange | None]) -> ChangeSet:
        """The change that undoes this one, given the pre-image `before`.

        `before` maps each changed path to the `FileChange` that reproduces its state before this
        change — its contents, mode and symlink target — or `None` if the path did not exist. This
        cannot be computed from the ChangeSet alone: a `FileChange` records only its post-change
        state, so reverting a modification needs the bytes that were there before, and reverting a
        deletion needs the file that was removed. Those live in the tree the change was applied
        against, not in the change — a materialiser reads them and hands them here.

        A path this change created (absent in `before`) inverts to a deletion; any other path
        inverts to whatever `before` says was there. Reverting a revert restores the change, because
        `before` is the same either way.
        """
        reverted: list[FileChange] = []
        for change in self.changes:
            prior = before.get(change.path)
            if prior is None:
                reverted.append(FileChange(path=change.path, contents=None, author=change.author))
            else:
                reverted.append(prior)
        return ChangeSet(
            changes=tuple(reverted),
            summary=f"revert: {self.summary}" if self.summary else "revert",
            ticket=self.ticket,
        )


@dataclass(frozen=True)
class Build:
    """The Build request. No shipped adapter serves it yet; the type reserves the shape."""

    target: str = ""
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class BuildResult:
    artifacts: tuple[str, ...] = ()
    log: str = ""


@dataclass(frozen=True)
class Run:
    """The Run request. No shipped adapter serves it yet; the type reserves the shape."""

    command: tuple[str, ...] = ()
    cwd: str = ""
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RunResult:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class SelfCheckReport:
    """What `in-lockstep run selfcheck` produced — the phase-1 proof of life."""

    validate: ValidationReport = field(default_factory=ValidationReport)
    tests: TestReport = field(default_factory=TestReport)
