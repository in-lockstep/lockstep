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
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..core.changes import ChangeGuard
from ..core.types import ChangeAuthor, ChangeSet, FileChange
from ..core.verbs import Capability
from ..privileged.redact import MASK, Redact
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

#: The one builtin the tool runner never sees. `AiInvoker` handles it itself, because what it does
#: is start another turn-loop -- and only the invoker holds the provider, the spend, the transcript
#: and the policy a nested loop has to share (#332).
DELEGATE_TOOL = "delegate"
SEARCH_TOOL = "search_code"
SEARCH_MODES = ("ask", "grep", "callers", "skeleton", "map")
#: Under `max_tool_result_chars`, so a search answer is never the thing truncation cuts.
MAX_SEARCH_CHARS = 12_000
DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 40
MAX_SEARCH_DEPTH = 3

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


class ValidateRunner(Protocol):
    """Runs the bound Validate verb over HEAD plus what this session has staged.

    The seam `run_tests` has, for the verb beside it. A session's writes are staged rather than on
    disk, so `run_script` -- which runs in a throwaway worktree of HEAD on purpose -- lints the
    code as it already was and reports it clean, which is worse than reporting nothing. Injected
    for the same reason `tests` is: materialising a change set lives in `adapters`, which `ai` may
    not import.
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


@dataclass(frozen=True)
class Hit:
    """One line of a code-search answer, with the path it names so the guard can judge it."""

    path: str
    line: str


@dataclass(frozen=True)
class SearchAnswer:
    """What a code-search backend says: hits, or a refusal by name, or that nothing matched."""

    hits: tuple[Hit, ...] = ()
    refusal: str | None = None
    empty: str = "(no matches)"


class CodeSearch(Protocol):
    """Whatever answers `search_code`. Structural, for the reason `CommandRunner` is: the shipped
    backend is Graft in the framework's cache (`adapters.graft`), which `ai` may not import, and
    the seam is what lets a test hand in an answer without a Node on the machine.

    `staged` is the session's own writes, so the backend can answer over the tree the session
    sees rather than the disk (GATE-WORKSPACE-1); `args` is what the model asked, already
    bounded by the tool.
    """

    async def __call__(
        self, mode: str, args: dict[str, object], staged: tuple[tuple[str, str | None], ...]
    ) -> SearchAnswer: ...


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
    #: The same redaction the tool results went through on their way to the model, so a write
    #: can put back what a read took out (see `record`).
    redact: Redact = field(default_factory=Redact)

    def changeset(self, *, summary: str = "", ticket: str = "", notes: tuple[str, ...] = ()) -> ChangeSet:
        return ChangeSet(changes=tuple(self.changes), summary=summary, ticket=ticket, notes=notes)

    def restage(self, path: str, contents: str) -> bool:
        """Replace the contents of a file this session already staged. Says whether it changed one.

        For a deterministic repair: the repository's own validator, run over the staged tree with
        its fixer on, rewrites files the model wrote, and those bytes have to become what the
        session holds -- or a later turn reads the unfixed version and the fix is lost when the
        change set is rebuilt.

        **Never adds a path.** A fixer that created a file did not create one this session is
        proposing, and the change set is the model's answer to the ticket rather than everything
        that happened to appear in a worktree.

        **The author stays what it was.** `ChangeAuthor.FRAMEWORK` exempts an entry from the guard,
        so relabelling a model's file to record who reformatted it would trade a control for a
        label. Who ran the fixer is recorded as a finding on the outcome instead, where a reader
        gets it and the guard is unaffected -- and the guard is re-run over the entry here, because
        the bytes are new even though the path is not.
        """
        for index, change in enumerate(self.changes):
            if change.path != path or change.deleted or change.contents == contents:
                continue
            fixed = replace(change, contents=contents)
            if self.guard.check_change(fixed, workflow_id=self.workflow_id) is not None:
                return False
            self.changes[index] = fixed
            return True
        return False

    def resolve(self, path: str) -> Path:
        return self.root / posixpath.normpath(path.replace("\\", "/"))

    def inside(self, target: Path) -> bool:
        """Whether `target`, followed through every symlink, still lies under the root.

        `check_read` is string-based on purpose -- it judges the path the model NAMED -- and a
        symlink is the case where the name and the file disagree: `link -> ../../.ssh/id_ed25519`
        is inside the root by name and outside it on disk. The write side has refused exactly
        this since GATE-GUARD-2; the read side followed the link (#308). Resolved here, once, for
        the three tools that open files, and compared as paths rather than prefixes so that a
        sibling directory sharing the root's name as a prefix is not mistaken for inside.
        """
        try:
            resolved = target.resolve()
            root = self.root.resolve()
        except OSError:
            return False
        return resolved == root or root in resolved.parents

    def record(self, path: str, contents: str | None) -> str:
        """Stage a write, or say why not. The return value is what the model sees."""
        restored = 0
        if contents is not None and MASK in contents:
            # The model reads through a redacting sink, so a key-shaped string in a file reached
            # it as `***` -- and a model that edits one line and writes the file back writes the
            # mask over the value, which is how this repository's first `/fix` on itself turned a
            # test's fake credential into `***` and failed the suite it had otherwise fixed (run
            # 34129809277, #312). The mask is what the model was shown in place of a value it was
            # never allowed to see, so a write is the one place the value can go back: every mask
            # is matched to the value the file holds at that position, and the file's own
            # redaction is what says where those are. A count that does not match is refused,
            # because then a mask is one the model typed, and nothing here guesses which.
            restore = _restore_masked(self.resolve(path), contents, self.redact)
            if isinstance(restore, str):
                return restore
            contents, restored = restore
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
        note = (
            f"; {restored} value(s) shown to you as {MASK} were put back from the file, unchanged"
            if restored
            else ""
        )
        return f"ok: {verb} {path} ({len(self.changes)} change(s) pending{note})"


def _restore_masked(target: Path, contents: str, redact: Redact) -> tuple[str, int] | str:
    """`contents` with each `***` replaced by the value the file holds there, and how many; or
    the refusal to hand the model.

    The file's own redaction says where its masked values are: splitting the redacted file on the
    mask gives the text between them, and walking the real file along those pieces recovers each
    value. The model's contents split on the mask the same way; the same number of pieces means
    the masks are the ones it was shown, in order, and the values go back between them.
    """
    try:
        original = target.read_text() if target.is_file() else ""
    except (OSError, UnicodeDecodeError):
        original = ""
    masked = redact.text(original) if original else ""
    pieces = masked.split(MASK)
    if len(pieces) == 1:
        return (
            f"refused: the contents carry {MASK}, which is the mask shown in place of a value you "
            f"may not see, and {target.name} holds no such value; writing the mask would put it in "
            f"the file as text. Write what you mean there, or leave the file alone."
        )
    values: list[str] = []
    position = 0
    for index, piece in enumerate(pieces[:-1]):
        if not original.startswith(piece, position):
            return f"refused: {target.name} changed while you were reading it; read it again."
        position += len(piece)
        following = pieces[index + 1]
        end = original.find(following, position) if following else len(original)
        if end < 0:
            return f"refused: {target.name} changed while you were reading it; read it again."
        values.append(original[position:end])
        position = end
    parts = contents.split(MASK)
    if len(parts) != len(values) + 1:
        return (
            f"refused: {target.name} holds {len(values)} value(s) shown to you as {MASK} and your "
            f"contents carry {len(parts) - 1} mask(s). Keep every masked value exactly where it "
            f"was, as {MASK}, and it is put back for you; do not write the mask anywhere else."
        )
    rebuilt = parts[0] + "".join(value + part for value, part in zip(values, parts[1:], strict=True))
    return rebuilt, len(values)


def read_only(
    workspace: Workspace, *, code_search: CodeSearch | None = None
) -> tuple[ToolSet, ToolRunnerImpl]:
    """What a reviewer needs: look at the tree it was asked about, and nothing else.

    `code_search` adds `search_code`, a symbol-level search over an index the framework built
    (#375). Handed in rather than always on, and not only because the backend lives in `adapters`:
    a tool's name is part of every recorded request's key, so a tool in every read-only session
    would stop the shipped review cassette replaying and move every corpus entry. A strategy says
    `code_search=True`; a session without it has no `search_code` to reach.
    """
    tools = ToolSet.of(
        Tool(
            server=BUILTIN_SERVER,
            name="read_file",
            description=(
                "Read a UTF-8 text file from the repository. A long file is truncated, and the "
                "truncation says how many lines it has: pass `offset` and `limit` to read any part "
                "of it, including past the truncation. Line numbers are the ones `search_text` "
                "reports and the ones a traceback names."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {
                        "type": "integer",
                        "description": "First line to return, 1-based. Omit to start at the top.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "How many lines to return. Omit for the rest of the file.",
                    },
                },
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
    if code_search is not None:
        tools = tools | ToolSet.of(
            Tool(
                server=BUILTIN_SERVER,
                name=SEARCH_TOOL,
                description=(
                    "Search the code graph instead of reading files. It covers HEAD plus what you "
                    "have staged in this session, so a symbol you just wrote is found at its staged "
                    "line. mode=ask: a task in words, ranked symbols with path:lines. mode=grep: a "
                    "regex, hits grouped by the enclosing symbol. mode=callers: who calls a symbol "
                    "(direction=in) or what it calls (direction=out), depth up to 3. mode=skeleton: "
                    "one file's signatures without bodies (file=). mode=map: the repository's "
                    "directories and hubs. Prefer this to read_file for finding code; read what it "
                    "points at."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": list(SEARCH_MODES)},
                        "query": {"type": "string", "description": "The task, regex or symbol name."},
                        "file": {"type": "string", "description": "For mode=skeleton: the file."},
                        "scope": {"type": "string", "description": "A path prefix to search within."},
                        "direction": {"type": "string", "enum": ["in", "out"]},
                        "depth": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_DEPTH},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_LIMIT},
                    },
                    "required": ["mode"],
                },
                capabilities=frozenset({Capability.READS_REPO}),
            )
        )
    runner = ToolRunnerImpl(workspace)
    runner.code_search = code_search
    return tools, runner


def read_write(
    workspace: Workspace, *, code_search: CodeSearch | None = None
) -> tuple[ToolSet, ToolRunnerImpl]:
    """Adds staging a change. Declares WRITES_FILES, which is what makes policy see it.

    That declaration is load-bearing in two places at once: egress enforcement becomes
    mandatory, and `ApprovalGate` gates the action.
    """
    tools, runner = read_only(workspace, code_search=code_search)
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
            name="edit_file",
            description=(
                "Replace one passage of a file with another and stage the result. `old` must occur "
                "exactly once in the file as it currently is (your own staged version, if you have "
                "one), so include enough surrounding lines to make it unique. Prefer this over "
                "write_file for a change to a large file: it costs you the lines you change, not "
                "the whole file, and it cannot drop a line you never meant to touch."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string", "description": "the exact text to replace, once"},
                    "new": {"type": "string", "description": "what takes its place"},
                },
                "required": ["path", "old", "new"],
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
    delegation: bool = False,
    code_search: CodeSearch | None = None,
    validates: ValidateRunner | None = None,
) -> tuple[ToolSet, ToolRunnerImpl]:
    """Read, stage, and run a command. The most capable set the framework ships.

    `delegation=True` adds `delegate` (see `with_delegation`). Off by default: a session that can
    start sessions is a session whose turn count no longer bounds its model calls by itself, and
    a repository should say so in its module rather than find out from its ledger.

    `EXECUTES_CODE` here is not a label — it is the declaration three separate controls key on,
    and declaring it is the point of this function existing rather than a `run_script=True`
    parameter on `read_write`. It makes egress enforcement mandatory before the first model call,
    and it makes `ApprovalGate` a startup requirement for any adapter that also spends money.

    Passing no `commands` runner is allowed and yields a set that still declares the capability
    while every call refuses. That is deliberate: a tool set's declaration is what policy sees, so
    a set that could execute on some other configuration must not read as harmless on this one.
    """
    tools, runner = read_write(workspace, code_search=code_search)
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
            description=_script_description(allowed_commands, commands),
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
    tools = tools | ToolSet.of(
        Tool(
            server=BUILTIN_SERVER,
            name="run_validate",
            description=(
                "Run the repository's own validator (its linter or formatter check) over the "
                "change you have staged, and return what it said. `run_script` cannot do this: it "
                "runs against HEAD, so it reports the code as it was before your edit. Run this "
                "before you finish -- a change that passes the suite and fails the validator is a "
                "change CI rejects."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Restrict to these paths. Empty checks what the binding checks.",
                    }
                },
            },
            # READS_REPO: a validator reads the tree and reports, where a suite executes it. That
            # is why this needs no container and `run_tests` does (GATE-SANDBOX-2) -- both shipped
            # Validate adapters declare READS_REPO and mean it.
            capabilities=frozenset({Capability.READS_REPO}),
        )
    )
    runner.validates = validates
    if delegation:
        tools = with_delegation(tools)
    return tools, runner


def _script_description(allowed_commands: tuple[str, ...], commands: CommandRunner | None) -> str:
    """What `run_script` tells a model it can do, before the model spends a turn finding out.

    A list of twelve programs under the word "Allowed" is read as an inventory, which is the only
    reading available to something that cannot look. Where the sandbox declares what its image
    declares, the description names the intersection instead -- the truth, before the first turn.
    Where it declares nothing, the description says that the list is a policy rather than pretending
    to knowledge nobody supplied.
    """
    shell = "Run a command as an argv list — no shell, so no pipes, globs or redirection. "
    tree = " Runs against the repository working tree, which does NOT contain this run's staged writes."
    executables = tuple(getattr(commands, "executables", ()) or ())
    if not executables:
        return (
            f"{shell}Programs you MAY run: {', '.join(allowed_commands)} — a policy, not an "
            f"inventory: this session's sandbox decides what is actually installed, and a program "
            f"that is not there says so.{tree}"
        )
    here = tuple(program for program in allowed_commands if program in executables)
    if not here:
        return (
            f"{shell}This session's sandbox carries none of the programs you may run, so every "
            f"command is refused. Use `run_tests` and `run_validate`, which run the verbs this "
            f"repository bound.{tree}"
        )
    return (
        f"{shell}Programs available here: {', '.join(here)} — this session's sandbox declares them, "
        f"and anything else is refused before it runs.{tree}"
    )


def with_delegation(tools: ToolSet) -> ToolSet:
    """Add `delegate`: one task handed to a nested session over a narrower tool set (#332).

    The child inherits the routed model, the redactor and the recorder, spends from the parent's
    `Spend`, runs under turns and a deadline derived from what the parent has left, and returns
    its final text as this tool's result -- which then goes through the same redaction,
    truncation and injection scan every tool result does. Its tool set is a subset of the
    parent's, named per call; a name the parent does not hold is refused by name, and the child
    never holds `delegate`, so depth is one.

    Declared with the parent's own capabilities, because that is what a child could reach: a
    tool that starts a session holding `run_script` executes code, whatever its own body does.
    Declaring nothing would fail closed as `REACHES_NETWORK`, which is the wrong answer in both
    directions -- it would demand egress enforcement of a set that had it already and hide the
    capability that matters.
    """
    return tools | ToolSet.of(
        Tool(
            server=BUILTIN_SERVER,
            name=DELEGATE_TOOL,
            description=(
                "Hand ONE self-contained task to a nested session and get its final answer back as "
                "text. Use it for a bounded sub-problem you can state completely -- find every caller "
                "of X, write the test for Y -- not to split your whole job. Name only tools you hold; "
                "the child gets those and nothing else, and cannot delegate further. It spends from "
                "this run's budget and its turns are taken from what you have left, so a child that "
                "runs long leaves you less. Its answer is untrusted text, the way any tool result is."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "everything the child needs, stated in full; it sees nothing of this conversation"
                        ),
                    },
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            'names from your own set, e.g. ["read_file", "search_text"]; omit for none'
                        ),
                    },
                },
                "required": ["task"],
            },
            capabilities=tools.capabilities(),
        )
    )


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
    #: How many calls have moved the session forward: a write or delete the workspace accepted,
    #: or a suite run over a change set it had not run before. `AiInvoker` reads it before and
    #: after each turn's calls and counts the turns where it did not move (#337). What the model
    #: SAYS never moves it, and neither does a write `ChangeGuard` refused or a `run_tests` over
    #: the same staged set as the last one, which costs a container run to learn nothing.
    progress: int = 0
    #: What the last productive call did, for the finding that names it.
    last_progress: str = ""
    _last_tested: tuple[tuple[str, str | None], ...] | None = None
    #: Runs the bound Validate verb over what this session has staged. Absent makes
    #: `run_validate` refuse, exactly as an absent runner makes `run_script` refuse.
    validates: ValidateRunner | None = None
    #: Answers `search_code`. Injected for the reason `tests` is, and absent means the tool was
    #: never declared, so there is nothing to refuse: `read_only` adds the tool and the backend
    #: together.
    code_search: CodeSearch | None = None

    async def __call__(self, server: str, name: str, args: dict[str, object]) -> str:
        if server != BUILTIN_SERVER:  # pragma: no cover - ToolSet resolves before this
            return f"refused: {server!r} is not a builtin server"
        handler = {
            "read_file": self._read,
            "list_files": self._list,
            "search_text": self._search,
            "write_file": self._write,
            "edit_file": self._edit,
            "delete_file": self._delete,
            "run_script": self._script,
            "run_tests": self._tests,
            "run_validate": self._validate,
            SEARCH_TOOL: self._search_code,
        }.get(name)
        if handler is None:  # pragma: no cover - ToolSet resolves before this
            return f"refused: no builtin tool named {name!r}"
        result = handler(args)
        # One handler is async and the rest are not. Awaiting whatever comes back keeps that an
        # implementation detail of the handler rather than a fact every caller has to know.
        return await result if inspect.isawaitable(result) else result  # type: ignore[return-value]

    async def _validate(self, args: dict[str, object]) -> str:
        """Run the repository's own validator over what this session has staged.

        The gap this closes: a session can test its staged writes and could not check them. Its
        writes are staged rather than on disk, so `run_script ruff check` runs in a worktree of
        HEAD -- deliberately, see `WorktreeRunner` -- and answers about the code as it already
        was. A model that ran it got "clean" about a tree it had not changed, and CI was the first
        thing to see the real answer. Run 34294139197 opened a pull request whose suite passed and
        whose lint did not, on four errors ruff would have named in a second.

        Not counted as progress (#337), and that is deliberate rather than an omission: a
        validation over a change set is only new because a write made it new, and that write has
        already counted. Counting both would let one edit buy two turns of allowance.
        """
        if self.validates is None:
            return (
                "refused: no Validate verb is bound, so there is nothing to run. Bind one in "
                ".lockstep/lockstep.py to check staged writes the way run_tests checks them."
            )
        raw = args.get("paths") or ()
        paths = tuple(str(p) for p in raw if str(p).strip()) if isinstance(raw, (list, tuple)) else ()
        for token in paths:
            if token.startswith("-"):
                return (
                    f"refused: {token!r} looks like an option, not a path; run_validate takes "
                    f"paths only, so pass the file or directory and let the validator choose its flags."
                )
        try:
            return await self.validates(paths)
        except Exception as e:  # noqa: BLE001 - a tool result is a message, never a crash
            return f"error: could not run the validator: {e}"

    async def _search_code(self, args: dict[str, object]) -> str:
        """One question to the code graph, over the tree this session sees.

        The model chooses the mode and the words; everything about how the backend is run --
        the argv, the environment, the index, the tree -- is the framework's, so the only thing
        checked here is the question's shape. Every hit passes the read guard before it is shown,
        silently, the way `search_text` skips a protected file: a search is how somebody looks
        for a credential, and the index does not know what the guard knows.
        """
        if self.code_search is None:  # pragma: no cover - never declared without a backend
            return "refused: search_code.unavailable: no code search is bound for this run"
        mode = str(args.get("mode", ""))
        if mode not in SEARCH_MODES:
            return f"refused: search_code.bad_mode: mode must be one of {', '.join(SEARCH_MODES)}"
        query = str(args.get("query", "") or "")
        file = str(args.get("file", "") or "")
        if mode in ("ask", "grep", "callers") and not query.strip():
            return f"refused: search_code.no_query: mode={mode} needs a query"
        if mode == "skeleton" and not file.strip():
            return "refused: search_code.no_file: mode=skeleton needs file="
        bounded: dict[str, object] = {
            "query": query.strip(),
            "file": file.strip(),
            "scope": str(args.get("scope", "") or "").strip(),
            "direction": "out" if args.get("direction") == "out" else "in",
            "depth": max(1, min(MAX_SEARCH_DEPTH, _int(args.get("depth"), 1))),
            "limit": max(1, min(MAX_SEARCH_LIMIT, _int(args.get("limit"), DEFAULT_SEARCH_LIMIT))),
        }
        staged = tuple((c.path, c.contents) for c in self.workspace.changes)
        try:
            answer = await self.code_search(mode, bounded, staged)
        except Exception as e:  # noqa: BLE001 - a tool result is a message, never a crash
            return f"error: search_code could not answer: {e}"
        if answer.refusal is not None:
            return answer.refusal
        shown = [
            hit.line
            for hit in answer.hits
            if not hit.path
            or (
                self.workspace.guard.check_read(hit.path) is None
                and self.workspace.inside(self.workspace.resolve(hit.path))
            )
        ]
        if not shown:
            return answer.empty
        # `limit` bounds the ranked modes. A skeleton or a map is one answer, not a ranking, and
        # ten lines of either would be a file with its middle missing; the character cap bounds those.
        ranked = mode in ("ask", "grep", "callers")
        limit = _int(bounded["limit"], DEFAULT_SEARCH_LIMIT) if ranked else len(shown)
        text = "\n".join(shown[:limit])
        if len(shown) > limit:
            text += f"\n…[{len(shown) - limit} more; raise limit or narrow the query]"
        if len(text) > MAX_SEARCH_CHARS:
            text = text[:MAX_SEARCH_CHARS] + "\n…[truncated; narrow the query]"
        return text

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
        # Option-confusion, the rule the worktree applies to a ref: a path reaches pytest as an
        # argv token, so `-p module`, `-c other.ini` or `-o cache_dir=/elsewhere` would be options
        # the model chose. A test path never begins with a dash, so one that does is refused by
        # name before anything runs (#313).
        for token in paths:
            if token.startswith("-"):
                return (
                    f"refused: {token!r} looks like a pytest option, not a test path; run_tests takes "
                    f"paths only, so pass the file or directory and let the runner choose its flags."
                )
        self._test_runs += 1
        staged = tuple((c.path, c.contents) for c in self.workspace.changes)
        if staged != self._last_tested:
            self._last_tested = staged
            self._advance(f"ran the suite over {len(staged)} staged change(s)")
        try:
            return await self.tests(paths)
        except Exception as e:  # noqa: BLE001 - a tool result is a message, never a crash
            # A failing runner is something the model can react to. Raising here would end the
            # session with the staged work unreported, which is the loss this tool exists to avoid.
            return f"error: could not run the suite: {e}"

    def _read(self, args: dict[str, object]) -> str:
        path = str(args.get("path", ""))
        offset, limit = _int(args.get("offset"), 0), _int(args.get("limit"), 0)
        # `check_read`, not `check_path`. This asked the guard and then acted on ONE of its
        # answers: every tier-1 refusal but `outside-repo-root` was computed and thrown away, so
        # `read_file(".env")` returned the file. The guard was consulted and overruled.
        #
        # A read is its own question, which is why the guard now has its own answer for it rather
        # than this reusing the write tiers — `.github/` must stay readable or an agent asked to
        # fix a workflow cannot see the workflow.
        refusal = self.workspace.guard.check_read(path)
        if refusal is not None:
            return f"refused: {path} is protected ({refusal.rule})"
        # This session's own staged version first. A model that writes and then reads was shown
        # the disk, which is the file as it was, and reasoned from that: the fix step of this
        # repository's eighth `/fix` on itself would have read the unfixed file over the fix it
        # had staged one step earlier (#337). What a session staged is what it sees.
        for change in reversed(self.workspace.changes):
            if change.path == path:
                if change.contents is None:
                    return f"error: no file at {path} (you staged its deletion)"
                # The staged version goes through the same window as the disk version: a file a
                # session wrote is exactly the file it is most likely to be too long to read back.
                return _window(change.contents, path, offset, limit)
        target = self.workspace.resolve(path)
        if not target.is_file():
            return f"error: no file at {path}"
        if not self.workspace.inside(target):
            # The same rule as the string guard's `outside-repo-root`, applied to where the file
            # IS rather than what it is called (GATE-GUARD-4).
            return f"refused: {path} is protected (outside-repo-root: it resolves outside the repository)"
        try:
            text = target.read_text()
        except (OSError, UnicodeDecodeError) as e:
            return f"error: {e}"
        return _window(text, path, offset, limit)

    def _staged(self) -> dict[str, str | None]:
        """This session's own writes, the last one per path, keyed the way the tree is walked."""
        view: dict[str, str | None] = {}
        for change in self.workspace.changes:
            view[posixpath.normpath(change.path.replace("\\", "/"))] = change.contents
        return view

    def _files(self, glob: str, staged: dict[str, str | None]) -> Iterator[tuple[str, Path | None]]:
        """Every file in the tree as THIS SESSION sees it, in order: the disk, minus what the
        session staged as deleted, plus what it staged as written.

        `read_file` got this view in #337 and `list_files` and `search_text` stayed on the disk, so
        a model that wrote a file and searched for the symbol it had just defined was told
        "(no matches)" -- and read the file a turn later to check, which is the turn this saves.
        The three tools answer from one walk so they cannot disagree about what exists; a path is
        yielded with `None` when its text is the staged version rather than a file on disk.

        The guard and the symlink rule apply to staged names too, for the same reason they apply
        to disk names: a search is how somebody looks for a credential, and a listing of what was
        declined is a map of where to look. Skipped silently, never named.
        """
        root = self.workspace.root
        entries: dict[str, Path | None] = {
            str(p.relative_to(root)): p for p in root.rglob("*") if p.is_file()
        }
        for rel, contents in staged.items():
            if contents is None:
                entries.pop(rel, None)
            else:
                entries[rel] = None
        for rel in sorted(entries):
            if not fnmatch.fnmatch(rel, glob):
                continue
            if self.workspace.guard.check_read(rel) is not None:
                continue
            path = entries[rel]
            # `rglob` yields a link by its in-tree name and `read_text` would follow it out.
            if path is not None and not self.workspace.inside(path):
                continue
            yield rel, path

    def _list(self, args: dict[str, object]) -> str:
        pattern = str(args.get("glob", "*"))
        # Names, not contents, so the stake is lower than `read_file`'s — but a listing of
        # `.git/` is noise at best, and a listing of what is protected is a map of where to look.
        matches = [rel for rel, _ in self._files(pattern, self._staged())]
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
        staged = self._staged()
        # The same walk `list_files` takes, guarded the same way (a search is how somebody LOOKS
        # for a credential, and this once walked `rglob("*")` with no guard at all, so
        # `search_text(pattern="API_KEY")` returned lines out of `.env` and `.git/`), and over
        # the session's own staged writes rather than the disk they have not reached.
        for rel, path in self._files(glob, staged):
            if path is None:
                text = staged[rel] or ""
            else:
                try:
                    text = path.read_text()
                except (OSError, UnicodeDecodeError):
                    # A binary file is not a search failure, and reporting one per binary would
                    # bury the answer under noise about the things that were never candidates.
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
        path = str(args.get("path", ""))
        answer = self.workspace.record(path, str(args.get("contents", "")))
        if answer.startswith("ok:"):
            self._advance(f"wrote {path}")
        return answer

    def _edit(self, args: dict[str, object]) -> str:
        """One passage for another, staged as the whole file.

        The seventh `/fix` on this repository's own #319 had its fix worked out and spent twenty
        turns trying to put it into a 30 KB file without retyping the file: the only write was
        `write_file`, which takes whole contents, so it scripted string replacements through
        `run_script`, whose worktree is thrown away (#337). A targeted edit is the tool it was
        reaching for. The replacement is done on the VIEW the model has -- the file as the
        redacting sink shows it -- and `Workspace.record` puts the masked values back, so an
        edit near a key-shaped string works the same way a rewrite does (GATE-REDACT-3), and an
        edit that touches one is refused there by count.
        """
        path = str(args.get("path", ""))
        old, new = str(args.get("old", "")), str(args.get("new", ""))
        if not old:
            return "error: edit_file needs a non-empty `old`; to write a new file, use write_file"
        current = self._current(path)
        if current is None:
            return f"error: no file at {path} to edit; write_file creates one"
        if isinstance(current, str) and current.startswith("refused:"):
            return current
        view = self.workspace.redact.text(current)
        count = view.count(old)
        if count == 0:
            return (
                f"error: `old` was not found in {path} as it currently is. Read the file again and "
                f"copy the passage exactly, whitespace included."
            )
        if count > 1:
            return (
                f"error: `old` occurs {count} times in {path}; include more surrounding lines so it "
                f"occurs once"
            )
        answer = self.workspace.record(path, view.replace(old, new, 1))
        if answer.startswith("ok:"):
            self._advance(f"edited {path}")
        return answer

    def _current(self, path: str) -> str | None:
        """The file as this session sees it: its own staged version if it has one, else the disk,
        through the same read refusals `read_file` applies. None when there is nothing there."""
        for change in reversed(self.workspace.changes):
            if change.path == path:
                return change.contents
        refusal = self.workspace.guard.check_read(path)
        if refusal is not None:
            return f"refused: {path} is protected ({refusal.rule})"
        target = self.workspace.resolve(path)
        if not target.is_file() or not self.workspace.inside(target):
            return None
        try:
            return target.read_text()
        except (OSError, UnicodeDecodeError):
            return None

    def _delete(self, args: dict[str, object]) -> str:
        path = str(args.get("path", ""))
        answer = self.workspace.record(path, None)
        if answer.startswith("ok:"):
            self._advance(f"deleted {path}")
        return answer

    def _advance(self, what: str) -> None:
        self.progress += 1
        self.last_progress = what

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
        # What the sandbox says it has, when it says anything. Answered here rather than by
        # starting a container and reading a 127 off it: the turn is the same either way, and the
        # round trip is not. Empty means nobody declared, which is unknown and not "none".
        executables = tuple(getattr(self.commands, "executables", ()) or ())
        if executables and program not in executables:
            return _not_in_this_sandbox(program, self, declared=False)

        requested = args.get("timeout_seconds")
        timeout = (
            float(requested) if isinstance(requested, (int, float)) and requested else self.script_timeout
        )
        result = await self.commands.run(argv, cwd=str(self.workspace.root), timeout=timeout)
        exit_code = getattr(result, "exit_code", 1)
        how = getattr(result, "how", "unknown")
        if exit_code == 127:
            # The backstop, not the mechanism. A declaration decides what the model is told and
            # never what is true, so a program that reaches here either was never declared -- an
            # undeclared sandbox, which is one turn to find out -- or was declared and is missing,
            # which is a stale `executables=` and says so.
            return _not_in_this_sandbox(program, self, declared=bool(executables), how=how)
        # The tail, not the head. A test run's useful part is the failure summary at the end, and
        # truncating from the front is how a model ends up reasoning about the collection banner.
        body = _tail(str(getattr(result, "stdout", "")), str(getattr(result, "stderr", "")))
        return f"exit {exit_code} ({how})\n{body}"


def _window(text: str, path: str, offset: int, limit: int) -> str:
    """What a read returns: the whole file, or the lines asked for, capped either way.

    The cap stays where it is and a caller cannot raise it: what a read returns is re-sent on every
    later turn, so an uncapped read is an uncapped prompt. What is new is being able to say WHICH
    forty thousand characters. Without that, a model handed a long file saw its first fifth and
    nothing else, ever -- run 34361896059 spent turns trying to reach line 800 of a file it was
    editing, and two of the cases harvested from it are that attempt (#402).

    Lines, not characters. That is the unit `search_text` answers in, the unit a traceback names,
    and the unit the model asked in when it could not: "extract lines 770-820". `offset` is 1-based
    for the same reason -- line 1 is the first line everywhere a person or a compiler counts.

    A truncated answer says how to continue, because a model told only that a file was cut does
    what that run did: work around the tool instead of using it.
    """
    lines = text.splitlines(keepends=True)
    total = len(lines)
    if not (offset or limit):
        if len(text) <= MAX_READ_CHARS:
            return text
        return (
            f"{text[:MAX_READ_CHARS]}\n…[truncated at {MAX_READ_CHARS} chars; {path} has {total} "
            f"line(s). Pass `offset` (1-based) and `limit` to read any part of it]"
        )
    start = max(offset - 1, 0)
    if start >= total:
        # An answer rather than an error: the model asked about a place, and where the file ends is
        # what it needed to know. Empty output would read as an empty file.
        return f"error: {path} has {total} line(s), so line {offset} is past its end"
    window = lines[start : start + limit] if limit else lines[start:]
    body = "".join(window)
    head = f"[{path} lines {start + 1}-{start + len(window)} of {total}]\n"
    if len(body) > MAX_READ_CHARS:
        return (
            f"{head}{body[:MAX_READ_CHARS]}\n…[truncated at {MAX_READ_CHARS} chars of the range "
            f"you asked for; raise `offset` to read on]"
        )
    return head + body


def _executes_here(runner: ToolRunnerImpl) -> str:
    """The tools in THIS session that run the repository's own bound verbs, named.

    Derived, never tabulated. The first version of this carried a map from program name to advice
    -- pytest to `run_tests`, ruff to `run_validate` -- which is the framework guessing at what a
    program is for in somebody else's repository (`cargo` is build and test; `make` is whatever
    the Makefile says) and, worse, asserting bindings that may not exist: telling a model to use
    `run_tests` where no Test verb is bound sends it from one dead end to another. What is bound
    is a fact this object holds.
    """
    have = []
    if runner.tests is not None:
        have.append("`run_tests` runs the bound Test verb over what you have staged")
    if runner.validates is not None:
        have.append("`run_validate` runs the bound Validate the same way")
    if not have:
        return (
            " Nothing in this session executes the repository's own tools, so reason from what you can read."
        )
    return f" {'; '.join(have)} -- and both see your staged writes, which `run_script` does not."


def _not_in_this_sandbox(program: str, runner: ToolRunnerImpl, *, declared: bool, how: str = "") -> str:
    """What a model is told when a program it may run is not where the command would run.

    Two facts and one instruction, because a bare `exit 127` is indistinguishable from a command
    that ran and failed: the program is absent, the allowlist never said otherwise, and here is
    what this session has instead. A model given only the first works around the tool by reading
    source, which is not progress and burns an idle allowance (#401).

    `declared` says which side was wrong. A sandbox that never listed its programs is simply
    unknown, and finding out this way costs one turn. A sandbox that DECLARED this program and
    does not have it is a stale declaration, and saying so points at the line to fix rather than
    leaving somebody to conclude the tool is broken.
    """
    if declared:
        head = (
            f"error: {program!r} is declared in this session's sandbox `executables=` and is not "
            f"installed in the image ({how or 'not found'}). The declaration is stale; fix it "
            f"where the sandbox is bound."
        )
    else:
        head = (
            f"error: {program!r} is not in this session's sandbox. The allowed list is what you MAY "
            f"run, not what is installed here."
        )
    return head + _executes_here(runner)


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


def _int(value: object, default: int) -> int:
    """A model's integer argument, or the default: never a crash over a string it sent."""
    try:
        return int(str(value)) if value not in (None, "") else default
    except ValueError:
        return default
