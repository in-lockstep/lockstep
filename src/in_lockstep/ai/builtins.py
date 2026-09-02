"""Built-in tools, and the workspace a model's writes accumulate into.

`ToolSet` has always been the dispatch table — a name it does not contain cannot be called,
because there is nothing to dispatch it to. What it lacked was anything to dispatch *to*: no
`ToolRunner` shipped, so `AiInvoker` accepted `tools` and `run_tool` and every shipped verb passed
neither. That left GATE-GUARD-1's first enforcement point, the in-loop tool boundary, as a place
rather than a thing.

Two decisions shape what is here.

**A write does not touch the disk.** `write_file` records a `FileChange` in a `Workspace`; the
resulting `ChangeSet` is applied afterwards, by `apply --from-artifact` in the two-job trampoline
or `--apply-inline` locally. So the model's writes cross the guard twice on the privileged path
and are reviewable as a unit either way — and a loop that ends `BLOCKED` halfway through leaves no
half-written tree behind, which is the property that makes an interrupted agent recoverable rather
than a mess.

**A refusal is a tool result, not an exception.** The model asked for something it may not have;
telling it so within the turn lets it choose differently, where raising ends the run and spends
the turns already paid for. The guard's answer is information, and the loop is the place to use
it.

**Executing anything is somebody else's decision.** `run_script` dispatches through an injected
`CommandRunner` and refuses when none is configured, so the dangerous half of this module is
inert until a caller deliberately supplies the thing that runs commands. The runner this repository
ships is `adapters.sandbox.Sandbox`, which drops every credential from the child environment — but
`ai` may not import `adapters`, and that constraint is the useful one here rather than an
inconvenience: it forces the seam to be a protocol, which is what lets a repository substitute a
stricter runner without touching the tool.
"""

from __future__ import annotations

import fnmatch
import inspect
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..core.changes import ChangeGuard
from ..core.types import ChangeAuthor, ChangeSet, FileChange
from ..core.verbs import Capability
from .tools import BUILTIN_SERVER, Tool, ToolSet

# A read that returns a whole vendored tree is a prompt nobody budgeted for.
MAX_READ_CHARS = 40_000
MAX_LISTED = 200
MAX_SEARCH_MATCHES = 80
# Command output is model input next turn, and a test suite prints a great deal of it. Kept well
# under the invoker's own tool-result cap so the tail — where a traceback lives — survives.
MAX_SCRIPT_OUTPUT_CHARS = 6_000
DEFAULT_SCRIPT_TIMEOUT = 300.0

#: How many times one session may run the suite by default. Three, because the loop this exists to
#: close is write → run → correct, and a model that has not converged in three full runs is not
#: going to on the fourth — it is going to spend the rest of its turns re-reading the same failures.
#: Each run costs real wall-clock (this repository's own suite takes about a hundred seconds), and
#: that time comes out of the same ceiling the model needs to finish.
DEFAULT_TEST_RUNS = 3

# What a model may run, by argv[0]. An allowlist rather than a denylist, because the interesting
# property is "somebody wrote this down", and a denylist of shells is trivially defeated by the
# next interpreter nobody thought of.
#
# Everything here executes repository-authored code — a Makefile, a conftest, a test — which is
# precisely why the runner drops credentials rather than why the list is short. The list is short
# because a model that needs `curl` is a model doing something the tool set did not grant it.
ALLOWED_COMMANDS: tuple[str, ...] = (
    "python",
    "python3",
    "pytest",
    "ruff",
    "mypy",
    "uv",
    "make",
    "npm",
    "npx",
    "node",
    "go",
    "cargo",
)


@runtime_checkable
@runtime_checkable
class TestRunner(Protocol):
    """Runs the repository's suite over HEAD plus what this session has staged.

    Structural and injected for the same reason `CommandRunner` is: materialising a change set
    lives in `adapters/worktree.py`, and `ai` may not import `adapters`. Naming a protocol is also
    the better answer on its own — what a repository substitutes here decides whether a model's
    "the tests pass" is a measurement or a claim.

    `paths` narrows the run. It matters more than it looks: a full suite here is about a hundred
    seconds, so an unnarrowed loop spends its budget waiting rather than working. It is also the
    one dangerous affordance in this tool — a model can run a subset that passes and talk itself
    into finishing — which is why the framework's own full run still decides the outcome, and why
    the result of this tool is never what a strategy reports.
    """

    async def __call__(self, paths: tuple[str, ...] = ()) -> str: ...


