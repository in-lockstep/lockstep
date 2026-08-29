"""The tracker-agnostic shape."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from ...ai.context import ContextItem, Provenance


class TicketState(Enum):
    """Framework-level states, with an escape hatch.

    Every tracker's state machine differs, so `raw` carries what the adapter actually saw and the
    enum carries what a workflow can branch on without knowing the tracker.
    """

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    CLOSED = "closed"
    OTHER = "other"


class TicketType(Enum):
    BUG = "bug"
    STORY = "story"
    TASK = "task"
    EPIC = "epic"
    SPIKE = "spike"
    OTHER = "other"


@dataclass(frozen=True)
class TicketRef:
    key: str
    url: str = ""


@dataclass(frozen=True)
class Ticket:
    key: str
    title: str
    description: str = ""
    state: TicketState = TicketState.OPEN
    type: TicketType = TicketType.OTHER
    url: str = ""
    labels: tuple[str, ...] = ()
    assignees: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    comments: tuple[str, ...] = ()
    raw_state: str = ""

    def as_context(self) -> tuple[ContextItem, ...]:
        """Ticket text is untrusted: anyone who can file one can write into a prompt."""
        items = [
            ContextItem(
                kind="ticket",
                content=f"{self.key}: {self.title}\n\n{self.description}",
                provenance=Provenance.UNTRUSTED_EXTERNAL,
                path=self.key,
            )
        ]
        items += [
            ContextItem(
                kind="ticket",
                content=comment,
                provenance=Provenance.UNTRUSTED_EXTERNAL,
                path=f"{self.key}#comment",
            )
            for comment in self.comments
        ]
        return tuple(items)


@dataclass(frozen=True)
class TicketDraft:
    title: str
    description: str = ""
    type: TicketType = TicketType.TASK
    labels: tuple[str, ...] = ()


@runtime_checkable
class TicketSource(Protocol):
    async def get(self, key: str) -> Ticket: ...

    async def comment(self, ticket: Ticket, body: str) -> None: ...


_HEADING = re.compile(r"(?im)^#{1,6}\s*acceptance criteria\s*$")
_TASK = re.compile(r"(?m)^\s*[-*]\s*\[[ xX]\]\s*(.+)$")
_BULLET = re.compile(r"(?m)^\s*[-*]\s+(.+)$")


def criteria_from(body: str) -> tuple[str, ...]:
    """An acceptance-criteria heading if there is one, else a task list.

    Falling back to the task list matters more than it looks: most trackers have no criteria
    field, and a checklist is what people actually write.
    """
    match = _HEADING.search(body)
    if match:
        section = body[match.end() :]
        nxt = re.search(r"(?m)^#{1,6}\s", section)
        if nxt:
            section = section[: nxt.start()]
        found = [m.group(1).strip() for m in _BULLET.finditer(section)]
        if found:
            return tuple(found)
    return tuple(m.group(1).strip() for m in _TASK.finditer(body))
