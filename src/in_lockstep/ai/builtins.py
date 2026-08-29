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
"""

from __future__ import annotations

import fnmatch
import posixpath
from dataclasses import dataclass, field
from pathlib import Path

from ..core.changes import ChangeGuard
from ..core.types import ChangeAuthor, ChangeSet, FileChange
from ..core.verbs import Capability
from .tools import BUILTIN_SERVER, Tool, ToolSet

# A read that returns a whole vendored tree is a prompt nobody budgeted for.
MAX_READ_CHARS = 40_000
MAX_LISTED = 200


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


@dataclass
class ToolRunnerImpl:
    """Dispatches the builtin names. Anything else is not reachable from here."""

    workspace: Workspace

    async def __call__(self, server: str, name: str, args: dict[str, object]) -> str:
        if server != BUILTIN_SERVER:  # pragma: no cover - ToolSet resolves before this
            return f"refused: {server!r} is not a builtin server"
        handler = {
            "read_file": self._read,
            "list_files": self._list,
            "write_file": self._write,
            "delete_file": self._delete,
        }.get(name)
        if handler is None:  # pragma: no cover - ToolSet resolves before this
            return f"refused: no builtin tool named {name!r}"
        return handler(args)

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

    def _write(self, args: dict[str, object]) -> str:
        return self.workspace.record(str(args.get("path", "")), str(args.get("contents", "")))

    def _delete(self, args: dict[str, object]) -> str:
        return self.workspace.record(str(args.get("path", "")), None)


__all__ = ["MAX_READ_CHARS", "ToolRunnerImpl", "Workspace", "read_only", "read_write"]