class CommandRunner(Protocol):
    """Whatever actually executes a command. Structural on purpose.

    `ai` may not import `adapters`, so this cannot name `Sandbox` — and naming a protocol instead
    is the better answer anyway. What a repository substitutes here is the entire difference
    between "the model ran the test suite in a container with no network" and "the model ran
    something on the machine holding your credentials", and that decision belongs at the binding
    site rather than inside the tool.

    The result needs `exit_code`, `stdout`, `stderr` and `how`; `adapters.sandbox.SandboxResult`
    is the shape this was written against.
    """

    async def run(self, command: list[str], *, cwd: str | None = None, timeout: float = 900.0) -> Any: ...


@dataclass
class Workspace:
    """Where a model's writes go instead of the filesystem.

    Holds the guard rather than consulting a global one, so a repository binding a stricter
    `PathPolicy` gets it here too — the tool boundary and `apply` must not be able to disagree
    about what is protected.
    """

    root: Path = field(default_factory=Path.cwd)
    guard: ChangeGuard = field(default_factory=ChangeGuard)
    workflow_id: str = ""
    changes: list[FileChange] = field(default_factory=list)

    def changeset(self, *, summary: str = "", ticket: str = "") -> ChangeSet:
        return ChangeSet(changes=tuple(self.changes), summary=summary, ticket=ticket)

    def resolve(self, path: str) -> Path:
        return self.root / posixpath.normpath(path.replace("\\", "/"))

    def record(self, path: str, contents: str | None) -> str:
        """Stage a write, or say why not. The return value is what the model sees."""
        change = FileChange(path=path, contents=contents, author=ChangeAuthor.AGENT)
        refusal = self.guard.check_change(change, workflow_id=self.workflow_id)
        if refusal is not None:
            return (
                f"refused: {refusal.path} is protected (tier {refusal.tier}, rule "
                f"{refusal.rule}). This path is not writable by an agent under any grant this "
                f"run holds. Change something else, or say why it cannot be done."
            )
        # Last write wins for one path, so a model correcting itself does not stage two versions
        # and leave `apply` to guess which was meant.
        self.changes = [c for c in self.changes if c.path != path]
        self.changes.append(change)
        verb = "staged deletion of" if contents is None else "staged write to"
        return f"ok: {verb} {path} ({len(self.changes)} change(s) pending)"


def read_only(workspace: Workspace) -> tuple[ToolSet, ToolRunnerImpl]:
    """What a reviewer needs: look at the tree it was asked about, and nothing else."""
    tools = ToolSet.of(
        Tool(
            server=BUILTIN_SERVER,
            name="read_file",
            description="Read a UTF-8 text file from the repository.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            capabilities=frozenset({Capability.READS_REPO}),
        ),
        Tool(
            server=BUILTIN_SERVER,
            name="list_files",
            description="List repository paths matching a glob.",
            parameters={
                "type": "object",
                "properties": {"glob": {"type": "string"}},
                "required": ["glob"],
            },
            capabilities=frozenset({Capability.READS_REPO}),
        ),
        # Exploring a repository by reading whole files is the expensive way to do it: each read
        # is up to MAX_READ_CHARS of prompt that is re-sent on every subsequent turn, so a model
        # that has to open six files to find one function has paid for six files forever. Search
        # is what makes a turn ceiling generous enough to explore under.
        Tool(
            server=BUILTIN_SERVER,
            name="search_text",
            description=(
                "Search repository files for a regular expression. Returns path:line matches. "
                "Cheaper than reading files: prefer this to locate code, then read what it found."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "glob": {"type": "string", "description": "Restrict to matching paths."},
                },
                "required": ["pattern"],
            },
            capabilities=frozenset({Capability.READS_REPO}),
        ),
    )
    return tools, ToolRunnerImpl(workspace)


