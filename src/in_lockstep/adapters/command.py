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

Provision is the same shape with one difference that matters: it is the step that builds the
environment every other adapter's tool comes from, so it runs first, and it is the one
deterministic adapter whose job is to reach a registry.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar

from ..core.outcome import Cost, Finding, Outcome, Severity, Status
from ..core.types import (
    Build,
    BuildResult,
    Provision,
    ProvisionResult,
    Resolution,
    Run,
    RunResult,
    Test,
    TestCase,
    TestReport,
    Validate,
    ValidationReport,
)
from ..core.verbs import Capability, Verb
from . import tooling
from .sandbox import Sandbox

__all__ = [
    "Build",
    "CommandBuild",
    "CommandProvision",
    "CommandRun",
    "CommandTest",
    "CommandValidate",
    "Provision",
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


def _locations(
    command: tuple[str, ...], cwd: str | None, root: str, sandbox: object
) -> tuple[Resolution, ...]:
    """Where the command's own binary (`npm`, `make`, `mypy`) comes from, for `ls` and `doctor`.

    No probe: a repository script or a build tool may not answer `--version` at all, and a
    `doctor` that runs an arbitrary command to see is not a diagnostic.
    """
    return (tooling.binary(command[0], cwd or root, sandbox),)


def _argv0(command: tuple[str, ...], cwd: str | None, ctx: object, sandbox: object) -> tuple[str, Resolution]:
    """What actually runs as the command's first word, and where it came from.

    The repository's own `.venv` is on nobody's PATH unless it is activated, so a tool found there
    is substituted by its path; a tool found on PATH keeps its bare name, since the sandbox passes
    PATH through and would find the same one. What `ls` prints is then what the run execs, and a
    `mypy` that lives only in the repository's environment runs from an installed copy.
    """
    root = cwd or getattr(getattr(ctx, "repo", None), "root", None)
    resolved = tooling.binary(command[0], root, sandbox)
    if resolved.path is not None and resolved.how == tooling.REPOSITORY_VENV:
        return resolved.path, resolved
    return command[0], resolved


def _could_not_run(argv0: str, resolved: Resolution) -> Outcome[Any]:
    """Exit 127 is the shell's "no such command". It is an environment fact, not a verdict on the
    change, and it names every place the tool was looked for."""
    looked = f"; looked for {', '.join(resolved.tried)}" if resolved.tried else ""
    return Outcome.errored(f"{argv0} could not be run{looked}")


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

    def locations(self, root: str) -> tuple[Resolution, ...]:
        return _locations(self.command, self.cwd, root, self.sandbox)

    async def invoke(self, ctx: object, inp: Test) -> Outcome[TestReport]:
        # A per-call `root` (a materialized worktree) wins over the bound `cwd` wins over the
        # repo's root: the spec is how a workflow points one bound adapter at a staged change
        # without rebinding it.
        cwd = inp.root or self.cwd or getattr(getattr(ctx, "repo", None), "root", None)
        selector = [*self.selector_arg, inp.selector] if (inp.selector and self.selector_arg) else []
        argv0, resolved = _argv0(self.command, self.cwd, ctx, self.sandbox)
        cmd = [argv0, *self.command[1:], *selector, *inp.args, *(inp.paths or ())]
        result = await self.sandbox.run(cmd, cwd=cwd)
        if result.exit_code == 127:
            return _could_not_run(argv0, resolved)

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

    def locations(self, root: str) -> tuple[Resolution, ...]:
        return _locations(self.command, self.cwd, root, self.sandbox)

    async def invoke(self, ctx: object, inp: Validate) -> Outcome[ValidationReport]:
        cwd = self.cwd or getattr(getattr(ctx, "repo", None), "root", None)
        argv0, resolved = _argv0(self.command, self.cwd, ctx, self.sandbox)
        result = await self.sandbox.run([argv0, *self.command[1:], *(inp.paths or ())], cwd=cwd)
        if result.exit_code == 127:
            return _could_not_run(argv0, resolved)
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

    def locations(self, root: str) -> tuple[Resolution, ...]:
        return _locations(self.command, self.cwd, root, self.sandbox)

    async def invoke(self, ctx: object, inp: Build) -> Outcome[BuildResult]:
        cwd = self.cwd or getattr(getattr(ctx, "repo", None), "root", None)
        argv0, resolved = _argv0(self.command, self.cwd, ctx, self.sandbox)
        cmd = [argv0, *self.command[1:], *([inp.target] if inp.target else []), *inp.args]
        result = await self.sandbox.run(cmd, cwd=cwd)
        if result.exit_code == 127:
            return _could_not_run(argv0, resolved)
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

    def locations(self, root: str) -> tuple[Resolution, ...]:
        return _locations(self.command, self.cwd, root, self.sandbox)

    async def invoke(self, ctx: object, inp: Run) -> Outcome[RunResult]:
        cwd = inp.cwd or self.cwd or getattr(getattr(ctx, "repo", None), "root", None)
        argv0, resolved = _argv0(self.command, self.cwd, ctx, self.sandbox)
        cmd = [argv0, *self.command[1:], *inp.command]
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
        if result.exit_code == 127:
            return _could_not_run(argv0, resolved)
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


class CommandProvision:
    """`Provision` over the steps that build the repository's own environment, in order.

    `steps` is a sequence of argvs (`[["uv", "sync", "--locked"], ["npm", "ci"]]`) because a
    repository can have more than one lockfile, and each is its own install. They run in order
    and stop at the first that fails: an install that half-happened is not an environment, and
    the step that failed is the one to name.

    The one deterministic adapter whose job is to reach a registry, so its default sandbox allows
    the network. It still drops the ambient credentials: a lockfile's install hooks (a `setup.py`,
    a `postinstall` script) are repository-authored code, and the provider key must not be in
    their environment. A `Sandbox` that names an image and denies the network is refused as
    `blocked` rather than run: with a runtime present the install would fail in a way that reads
    as the registry's, and without one `Sandbox` would run the steps on the host, with the network
    it has, which is the binding quietly doing less than it said. Either way the fix is to write
    `Sandbox(image=..., allow_network=True)` deliberately, or `Sandbox()` to provision on the host.
    An environment provisioned inside an image lands in the bind mount and serves the adapters
    that run on the host; a containerized suite resolves its tools inside the image (`tooling`
    returns the bare name there), so an image is expected to carry its own.
    """

    verb: ClassVar[Verb] = Verb.PROVISION
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {
            Capability.EXECUTES_CODE,
            Capability.READS_REPO,
            Capability.WRITES_FILES,
            Capability.REACHES_NETWORK,
        }
    )

    def __init__(
        self,
        steps: Sequence[Sequence[str]],
        *,
        cwd: str | None = None,
        sandbox: Sandbox | None = None,
    ) -> None:
        self.steps = tuple(tuple(step) for step in steps)
        if not self.steps or any(not step for step in self.steps):
            raise ValueError("CommandProvision needs at least one step, and no empty one")
        self.cwd = cwd
        self.sandbox = sandbox or Sandbox(allow_network=True)

    def locations(self, root: str) -> tuple[Resolution, ...]:
        """One line per distinct tool the steps run, for `ls` and `doctor`."""
        seen: list[str] = []
        out: list[Resolution] = []
        for step in self.steps:
            if step[0] in seen:
                continue
            seen.append(step[0])
            out.append(self._resolve(step[0], self.cwd or root)[1])
        return tuple(out)

    def _resolve(self, name: str, root: str | None) -> tuple[str, Resolution]:
        """What runs as a step's first word, and where it came from.

        `python` resolves the way the suite's interpreter does (the venv, then this process when
        it lives inside the repository, then PATH's `python` or `python3`), minus the
        `import pytest` probe: the interpreter that creates a venv need not have pytest in it. It
        runs by the path found in every case, because `python3` on PATH is not `python`, and the
        bare name would be a different command from the one `ls` showed. Everything else follows
        `_argv0`: the venv's copy by path, a PATH tool by its bare name.
        """
        if name == "python":
            found = replace(tooling.interpreter(root, self.sandbox), probe=())
            return (found.path or name), found
        resolved = tooling.binary(name, root, self.sandbox)
        if resolved.path is not None and resolved.how == tooling.REPOSITORY_VENV:
            return resolved.path, resolved
        return name, resolved

    async def invoke(self, ctx: object, inp: Provision) -> Outcome[ProvisionResult]:
        cwd = inp.root or self.cwd or getattr(getattr(ctx, "repo", None), "root", None)
        # `isinstance`, not attribute reads: `UnsandboxedRun` and a test's fake runner have only
        # `run`, and the sibling adapters accept them (`CommandRun` refuses one by name).
        sandbox = self.sandbox
        if isinstance(sandbox, Sandbox) and sandbox.image and not sandbox.allow_network:
            return Outcome.blocked_by(
                "provisioning reaches a registry and the bound sandbox names an image and denies the "
                "network; bind CommandProvision with Sandbox(image=..., allow_network=True) to allow it "
                "deliberately, or with Sandbox() to provision on the host"
            )
        ran: list[str] = []
        log = ""
        for step in self.steps:
            # Resolved per step rather than once up front, because the first step may be what
            # creates the venv the second one runs from.
            argv0, resolved = self._resolve(step[0], cwd)
            cmd = [argv0, *step[1:]]
            result = await self.sandbox.run(cmd, cwd=cwd)
            if getattr(result, "how", "") == "refused:no-container":
                # A sandbox told to require a container found none: the control working, not
                # the install failing.
                return Outcome.blocked_by(result.stderr.strip() or "refused to run outside a container")
            if result.exit_code == 127 and resolved.path is None:
                # 127 is "no such command" only when nothing was found. Provisioning is where a
                # lockfile's hooks run, and a `preinstall` that names a missing command exits 127
                # through npm: that is the install failing, with its tail, not npm absent.
                return _could_not_run(argv0, resolved)
            log = _tail(result.stdout, result.stderr)
            ran.append(" ".join(cmd))
            if result.exit_code != 0:
                return Outcome(
                    status=Status.FAILED,
                    value=ProvisionResult(steps=tuple(ran), log=log),
                    findings=(
                        Finding(
                            id="provision.command_failed",
                            message=_exited(cmd, result.exit_code, log),
                            severity=Severity.ERROR,
                            blocking=True,
                        ),
                    ),
                    cost=Cost(),
                )
        return Outcome(
            status=Status.SUCCEEDED,
            value=ProvisionResult(steps=tuple(ran), log=log),
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
