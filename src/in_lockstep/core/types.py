"""The data types between verbs.

These are the contract that makes workflows composable: Diagnosis -> Fix -> ChangeSet ->
Test -> TestReport. All frozen; all serialize losslessly, because they are checkpointed and
written to the ledger.

A verb request (`Test`, `Validate`, ...) is both the payload and the dispatch key: workflows do
`ctx.do(Test(...))`, and the request's type is what the container resolves an adapter for.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


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
    #: How many of `failed` are in files this change did not touch.
    #:
    #: Two states that look identical in a count and mean opposite things. A change whose OWN tests
    #: are red does not satisfy its own specification and is not proposable. A change that is
    #: complete while the suite is red somewhere else is a fact about the repository, or about the
    #: environment the suite ran in, and destroying the change to report it spends a run and
    #: delivers nothing. Run 34363672287 was told "the implementation did not make the staged test
    #: pass" about a change whose own 35 tests passed and whose four failures were in a file it
    #: never touched, and $11.29 of correct work went in the bin (#405).
    #:
    #: A count rather than the names, because a verdict is counts and a status so it serialises on
    #: the redacted side of the artifact; the names travel as findings.
    elsewhere: int = 0

    @property
    def green(self) -> bool:
        """The suite ran something, and everything it ran passed.

        `passed > 0`, not only `failed == 0`: a verdict whose counts are all zero is a suite that
        ran nothing, and the writing verbs turn a change into a pull request on this property.
        The adapter already declines to decide such a run, so the guard here is the second lock
        on the same door -- a verdict read back from an artifact somebody else wrote does not get
        to be green on a status alone (#313).
        """
        return self.decided and self.status == "succeeded" and self.failed == 0 and self.passed > 0

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
    def of(
        cls, status: str, decided: bool, report: TestReport, *, changed: tuple[str, ...] = ()
    ) -> TestVerdict:
        """From a Test Outcome's status/decided flags and its report. Kept to primitives so `core`
        need not import `Outcome` — the caller unpacks `outcome.status.value` and `outcome.decided`.

        `changed` is the paths the change under test staged. Given them, the verdict can say how
        many failures are somebody else's; without them it says nothing, which is the honest answer
        for a caller that did not know.
        """
        return cls(
            status=status,
            decided=decided,
            total=report.total,
            passed=report.passed,
            failed=report.failed,
            skipped=report.skipped,
            elsewhere=len(failures_elsewhere(report, changed)) if changed else 0,
        )

    @property
    def only_elsewhere(self) -> bool:
        """Red, and every failure is in a file this change did not touch.

        The state that must not be reported as a change failing its own test. `failed` is compared
        rather than trusted alone: a run with no failures is not "only elsewhere", and a partial
        overlap — some of its own tests red as well — is the model's to fix like any other.
        """
        return self.red and self.failed > 0 and self.elsewhere == self.failed


def failures_elsewhere(report: TestReport, changed: tuple[str, ...]) -> tuple[str, ...]:
    """The failing cases whose file is not one this change staged.

    A pytest node id is `path/to/test_x.py::test_y`, so the file is what precedes the first `::`.
    A case whose id carries no such path -- a collection error, a runner that reports names rather
    than ids -- is counted as the change's own: an unattributable failure is not evidence that
    somebody else broke something, and the safe direction is the one that keeps the model
    responsible for it.
    """
    mine = {path.replace("\\", "/") for path in changed}
    out = []
    for case in report.cases:
        if case.outcome not in ("failed", "error"):
            continue
        head, sep, _ = case.id.partition("::")
        where = head.replace("\\", "/")
        if not sep or not where or where in mine:
            continue
        out.append(case.id)
    return tuple(out)


@dataclass(frozen=True)
class Validate:
    """The Validate request. Workflows do `ctx.do(Validate(...))`; a binding decides what runs it."""

    paths: tuple[str, ...] = ()
    rules: tuple[str, ...] = ()
    fix: bool = False
    #: Validate this tree instead of the repository. The field `Test` already has, for the same
    #: caller: a session's writes are staged rather than on disk, so the only way to check them is
    #: to materialise them into a worktree and point the verb at it (`run_validate`).
    root: str = ""
    #: Run it HERE rather than wherever the binding says (#419). The same contract `root` has, on
    #: the other axis: `root` is what to check and this is where to execute, both supplied by the
    #: caller that knows and both honoured by an adapter that reads them. It exists because one
    #: binding serves two callers with different requirements -- a person's `selfcheck` validates
    #: the working tree on the host, and a model's staged change must be executed in a container --
    #: and a `sandbox=` declared once cannot be both. Absent means the binding's own, which is
    #: every path a person drives.
    runner: Any = None


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
    #: The rest of the cover note: what the model said a reviewer should know about the approach.
    #: On the change set rather than only on the strategy's report, because the report stops at the
    #: work job and the change set is what crosses the artifact into `propose` — where a pull
    #: request body is written. The first fix this repository's own loop opened (#343) arrived
    #: titled `Fix #319` over a body that said nothing, while the model's two-sentence summary and
    #: four notes sat in a report nothing downstream read.
    notes: tuple[str, ...] = ()

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


#: Where this platform's virtual environments keep their executables, relative to the
#: environment's root. In `core` rather than beside the adapters that look there, because
#: detection (the `lockstep` layer, which may not import `adapters`) names the same path when it
#: binds a `requirements.txt` repository's second provisioning step: the interpreter the first step
#: creates is the one the install must run from, and two spellings of one layout is how what `ls`
#: prints and what runs stop agreeing.
VENV_BIN: tuple[str, ...] = (".venv", "Scripts") if os.name == "nt" else (".venv", "bin")


@dataclass(frozen=True)
class Provision:
    """The Provision request: build the repository's own environment, from its own lockfile.

    `CommandProvision` serves it over the steps detection bound (`uv sync --locked`, `npm ci`, a
    Makefile `deps` target) or a module bound by hand. The scaffolded work jobs run it first,
    before `doctor`, because an installed `in-lockstep` runs from an interpreter with nothing of
    the repository's in it, and the suite a strategy runs to prove a change needs the
    repository's (#185). `root` overrides where the steps run, as `Test.root` does.
    """

    root: str = ""


@dataclass(frozen=True)
class ProvisionResult:
    #: Each step as it ran, argv joined, in order: what a record says was provisioned.
    steps: tuple[str, ...] = ()
    log: str = ""


@dataclass(frozen=True)
class Build:
    """The Build request. `CommandBuild` serves it over whatever command a repository builds with,
    and detection binds one from a Makefile `build` target or a package.json `build` script.

    `target` is passed to the command as a positional argument (`make build release`), and `args`
    verbatim after it. A runner that takes targets some other way is bound with the argv it needs.
    """

    target: str = ""
    args: tuple[str, ...] = ()
    #: Build this tree instead of the repository. The field `Test` has had and `Validate` gained
    #: in #390, for the same caller and the same reason: a session's writes are staged rather than
    #: on disk, so building them means materialising them into a worktree and pointing the verb
    #: there. A change that does not compile is the first thing worth knowing about it.
    root: str = ""


@dataclass(frozen=True)
class BuildResult:
    artifacts: tuple[str, ...] = ()
    log: str = ""


@dataclass(frozen=True)
class Run:
    """The Run request. `CommandRun` serves it over whatever a repository runs itself with, and
    detection binds one from a Makefile `run` target or a package.json `start` script.

    Run to completion: the exit code is the verdict and the output is the result. `command` is
    appended to the bound argv, `cwd` overrides the bound working directory, and `env` is carried
    into the sandbox beside the variables it already allows through.
    """

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


@dataclass(frozen=True)
class Resolution:
    """Where a deterministic adapter found the tool it runs, or that it did not.

    A detected binding promises to run the repository's own tooling. An installed copy of this
    framework has an interpreter of its own with nothing of the repository's in it, and running
    pytest from there told two first-time users "ruff is not installed" and "test failed" about
    repositories that had both (#167). `ls` prints one of these per tool so a wrong answer is
    visible before a run, and `doctor` refuses on one that found nothing.
    """

    tool: str
    path: str | None
    how: str
    tried: tuple[str, ...] = ()
    #: An argv that proves the tool is usable from `path`, for `doctor` to run. Empty when there
    #: is nothing cheap to prove.
    probe: tuple[str, ...] = ()

    def render(self) -> str:
        if self.path is None:
            return f"{self.tool}  not found  (looked for {', '.join(self.tried)})"
        return f"{self.tool}  {self.path}  ({self.how})"


@runtime_checkable
class Locatable(Protocol):
    """An adapter that can say where the tools it runs come from. `ls` and `doctor` ask."""

    def locations(self, root: str) -> tuple[Resolution, ...]: ...