def read_write(workspace: Workspace) -> tuple[ToolSet, ToolRunnerImpl]:
    """Adds staging a change. Declares WRITES_FILES, which is what makes policy see it.

    That declaration is load-bearing in three places at once: egress enforcement becomes
    mandatory, `ApprovalGate` gates the action, and `Retry` refuses to re-invoke it.
    """
    tools, runner = read_only(workspace)
    tools = tools | ToolSet.of(
        Tool(
            server=BUILTIN_SERVER,
            name="write_file",
            description="Stage a write. Applied after the run, not immediately.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}, "contents": {"type": "string"}},
                "required": ["path", "contents"],
            },
            capabilities=frozenset({Capability.WRITES_FILES}),
        ),
        Tool(
            server=BUILTIN_SERVER,
            name="delete_file",
            description="Stage a deletion. Applied after the run, not immediately.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            capabilities=frozenset({Capability.WRITES_FILES}),
        ),
    )
    return tools, runner


def read_write_execute(
    workspace: Workspace,
    *,
    commands: CommandRunner | None = None,
    tests: TestRunner | None = None,
    allowed_commands: tuple[str, ...] = ALLOWED_COMMANDS,
    script_timeout: float = DEFAULT_SCRIPT_TIMEOUT,
    max_test_runs: int = DEFAULT_TEST_RUNS,
) -> tuple[ToolSet, ToolRunnerImpl]:
    """Read, stage, and run a command. The most capable set the framework ships.

    `EXECUTES_CODE` here is not a label — it is the declaration three separate controls key on,
    and declaring it is the point of this function existing rather than a `run_script=True`
    parameter on `read_write`. It makes egress enforcement mandatory before the first model call,
    it makes `ApprovalGate` a startup requirement for any adapter that also spends money, and it
    makes `Retry` refuse to re-invoke the action.

    Passing no `commands` runner is allowed and yields a set that still declares the capability
    while every call refuses. That is deliberate: a tool set's declaration is what policy sees, so
    a set that could execute on some other configuration must not read as harmless on this one.
    """
    tools, runner = read_write(workspace)
    runner.commands = commands
    runner.tests = tests
    runner.allowed_commands = allowed_commands
    runner.script_timeout = script_timeout
    runner.max_test_runs = max_test_runs
    tools = tools | ToolSet.of(
        Tool(
            server=BUILTIN_SERVER,
            name="run_tests",
            description=(
                "Run this repository's test suite over the current code PLUS everything you have "
                "staged with write_file, and return what it said. This is the only way to find out "
                "whether your change works before the run ends — run_script sees the tree WITHOUT "
                f"your staged writes. Limited to {max_test_runs} call(s) per session, because the "
                "suite costs real time; narrow it with `paths` rather than spending a call on the "
                "whole suite. Passing a subset does not mean the change is done: the framework runs "
                "everything at the end, and that run is what decides."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            'test files or directories to run, e.g. ["tests/in_lockstep/test_x.py"].'
                            " Omit to run everything, which is slow."
                        ),
                    }
                },
            },
            capabilities=frozenset({Capability.EXECUTES_CODE}),
        ),
        Tool(
            server=BUILTIN_SERVER,
            name="run_script",
            description=(
                "Run a command as an argv list — no shell, so no pipes, globs or redirection. "
                f"Allowed programs: {', '.join(allowed_commands)}. Runs against the repository "
                "working tree, which does NOT contain this run's staged writes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": 'argv, e.g. ["pytest", "-q", "tests/test_thing.py"]',
                    },
                    "timeout_seconds": {"type": "number"},
                },
                "required": ["command"],
            },
            capabilities=frozenset({Capability.EXECUTES_CODE}),
        ),
    )
    return tools, runner


