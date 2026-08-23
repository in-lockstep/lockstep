"""Parsing a chat-ops command out of a comment.

A workflow triggered by a comment runs on behalf of the repository, not the commenter. Deciding what
a comment asked for is therefore parsing untrusted input, and it belongs in tested code rather than
in a shell one-liner inside a workflow.

This module only reads the comment. Deciding whether the *author* may ask for it is a separate
question, answered against the API by the action that calls this.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

# A command must open a line. Otherwise quoting somebody else's comment, or discussing the command in
# prose, would invoke it.
COMMAND_LINE = re.compile(r"^\s*(?P<command>/[A-Za-z][\w-]*)(?P<rest>.*)$", re.MULTILINE)


@dataclass
class Invocation:
    matched: bool = False
    command: str = ""
    arguments: dict[str, str] = field(default_factory=dict)
    positional: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def instruction(self) -> str:
        """Whatever free text followed the arguments — the human's actual request."""
        return self.note.strip()


def parse(body: str, command: str, *, names: list[str] | None = None) -> Invocation:
    """Find `command` at the start of a line and read its arguments.

    Accepts `--key=value`, `key=value`, and bare positionals mapped onto `names` in order, so a
    reviewer can write whichever feels natural in a comment box.
    """
    wanted = command if command.startswith("/") else f"/{command}"

    for match in COMMAND_LINE.finditer(body or ""):
        if match.group("command") != wanted:
            continue

        rest = match.group("rest").strip()
        try:
            tokens = shlex.split(rest)
        except ValueError:
            # An unbalanced quote is a human typing, not an attack. Fall back to whitespace.
            tokens = rest.split()

        invocation = Invocation(matched=True, command=wanted)
        leftovers: list[str] = []
        for token in tokens:
            cleaned = token[2:] if token.startswith("--") else token
            if "=" in cleaned:
                key, _, value = cleaned.partition("=")
                invocation.arguments[key.strip().replace("-", "_")] = value.strip()
            else:
                leftovers.append(token)

        # Bare words fill the declared argument names in order, so `/implement APP-412` works.
        for name, value in zip(names or [], leftovers, strict=False):
            invocation.arguments.setdefault(name.replace("-", "_"), value)
        invocation.positional = leftovers

        # Everything after the command line is context the human wrote for the run.
        remainder = (body or "")[match.end() :].strip()
        invocation.note = remainder
        return invocation

    return Invocation(matched=False)
