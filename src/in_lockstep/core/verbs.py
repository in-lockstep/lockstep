"""Verbs, capabilities, and the action interface.

A verb is a small generic interface with a typed input and output. Adapters declare what they can
*do* — write files, execute code, reach the network — and policy keys off that without knowing
anything about the adapter. Workflows do not: a workflow asks for `Test`, not for something that
happens to run pytest.
"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar, Protocol, TypeVar, runtime_checkable

from .outcome import Outcome

InputT = TypeVar("InputT", contravariant=True)
ValueT = TypeVar("ValueT")


class Verb(Enum):
    BUILD = "build"
    TEST = "test"
    VALIDATE = "validate"
    RUN = "run"
    IMPLEMENT = "implement"
    FIX = "fix"
    REVIEW = "review"
    TRIAGE = "triage"
    DEBUG = "debug"


class Capability(Enum):
    """What an adapter can do. Declared, never inferred.

    `REACHES_NETWORK` exists because "read-only" is about mutation, not transmission: a fetch or
    search tool mutates nothing and is still an egress channel. Without it, such a tool launders
    itself as harmless and drops a run below the threshold where egress control is mandatory.
    """

    WRITES_FILES = "writes_files"
    EXECUTES_CODE = "executes_code"
    REACHES_NETWORK = "reaches_network"
    SPENDS_BUDGET = "spends_budget"
    READS_REPO = "reads_repo"


@runtime_checkable
class Action(Protocol[InputT, ValueT]):
    """What every adapter satisfies. The verb interface knows nothing about AI or determinism."""

    verb: ClassVar[Verb]
    capabilities: ClassVar[frozenset[Capability]]

    async def invoke(self, ctx: object, inp: InputT) -> Outcome[ValueT]: ...


def capabilities_of(action: object) -> frozenset[Capability]:
    caps = getattr(action, "capabilities", None)
    return caps if isinstance(caps, frozenset) else frozenset()


def verb_of(action: object) -> Verb | None:
    verb = getattr(action, "verb", None)
    return verb if isinstance(verb, Verb) else None
