"""Test, Validate, Build and Run over an arbitrary command.

The deterministic verbs are interfaces, and pytest/ruff are one implementation each. A repository
that is not Python needs the same verbs served by `npm test` or `eslint`, and these are that: a
command runs, its exit code maps to an `Outcome` the same way `PytestTest` maps pytest's, and —
when the runner writes one — a JUnit report is read so `TestReport.cases` carries per-test results
instead of only an exit code. Without that last part a non-pytest suite could say red or green but
never which test failed, which is exactly what a fix loop needs to reproduce.

Build and Run have no pytest or ruff of their own: an exit code is the whole answer, so the command
adapters are the only shipped implementations, and detection binds them to the `make build`,
`make run`, `npm run build` or `npm start` a repository already has.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import ClassVar

from ..core.outcome import Cost, Finding, Outcome, Severity, Status
from ..core.types import (
    Build,
    BuildResult,
    Run,
    RunResult,
    Test,
    TestCase,
    TestReport,
    Validate,
    ValidationReport,
)
from ..core.verbs import Capability, Verb
from .sandbox import Sandbox

__all__ = [
    "Build",
    "CommandBuild",
    "CommandRun",
    "CommandTest",
    "CommandValidate",
    "Run",
    "Test",
    "Validate",
    "parse_junit",
]

# How much of a failed command's output travels in the finding. Enough to see why, not the log.
_TAIL_LINES = 20


def _tail(stdout: str, stderr: str) -> str:
    """The last lines a command printed: stdout, then stderr, neither glued to the other.

    Concatenating the two strings put stdout's last line and stderr's first on one line whenever
    stdout ended without a newline, which showed a line nothing printed. Empty when nothing was
    printed, so a message that appends this ends where its own text does.
    """
    text = "\n".join(s.rstrip("\n") for s in (stdout, stderr) if s.strip())
    return "\n".join(text.splitlines()[-_TAIL_LINES:])


def _exited(cmd: list[str] | tuple[str, ...], code: int, tail: str) -> str:
    return f"{' '.join(cmd)} exited {code}" + (f"\n{tail}" if tail else "")


class CommandTest:
    """`Test` over any runner. `command` is the argv prefix, e.g. `("npm", "test")`."""

    verb: ClassVar[Verb] = Verb.TEST
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.EXECUTES_CODE, Capability.READS_REPO}
    )

    def __init__(
        self,
        command: list[str] | tuple[str, ...],
        *,
        cwd: str | None = None,
        sandbox: Sandbox | None = None,
        junit: str = "",
        selector_arg: tuple[str, ...] = (),
    ) -> None:
        if not command:
            raise ValueError("CommandTest needs a command to run")
        self.command = tuple(command)
        self.cwd = cwd
        # Out of process by default, like PytestTest: a repository's test command runs
        # repository-authored code, and dropping the ambient credentials is the whole point.
        self.sandbox = sandbox or Sandbox()
        # An optional path (relative to cwd) to a JUnit XML report the runner writes — e.g.
        # `jest --reporters=jest-junit` or `pytest --junitxml=`. Read after the run so
        # `TestReport.cases` is populated; absent, the report carries counts from the exit code.
        self.junit = junit
        # How this runner narrows to one test, so `Test.selector` is honoured rather than
        # silently dropped — pytest's `-k`, jest's `--testNamePattern`, go's `-run`. Empty means
        # the runner is not told, and a selector then widens to the whole suite; a reproducer that
        # needs one test isolated must supply this, or the narrowing is lost.
        self.selector_arg = tuple(selector_arg)

    async def invoke(self, ctx: object, inp: Test) -> Outcome[TestReport]:
        # A per-call `root` (a materialized worktree) wins over the bound `cwd` wins over the
        # repo's root: the spec is how a workflow points one bound adapter at a staged change
        # without rebinding it.
        cwd = inp.root or self.cwd or getattr(getattr(ctx, "repo", None), "root", None)
        selector = [*self.selector_arg, inp.selector] if (inp.selector and self.selector_arg) else []
        cmd = [*self.command, *selector, *inp.args, *(inp.paths or ())]
        result = await self.sandbox.run(cmd, cwd=cwd)

        report = self._report(cwd)
        # A red run is always decided: a nonzero exit is a verdict, and marking it undecided would
        # let a fan-out barrier read a real failure as "no evidence". "Nothing was decided" is
        # reserved for the one case it is true — a JUnit report that ran zero tests on a clean
        # exit, the generic equivalent of pytest's exit 5. Without a report there is only the exit
        # code, which cannot tell "ran nothing" from "ran and passed", so the run is taken at its
        # word.
        red = result.exit_code != 0 or report.red
        ran_nothing = bool(self.junit) and report.total == 0 and result.exit_code == 0
        decided = not ran_nothing

        satisfied = (not red) if inp.expect == "pass" else red
        status = Status.SUCCEEDED if satisfied else Status.FAILED
        findings: tuple[Finding, ...] = ()
        if not satisfied:
            failed = report.failed or (0 if result.exit_code == 0 else 1)
            findings = (
                Finding(
                    id="test.expectation_unmet",
                    message=f"expected the suite to {inp.expect}; command exited {result.exit_code}"
                    + (f", {failed} failed" if report.total else ""),
                    severity=Severity.ERROR,
                    blocking=True,
                ),
            )
        elif ran_nothing:
            findings = (
                Finding(
                    id="test.no_tests_collected",
                    message="the runner reported no tests; nothing was decided",
                    severity=Severity.NOTE,
                ),
            )

        return Outcome(
            status=status,
            value=report,
            findings=findings,
            decided=decided,
            cost=Cost(wall_seconds=report.duration_seconds),
        )

    def _report(self, cwd: str | None) -> TestReport:
        if not self.junit:
            return TestReport()
        path = Path(cwd or ".") / self.junit
        try:
            xml = path.read_text()
        except OSError:
            return TestReport()
        return parse_junit(xml)


class CommandValidate:
    """`Validate` over any linter. `command` is the argv, e.g. `("npx", "eslint", ".")`."""

    verb: ClassVar[Verb] = Verb.VALIDATE
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.READS_REPO})

    def __init__(
        self,
        command: list[str] | tuple[str, ...],
        *,
        cwd: str | None = None,
        sandbox: Sandbox | None = None,
    ) -> None:
        if not command:
            raise ValueError("CommandValidate needs a command to run")
        self.command = tuple(command)
        self.cwd = cwd
        self.sandbox = sandbox or Sandbox()

    async def invoke(self, ctx: object, inp: Validate) -> Outcome[ValidationReport]:
        cwd = self.cwd or getattr(getattr(ctx, "repo", None), "root", None)
        result = await self.sandbox.run([*self.command, *(inp.paths or ())], cwd=cwd)
        clean = result.exit_code == 0
        # A generic linter has no structured output this can rely on, so the whole run is one
        # finding rather than one per rule — honest about what it knows. A repository that wants
        # per-rule findings binds an adapter that parses its linter's format, as `RuffValidate` does.
        findings: tuple[Finding, ...] = ()
        if not clean:
            findings = (
                Finding(
                    id="validate.command_failed",
                    message=_exited(self.command, result.exit_code, _tail(result.stdout, result.stderr)),
                    severity=Severity.ERROR,
                    blocking=True,
                ),
            )
        return Outcome(
            status=Status.SUCCEEDED if clean else Status.FAILED,
            value=ValidationReport(),
            findings=findings,
            cost=Cost(),
        )


class CommandBuild:
    """`Build` over any build command. `command` is the argv prefix, e.g. `("make", "build")`.

    What a build produced is not something a generic command can know, so `BuildResult.artifacts`
    is empty rather than guessed from the tree, and `log` carries the tail of what the command
    printed. A repository whose build writes to a known place binds an adapter that names it.
    """

    verb: ClassVar[Verb] = Verb.BUILD
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.EXECUTES_CODE, Capability.READS_REPO}
    )

    def __init__(
        self,
        command: list[str] | tuple[str, ...],
        *,
        cwd: str | None = None,
        sandbox: Sandbox | None = None,
    ) -> None:
        if not command:
            raise ValueError("CommandBuild needs a command to run")
        self.command = tuple(command)
        self.cwd = cwd
        # Out of process, like CommandTest: a build runs repository-authored code (a setup.py, a
        # postinstall script), and dropping the ambient credentials is the point.
        self.sandbox = sandbox or Sandbox()

    async def invoke(self, ctx: object, inp: Build) -> Outcome[BuildResult]:
        cwd = self.cwd or getattr(getattr(ctx, "repo", None), "root", None)
        cmd = [*self.command, *([inp.target] if inp.target else []), *inp.args]
        result = await self.sandbox.run(cmd, cwd=cwd)
        ok = result.exit_code == 0
        log = _tail(result.stdout, result.stderr)
        findings: tuple[Finding, ...] = ()
        if not ok:
            findings = (
                Finding(
                    id="build.command_failed",
                    message=_exited(cmd, result.exit_code, log),
                    severity=Severity.ERROR,
                    blocking=True,
                ),
            )
        return Outcome(
            status=Status.SUCCEEDED if ok else Status.FAILED,
            value=BuildResult(artifacts=(), log=log),
            findings=findings,
            cost=Cost(),
        )


class CommandRun:
    """`Run` over any command. `command` is the argv prefix, e.g. `("make", "run")`.

    Run to completion: the exit code is the verdict and the output is the result. A server that
    never exits is not what this verb means, and one ends at the sandbox's timeout rather than
    holding the run open.
    """

    verb: ClassVar[Verb] = Verb.RUN
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.EXECUTES_CODE, Capability.READS_REPO}
    )

    def __init__(
        self,
        command: list[str] | tuple[str, ...],
        *,
        cwd: str | None = None,
        sandbox: Sandbox | None = None,
    ) -> None:
        if not command:
            raise ValueError("CommandRun needs a command to run")
        self.command = tuple(command)
        self.cwd = cwd
        self.sandbox = sandbox or Sandbox()

    async def invoke(self, ctx: object, inp: Run) -> Outcome[RunResult]:
        cwd = inp.cwd or self.cwd or getattr(getattr(ctx, "repo", None), "root", None)
        cmd = [*self.command, *inp.command]
        runner = self.sandbox
        if inp.env:
            # The sandbox allows a fixed set of variables through and adds its own; a request's
            # variables join those rather than replacing them, so the credential drop still holds,
            # and `Sandbox` carries them into a container as flags rather than leaving them on the
            # runtime's client, where they would reach nothing. A runner that cannot carry
            # variables gets a refusal rather than the command run without them: that would be a
            # different command from the one the workflow asked for.
            if not isinstance(runner, Sandbox):
                return Outcome(
                    status=Status.FAILED,
                    value=None,
                    findings=(
                        Finding(
                            id="run.env_unsupported",
                            message=f"{type(runner).__name__} cannot carry environment variables; "
                            f"bind CommandRun with a Sandbox, or drop `env` from the request",
                            severity=Severity.ERROR,
                            blocking=True,
                        ),
                    ),
                    cost=Cost(),
                )
            runner = replace(runner, extra_env={**runner.extra_env, **dict(inp.env)})
        result = await runner.run(cmd, cwd=cwd)
        ok = result.exit_code == 0
        findings: tuple[Finding, ...] = ()
        if not ok:
            findings = (
                Finding(
                    id="run.command_failed",
                    message=_exited(cmd, result.exit_code, _tail(result.stdout, result.stderr)),
                    severity=Severity.ERROR,
                    blocking=True,
                ),
            )
        return Outcome(
            status=Status.SUCCEEDED if ok else Status.FAILED,
            value=RunResult(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr),
            findings=findings,
            cost=Cost(),
        )


def parse_junit(xml_text: str) -> TestReport:
    """Read a JUnit XML report into a `TestReport`. Tolerant: a malformed report is empty, not a
    crash, because a test command's own failure must not become the framework's."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return TestReport()

    cases: list[TestCase] = []
    passed = failed = skipped = 0
    duration = 0.0
    for tc in root.iter("testcase"):
        name = tc.get("name", "")
        classname = tc.get("classname", "")
        case_id = f"{classname}::{name}" if classname else name
        try:
            case_time = float(tc.get("time") or 0.0)
        except ValueError:
            case_time = 0.0
        duration += case_time
        child_tags = {child.tag for child in tc}
        # `<error>` (the test could not run — a fixture blew up, an import failed) is distinct from
        # `<failure>` (an assertion): `TestReport` counts both as failed, but the per-case outcome
        # keeps the "error" label the type documents, so a fix loop can tell "the test is wrong"
        # from "the code under test is wrong".
        if "error" in child_tags:
            outcome, message = "error", _first_message(tc, ("error",))
            failed += 1
        elif "failure" in child_tags:
            outcome, message = "failed", _first_message(tc, ("failure",))
            failed += 1
        elif "skipped" in child_tags:
            outcome, message = "skipped", ""
            skipped += 1
        else:
            outcome, message = "passed", ""
            passed += 1
        cases.append(TestCase(id=case_id, outcome=outcome, duration_seconds=case_time, message=message))

    return TestReport(
        total=passed + failed + skipped,
        passed=passed,
        failed=failed,
        skipped=skipped,
        cases=tuple(cases),
        duration_seconds=round(duration, 3),
    )


def _first_message(testcase: object, tags: tuple[str, ...]) -> str:
    for child in testcase:  # type: ignore[attr-defined]
        if child.tag in tags:
            return str(child.get("message") or (child.text or "").strip())[:500]
    return ""
