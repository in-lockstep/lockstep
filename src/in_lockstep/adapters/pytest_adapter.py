"""Test, over pytest.

New code, not a port: the compiler-era `test-runner` drives committed browser/API scripts against
a running application, which is a different verb from running a repository's own suite.

Declares EXECUTES_CODE, and means it. pytest collects and executes `conftest.py` from every
directory on the rootdir path, so running a suite is running repository-authored Python. Phase 3
moves that out of process, away from anything holding credentials; until then the capability is
declared so policy can see it.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from typing import ClassVar

from ..core.outcome import Cost, Finding, Outcome, Severity, Status
from ..core.types import Test, TestReport
from ..core.verbs import Capability, Verb
from .sandbox import Sandbox

__all__ = ["PytestTest", "Test"]

# pytest's exit code for "no tests ran".
NO_TESTS_COLLECTED = 5


class PytestTest:
    verb: ClassVar[Verb] = Verb.TEST
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.EXECUTES_CODE, Capability.READS_REPO}
    )

    def __init__(
        self,
        args: list[str] | None = None,
        cwd: str | None = None,
        sandbox: Sandbox | None = None,
    ) -> None:
        self.args = args or ["-q"]
        self.cwd = cwd
        # Out of process by default. pytest executes conftest.py from the repository, so an
        # in-process run hands repository-authored Python the credentials this process holds.
        self.sandbox = sandbox or Sandbox()

    async def invoke(self, ctx: object, inp: Test) -> Outcome[TestReport]:
        interpreter = _interpreter(self.sandbox)
        if interpreter is None:  # pragma: no cover - defensive
            return Outcome.errored("no python interpreter on PATH")

        report_dir = Path(tempfile.mkdtemp(prefix="in-lockstep-test-"))
        cmd = [
            interpreter,
            "-m",
            "pytest",
            *self.args,
            *inp.args,
            *(inp.paths or ()),
        ]
        if inp.selector:
            cmd += ["-k", inp.selector]

        try:
            # `inp.root` (a materialized worktree) wins over the bound `cwd` wins over the repo's
            # root, so a staged change can be tested without rebinding this adapter.
            result = await self.sandbox.run(
                cmd, cwd=inp.root or self.cwd or getattr(getattr(ctx, "repo", None), "root", None)
            )
        finally:
            shutil.rmtree(report_dir, ignore_errors=True)

        exit_code = result.exit_code
        text = result.stdout + result.stderr
        report = _parse(text)

        # pytest exits 5 for "no tests collected". That is not a red suite and it is not a green
        # one either: nothing was decided. Reporting it as SUCCEEDED with decided=True would be
        # the reassuring number — a suite that ran nothing looking exactly like a suite that
        # passed everything.
        if exit_code == NO_TESTS_COLLECTED:
            return Outcome(
                status=Status.SUCCEEDED,
                value=report,
                decided=False,
                findings=(
                    Finding(
                        id="test.no_tests_collected",
                        message="no tests were collected; nothing was decided",
                        severity=Severity.NOTE,
                    ),
                ),
            )

        # A reproducer that does not fail proves nothing: `expect="fail"` inverts the verdict, so
        # a pipeline can assert red before a fix and green after it.
        red = report.red or exit_code != 0
        satisfied = (not red) if inp.expect == "pass" else red

        status = Status.SUCCEEDED if satisfied else Status.FAILED
        findings: tuple[Finding, ...] = ()
        if not satisfied:
            findings = (
                Finding(
                    id="test.expectation_unmet",
                    message=(f"expected the suite to {inp.expect}, {report.failed} failed of {report.total}"),
                    severity=Severity.ERROR,
                    blocking=True,
                ),
            )

        return Outcome(
            status=status,
            value=report,
            findings=findings,
            cost=Cost(wall_seconds=report.duration_seconds),
        )


def _interpreter(sandbox: object) -> str | None:
    """What to invoke pytest with, resolved for the runner that will execute it.

    A containerized run resolves the name inside the image, where this host's PATH says nothing
    about what exists — so the probe is skipped entirely and the plain name travels, as it always
    has.

    A host subprocess takes `sys.executable`: the interpreter running this process is the one
    whose environment was set up to run this repository's tooling, which is what `uv run
    in-lockstep` hands us and precisely what the old `python`-on-PATH lookup already resolved to
    under CI. Preferring it changes nothing there and fixes two cases where the name lookup was
    actively wrong:

      * `python` is not a given. Debian, Ubuntu and most slim images ship `python3` with no
        alias, and an unactivated virtualenv puts neither on PATH. The old check asked for
        `python` alone and returned ERRORED when it was absent — which a propose step then read
        as a red suite, because a verdict could only answer `green`. "Your change broke the
        build" when what happened is "this machine spells it python3" is a wrong number that
        gets acted on.
      * The first `python3` on PATH is often the wrong one. On a Mac with homebrew it is the
        system interpreter, which has no pytest installed, so the suite exits 1 with nothing
        collected — a red verdict earned by the wrong environment rather than by the change.

    The name lookup stays as a fallback for an embedding where `sys.executable` is empty.
    """
    if getattr(sandbox, "image", "") and getattr(sandbox, "runtime", lambda: None)():
        return "python"
    if sys.executable:
        return sys.executable
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _parse(text: str) -> TestReport:
    """Read pytest's terminal summary. Deliberately tolerant: a missing count is 0, not a crash."""
    passed = failed = skipped = 0
    duration = 0.0
    for line in reversed(text.splitlines()):
        stripped = line.strip("= ")
        if " passed" in stripped or " failed" in stripped or " error" in stripped:
            parts = stripped.replace(",", "").split()
            for i, token in enumerate(parts):
                if not token.isdigit():
                    continue
                count = int(token)
                label = parts[i + 1] if i + 1 < len(parts) else ""
                if label.startswith("passed"):
                    passed = count
                elif label.startswith(("failed", "error")):
                    failed += count
                elif label.startswith("skipped"):
                    skipped = count
            for token in parts:
                if token.endswith("s") and token[:-1].replace(".", "", 1).isdigit():
                    duration = float(token[:-1])
            break
    return TestReport(
        total=passed + failed + skipped,
        passed=passed,
        failed=failed,
        skipped=skipped,
        duration_seconds=duration,
    )
