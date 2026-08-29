"""The tracker-agnostic shape.

The protocol is wider than what 1.0 calls, deliberately. Retrofitting a method onto a Protocol
that third parties implement is a breaking change — `core/ports` makes the same argument for
`compare_and_set` — so the shapes the named workflows need (triage acting on its verdict,
backport targeting a release line, RFE creating the ticket it refined) are committed now, with
`Unsupported`-raising defaults so a tracker that cannot do one says so instead of growing a
different-shaped method later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from ...ai.context import ContextItem, Provenance
from ...core.ports import Unsupported


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
    # Release traceability — what a backport workflow routes on and a release manager greps for.
    # Empty on trackers that have no such concept, which GitHub Issues mostly is: `milestone` is
    # the closest it comes, and the version tuples stay empty there rather than being guessed
    # from labels.
    fix_versions: tuple[str, ...] = ()
    affects_versions: tuple[str, ...] = ()
    milestone: str = ""

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
    """What a workflow may ask a tracker for.

    `get` and `comment` are required — nothing qualifies as a ticket source without them. The
    rest default to a refusal rather than being absent, so a workflow can catch `Unsupported`
    and degrade honestly, and an adapter that cannot serve one never invents a signature.
    """

    async def get(self, key: str) -> Ticket: ...

    async def comment(self, ticket: Ticket, body: str) -> None: ...

    async def create(self, draft: TicketDraft) -> Ticket:  # pragma: no cover - default refuses
        raise Unsupported("this TicketSource does not create tickets")

    async def search(
        self, query: str, *, limit: int = 20
    ) -> tuple[Ticket, ...]:  # pragma: no cover - default refuses
        raise Unsupported("this TicketSource does not search")

    async def add_labels(self, ticket: Ticket, *labels: str) -> None:  # pragma: no cover
        raise Unsupported("this TicketSource does not label")

    async def transition(
        self, ticket: Ticket, state: TicketState, *, raw: str = ""
    ) -> None:  # pragma: no cover - default refuses
        """Move a ticket. `raw` names the tracker's own state where the enum is too coarse —
        the same escape hatch `Ticket.raw_state` provides in the other direction."""
        raise Unsupported("this TicketSource does not transition tickets")


_HEADING = re.compile(r"(?im)^#{1,6}\s*acceptance criteria\s*$")
_TASK = re.compile(r"(?m)^\s*[-*]\s*\[[ xX]\]\s*(.+)$")
# The checkbox is optional here, and its absence was a bug. An issue with BOTH an "Acceptance
# criteria" heading and a task list under it — which is how people actually write one — took the
# heading branch, matched with this pattern, and carried "[ ] " into the text of every criterion.
# The `_TASK` fallback stripped it correctly, so the defect only appeared on the better-formatted
# input, which is the wrong way round.
_BULLET = re.compile(r"(?m)^\s*[-*]\s+(?:\[[ xX]\]\s*)?(.+)$")


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