@dataclass
class ToolRunnerImpl:
    """Dispatches the builtin names. Anything else is not reachable from here."""

    workspace: Workspace
    #: Absent by default, which makes `run_script` refuse. Executing is opt-in at the binding site.
    commands: CommandRunner | None = None
    allowed_commands: tuple[str, ...] = ALLOWED_COMMANDS
    script_timeout: float = DEFAULT_SCRIPT_TIMEOUT
    #: Runs the bound Test verb over HEAD plus what this session has staged. Injected, and it has
    #: to be: materialising a changeset lives in `adapters`, which `ai` may not import. The same
    #: inversion `commands` uses — this layer declares the seam, the composition root fills it.
    #: Absent makes `run_tests` refuse, exactly as an absent runner makes `run_script` refuse.
    tests: TestRunner | None = None
    #: How many times one session may run the suite. A ceiling rather than a budget: the suite
    #: costs wall-clock, and a model that runs it after every edit exhausts its turns before it
    #: finishes. Set from `InvokePolicy.max_test_runs` at the binding site.
    max_test_runs: int = DEFAULT_TEST_RUNS
    _test_runs: int = 0

    async def __call__(self, server: str, name: str, args: dict[str, object]) -> str:
        if server != BUILTIN_SERVER:  # pragma: no cover - ToolSet resolves before this
            return f"refused: {server!r} is not a builtin server"
        handler = {
            "read_file": self._read,
            "list_files": self._list,
            "search_text": self._search,
            "write_file": self._write,
            "delete_file": self._delete,
            "run_script": self._script,
            "run_tests": self._tests,
        }.get(name)
        if handler is None:  # pragma: no cover - ToolSet resolves before this
            return f"refused: no builtin tool named {name!r}"
        result = handler(args)
        # One handler is async and the rest are not. Awaiting whatever comes back keeps that an
        # implementation detail of the handler rather than a fact every caller has to know.
        return await result if inspect.isawaitable(result) else result  # type: ignore[return-value]

    async def _tests(self, args: dict[str, object]) -> str:
        """Run the suite over what this session has staged, and say what happened.

        The whole point of the tool: without it a model writes an implementation, stops, and learns
        whether it worked only after the run is over and paid for. Run 33582850420 is what that
        costs — `tdd.not_green`, 13 failing tests of 1644, $13.84 for a diff one test run would have
        rejected.
        """
        if self.tests is None:
            return (
                "refused: no Test verb is bound, so there is nothing to run. Bind Test (e.g. "
                "PytestTest) in lockstep.py, or work without a suite."
            )
        if self._test_runs >= self.max_test_runs:
            # A refusal that says the number, because the alternative is a model retrying a call
            # that will never work and burning the turns it needs to finish.
            return (
                f"refused: the suite has already been run {self._test_runs} time(s), which is this "
                f"session's limit. Decide from what the last run said rather than running it again."
            )
        raw = args.get("paths") or ()
        paths = tuple(str(p) for p in raw if str(p).strip()) if isinstance(raw, (list, tuple)) else ()
        self._test_runs += 1
        try:
            return await self.tests(paths)
        except Exception as e:  # noqa: BLE001 - a tool result is a message, never a crash
            # A failing runner is something the model can react to. Raising here would end the
            # session with the staged work unreported, which is the loss this tool exists to avoid.
            return f"error: could not run the suite: {e}"

    def _read(self, args: dict[str, object]) -> str:
        path = str(args.get("path", ""))
        # The guard's own out-of-root rule, applied to reads. A model that can read `../../.ssh`
        # has exfiltrated it the moment the result enters the transcript.
        refusal = self.workspace.guard.check_path(path, workflow_id=self.workspace.workflow_id)
        if refusal is not None and refusal.rule == "outside-repo-root":
            return f"refused: {path} is outside the repository"
        target = self.workspace.resolve(path)
        if not target.is_file():
            return f"error: no file at {path}"
        try:
            text = target.read_text()
        except (OSError, UnicodeDecodeError) as e:
            return f"error: {e}"
        if len(text) > MAX_READ_CHARS:
            return text[:MAX_READ_CHARS] + "\n…[truncated]"
        return text

    def _list(self, args: dict[str, object]) -> str:
        pattern = str(args.get("glob", "*"))
        matches = sorted(
            str(p.relative_to(self.workspace.root))
            for p in self.workspace.root.rglob("*")
            if p.is_file() and fnmatch.fnmatch(str(p.relative_to(self.workspace.root)), pattern)
        )
        if not matches:
            return "(no matches)"
        listed = matches[:MAX_LISTED]
        suffix = f"\n…[{len(matches) - len(listed)} more]" if len(matches) > len(listed) else ""
        return "\n".join(listed) + suffix

    def _search(self, args: dict[str, object]) -> str:
        pattern = str(args.get("pattern", ""))
        if not pattern:
            return "error: search_text needs a pattern"
        try:
            expression = re.compile(pattern)
        except re.error as e:
            # The model wrote the regex, so a bad one is its mistake to correct — not a crash.
            return f"error: {pattern!r} is not a valid regular expression: {e}"
        glob = str(args.get("glob", "") or "*")

        matches: list[str] = []
        truncated = False
        for path in sorted(self.workspace.root.rglob("*")):
            if not path.is_file():
                continue
            rel = str(path.relative_to(self.workspace.root))
            if not fnmatch.fnmatch(rel, glob):
                continue
            try:
                text = path.read_text()
            except (OSError, UnicodeDecodeError):
                # A binary file is not a search failure, and reporting one per binary would bury
                # the answer under noise about the things that were never candidates.
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if expression.search(line):
                    if len(matches) >= MAX_SEARCH_MATCHES:
                        truncated = True
                        break
                    matches.append(f"{rel}:{number}: {line.strip()[:200]}")
            if truncated:
                break
        if not matches:
            return "(no matches)"
        suffix = f"\n…[stopped at {MAX_SEARCH_MATCHES} matches; narrow the pattern]" if truncated else ""
        return "\n".join(matches) + suffix

    def _write(self, args: dict[str, object]) -> str:
        return self.workspace.record(str(args.get("path", "")), str(args.get("contents", "")))

    def _delete(self, args: dict[str, object]) -> str:
        return self.workspace.record(str(args.get("path", "")), None)

    async def _script(self, args: dict[str, object]) -> str:
        """Run a command, or say precisely why not.

        Four refusals, and each is a separate thing that could go wrong: no runner configured, a
        malformed argv, a program nobody allowed, and a path that would leave the repository. They
        are separate messages because a model that cannot tell them apart cannot correct any of
        them — "refused" alone reads as "stop trying", where "python is not in the allowlist"
        reads as "use pytest".
        """
        if self.commands is None:
            return (
                "refused: no command runner is configured for this run, so nothing can be "
                "executed. Reason about the code from what you can read instead."
            )

        raw = args.get("command")
        if isinstance(raw, str):
            # Deliberately not split into argv. A string is what a model reaches for when it wants
            # a shell, and quietly turning `a && b` into a token list would run something other
            # than what was asked, which is the worst of the available answers.
            return (
                "refused: `command` must be an argv array, not a string — there is no shell here, "
                'so pipes, globs and `&&` do not work. Use e.g. ["pytest", "-q"].'
            )
        if not isinstance(raw, list) or not raw or not all(isinstance(a, str) for a in raw):
            return 'refused: `command` must be a non-empty array of strings, e.g. ["pytest", "-q"]'

        argv = [str(a) for a in raw]
        program = posixpath.basename(argv[0])
        if program not in self.allowed_commands:
            return (
                f"refused: {program!r} is not an allowed program. Allowed: "
                f"{', '.join(self.allowed_commands)}."
            )

        requested = args.get("timeout_seconds")
        timeout = (
            float(requested) if isinstance(requested, (int, float)) and requested else self.script_timeout
        )
        result = await self.commands.run(argv, cwd=str(self.workspace.root), timeout=timeout)
        exit_code = getattr(result, "exit_code", 1)
        how = getattr(result, "how", "unknown")
        # The tail, not the head. A test run's useful part is the failure summary at the end, and
        # truncating from the front is how a model ends up reasoning about the collection banner.
        body = _tail(str(getattr(result, "stdout", "")), str(getattr(result, "stderr", "")))
        return f"exit {exit_code} ({how})\n{body}"


def _tail(stdout: str, stderr: str) -> str:
    """Both streams, capped from the end, each labelled."""
    parts = []
    for label, text in (("stdout", stdout), ("stderr", stderr)):
        if not text.strip():
            continue
        clipped = text
        if len(clipped) > MAX_SCRIPT_OUTPUT_CHARS:
            clipped = "…[earlier output dropped]\n" + clipped[-MAX_SCRIPT_OUTPUT_CHARS:]
        parts.append(f"--- {label} ---\n{clipped.rstrip()}")
    return "\n".join(parts) if parts else "(no output)"


__all__ = [
    "ALLOWED_COMMANDS",
    "MAX_READ_CHARS",
    "CommandRunner",
    "ToolRunnerImpl",
    "Workspace",
    "read_only",
    "read_write",
    "read_write_execute",
]
