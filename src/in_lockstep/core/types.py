"""The data types between verbs.

These are the contract that makes workflows composable: Diagnosis -> FixSpec -> ChangeSet ->
TestSpec -> TestReport. All frozen; all serialize losslessly, because they are checkpointed and
written to the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True)
class TestSpec:
    paths: tuple[str, ...] = ()
    selector: str = ""
    args: tuple[str, ...] = ()
    # A reproducer that does not fail proves nothing. A pipeline asserts the failure before the
    # fix and the pass after it; a test passing both times has said nothing about the bug.
    expect: str = "pass"  # "pass" | "fail"


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
class ValidateSpec:
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


@dataclass(frozen=True)
class BuildSpec:
    target: str = ""
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class BuildResult:
    artifacts: tuple[str, ...] = ()
    log: str = ""


@dataclass(frozen=True)
class RunSpec:
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
