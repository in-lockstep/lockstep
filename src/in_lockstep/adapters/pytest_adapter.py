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
import tempfile
from pathlib import Path
from typing import ClassVar

from ..core.outcome import Cost, Finding, Outcome, Severity, Status
from ..core.types import Resolution, Test, TestCase, TestReport
from ..core.verbs import Capability, Verb
from . import tooling
from .command import _refused
from .sandbox import Runner, Sandbox

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
        sandbox: Runner | None = None,
    ) -> None:
        self.args = args or ["-q"]
        self.cwd = cwd
        # Out of process by default. pytest executes conftest.py from the repository, so an
        # in-process run hands repository-authored Python the credentials this process holds.
        self.sandbox = sandbox or Sandbox()

    def locations(self, root: str) -> tuple[Resolution, ...]:
        """Where the suite will run from, for `ls` and `doctor`. The repository's, not this
        process's: see `tooling`."""
        return (tooling.interpreter(self.cwd or root, self.sandbox),)

    async def invoke(self, ctx: object, inp: Test) -> Outcome[TestReport]:
        # Resolved against the repository's root, not `inp.root`: a materialized worktree is a
        # copy of HEAD, and the environment (`.venv` is ignored by git) lives in the real tree.
        repo_root = self.cwd or getattr(getattr(ctx, "repo", None), "root", None)
        resolved = tooling.interpreter(repo_root, self.sandbox)
        if resolved.path is None:
            return Outcome.errored(
                f"no python interpreter for the repository; looked for {', '.join(resolved.tried)}"
            )
        interpreter = resolved.path

        report_dir = Path(tempfile.mkdtemp(prefix="in-lockstep-test-"))
        # `inp.root` (a materialized worktree) wins over the bound `cwd` wins over the repo's
        # root, so a staged change can be tested without rebinding this adapter -- and a bound
        # package directory is kept INSIDE the worktree rather than replaced by it, with the
        # model's root-relative paths rebased to it (`tooling.within`, GATE-TOOLING-4).
        paths = tuple(inp.paths or ())
        cwd: str | None
        if inp.root:
            cwd, package = tooling.within(
                inp.root, self.cwd, getattr(getattr(ctx, "repo", None), "root", None)
            )
            paths = tooling.rebase(paths, package)
        else:
            cwd = repo_root
        # Whichever branch set it, an absolute path inside `cwd` is made relative to it: the
        # same file on both sides of a container mount (GATE-TOOLING-4).
        paths = tooling.relative(paths, cwd)
        cmd = [
            interpreter,
            "-m",
            "pytest",
            *self.args,
            *inp.args,
            *paths,
        ]
        if inp.selector:
            cmd += ["-k", inp.selector]

        try:
            result = await self.sandbox.run(cmd, cwd=cwd)
        finally:
            shutil.rmtree(report_dir, ignore_errors=True)

        # A runner constructed to require a container and finding none ran nothing; that is the
        # control working, and reading its exit as "no summary, errored" told a strategy the
        # suite broke. `CommandTest` had this guard (#256) and this adapter did not.
        if (declined := _refused(result)) is not None:
            return declined

        exit_code = result.exit_code
        text = result.stdout + result.stderr
        report, summarized = _parse(text)
        # An interpreter without pytest is not a red suite. Reading it as one blamed the change
        # for the environment, which is the wrong number that gets acted on. The shape, not the
        # substring: `python -m pytest` with no pytest prints exactly one line and no summary,
        # where a red suite whose own output mentions the phrase still ends in a summary line.
        last = text.strip().splitlines()[-1] if text.strip() else ""
        if exit_code != 0 and report.total == 0 and last.endswith("No module named pytest"):
            return Outcome.errored(f"pytest is not installed in {interpreter} ({resolved.how})")

        # pytest exits 5 for "no tests collected". That is not a red suite and it is not a green
        # one either: nothing was decided. Reporting it as SUCCEEDED with decided=True would be
        # the reassuring number — a suite that ran nothing looking exactly like a suite that
        # passed everything.
        if exit_code == NO_TESTS_COLLECTED:
            return _undecided(report, "test.no_tests_collected", "no tests were collected")

        # No summary line at all is not a suite that ran and reported nothing wrong; it is a run
        # that never reported. A staged module calling `os._exit(0)` at import, or output that
        # never reached stdout, used to read as a green suite of zero tests -- and with
        # `expect="fail"`, `os._exit(1)` read as red -- so the number the writing verbs turn on
        # was a script's claim about a run that ran nothing (#313, GATE-VERDICT-1). A clean exit
        # with no summary decides nothing; a non-zero exit with no summary is the runner failing,
        # which is `errored` and not evidence about the change.
        if not summarized:
            # What it printed, because the exit code alone is not something anybody can act on.
            # A container job dispatching this got `pytest exited 4` and nothing else, and the
            # usage error behind it -- which pytest had written to stderr -- was recoverable
            # from no record, no log and no finding. Same argument as GATE-VERDICT-2 makes about
            # a failing test's message, one layer down: the runner failing IS the evidence here,
            # and a verdict that drops it sends the next reader back to reproduce what the run
            # already knew.
            printed = _tail(text)
            if exit_code != 0:
                return Outcome(
                    status=Status.ERRORED,
                    reason="test.no_summary",
                    value=report,
                    findings=(
                        Finding(
                            id="test.no_summary",
                            message=(
                                f"pytest exited {exit_code} without a summary line; the suite did not "
                                f"report, so nothing was decided about the change"
                                + (f". It printed: {printed}" if printed else " and printed nothing")
                            ),
                            severity=Severity.ERROR,
                        ),
                    ),
                )
            return _undecided(
                report,
                "test.no_summary",
                "pytest exited 0 without a summary line"
                + (f". It printed: {printed}" if printed else " and printed nothing"),
            )
        if report.passed + report.failed == 0:
            # A summary that counts nothing executed -- every test skipped or deselected -- is the
            # exit-5 case wearing a different exit code.
            return _undecided(report, "test.nothing_ran", "no test passed or failed")

        # A reproducer that does not fail proves nothing: `expect="fail"` inverts the verdict, so
        # a pipeline can assert red before a fix and green after it.
        red = report.red or exit_code != 0
        satisfied = (not red) if inp.expect == "pass" else red

        status = Status.SUCCEEDED if satisfied else Status.FAILED
        findings: tuple[Finding, ...] = ()
        if not satisfied:
            # The failing tests by name, because the number alone is not something anybody can
            # act on: this repository's first `/fix` on itself reported "9 failed of 2495" into
            # the ledger, the ticket and the log, and which nine was recoverable from none of
            # them (#312). The model's own `run_tests` result already listed them; the verdict
            # the record carries is for the next engineer (O12), who was told less.
            failing = [c for c in report.cases if c.outcome in ("failed", "error")]
            named = "; ".join(_named(c) for c in failing[:10]) + (
                f" (+{len(failing) - 10} more)" if len(failing) > 10 else ""
            )
            findings = (
                Finding(
                    id="test.expectation_unmet",
                    message=(
                        f"expected the suite to {inp.expect}, {report.failed} failed of {report.total}"
                        + (f": {named}" if named else "")
                    ),
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


def _tail(text: str) -> str:
    """The last of what the runner printed, on one line, bounded."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return " / ".join(lines)[-RUNNER_OUTPUT_CHARS:] if lines else ""


def _undecided(report: TestReport, finding: str, why: str) -> Outcome[TestReport]:
    """A suite that ran nothing is neither red nor green. Reporting it as SUCCEEDED with
    `decided=True` would be the reassuring number -- a suite that ran nothing looking exactly
    like a suite that passed everything -- which is why `decided` is on Outcome at all."""
    return Outcome(
        status=Status.SUCCEEDED,
        value=report,
        decided=False,
        findings=(Finding(id=finding, message=f"{why}; nothing was decided", severity=Severity.NOTE),),
    )


#: How much of a failure's message a verdict keeps. Enough for the assertion and its first
#: values; a message is one line of pytest's summary and rarely longer, but a parametrised
#: repr can run to kilobytes, and a finding is read on a ticket.
MESSAGE_CHARS = 240
#: How much of a silent runner's own output a finding carries. The tail, not the head: a usage
#: error, a traceback and an import failure all end with the sentence that names the cause, and
#: the banner above it is the part somebody can reconstruct. Sized so the whole thing survives
#: `Finding.as_record`'s own cap rather than being clipped again on the way to the ledger.
RUNNER_OUTPUT_CHARS = 320


def _case(line: str) -> TestCase:
    """One `FAILED <id> - <why>` line into a case that keeps both halves."""
    head, _, why = line.partition(" - ")
    return TestCase(
        id=head.split(maxsplit=1)[1].strip(),
        outcome=line.split()[0].lower(),
        message=why.strip()[:MESSAGE_CHARS],
    )


def _named(case: TestCase) -> str:
    return f"{case.id} - {case.message}" if case.message else case.id


def _parse(text: str) -> tuple[TestReport, bool]:
    """Read pytest's terminal summary, and say whether one was there to read.

    Tolerant about counts -- a missing count is 0, not a crash -- and strict about the line: the
    second value is False when no summary line was found at all, and the caller treats that as a
    run that never reported rather than as zeros (#313).
    """
    passed = failed = skipped = 0
    duration = 0.0
    summarized = False
    # The short test summary pytest prints by default (`-r fE`): one `FAILED <id> - <why>` or
    # `ERROR <id>` line per test that did not pass. Names, so the verdict can say WHICH nine --
    # and the `<why>`, kept as the case's message, so it can say what each one said. The tape
    # that held the full output dies with the runner, and the first `/implement` here failed on
    # three tests whose names survived and whose messages did not; one of them could not be
    # reproduced anywhere afterwards (GATE-VERDICT-2).
    cases = tuple(
        _case(line)
        for line in text.splitlines()
        if line.startswith(("FAILED ", "ERROR ")) and len(line.split()) > 1
    )
    for line in reversed(text.splitlines()):
        stripped = line.strip("= ")
        if any(word in stripped for word in (" passed", " failed", " error", " skipped", " deselected")):
            summarized = True
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
    report = TestReport(
        total=passed + failed + skipped,
        passed=passed,
        failed=failed,
        skipped=skipped,
        duration_seconds=duration,
        cases=cases,
    )
    return report, summarized
