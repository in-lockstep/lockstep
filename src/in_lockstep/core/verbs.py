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


class Verb:
    """A verb name: a routing key and a telemetry label, open to extension.

    This was a closed `Enum`, which made the framework's own verbs the only ones that could exist.
    A user could bind a new interface — `lockstep.bind(Benchmark, PyperfBenchmark())` works — but
    the adapter's `verb` attribute had to borrow a shipped member, so a benchmark run reported
    itself as `run` in every span, metric dimension and step id, and shared a strategy namespace
    with something unrelated. Extension was possible and mislabelled, which is worse than blocked.

    Nothing was using the enum-ness: no iteration, no `match`, no `Verb["TEST"]`. What the enum
    genuinely provided was **identity** — `verb is Verb.TEST` — and that is preserved here by
    interning, so `Verb("test") is Verb.TEST` holds and every existing comparison keeps working.

    Values are normalised to lowercase and interned, which also means a typo makes a *distinct*
    verb rather than silently aliasing an existing one. That failure surfaces as a verb nothing is
    bound to — visible in `in-lockstep ls` — rather than as work routed to the wrong strategy.
    """

    __slots__ = ("value",)

    #: The normalised name. Annotation only — `__slots__` owns the storage.
    value: str

    _interned: ClassVar[dict[str, Verb]] = {}

    # Shipped verbs, assigned below the class body. Declared here so type checkers and readers see
    # the full set in one place.
    BUILD: ClassVar[Verb]
    TEST: ClassVar[Verb]
    VALIDATE: ClassVar[Verb]
    RUN: ClassVar[Verb]
    IMPLEMENT: ClassVar[Verb]
    FIX: ClassVar[Verb]
    BACKPORT: ClassVar[Verb]
    REVIEW: ClassVar[Verb]
    TRIAGE: ClassVar[Verb]
    DEBUG: ClassVar[Verb]

    def __new__(cls, value: str) -> Verb:
        key = value.strip().lower()
        if not key:
            raise ValueError("a verb needs a name")
        existing = cls._interned.get(key)
        if existing is not None:
            return existing
        verb = super().__new__(cls)
        object.__setattr__(verb, "value", key)
        cls._interned[key] = verb
        return verb

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Verb is immutable; construct a new one")

    def __repr__(self) -> str:
        return f"Verb({self.value!r})"

    def __str__(self) -> str:
        return self.value

    def __reduce__(self) -> tuple[type[Verb], tuple[str]]:
        # Interning has to survive a round trip, or a verb that came back from a pickle would
        # compare unequal to the one it left as.
        return (Verb, (self.value,))

    @classmethod
    def known(cls) -> tuple[Verb, ...]:
        """Every verb defined so far, shipped or user-declared."""
        return tuple(sorted(cls._interned.values(), key=lambda v: v.value))

    @classmethod
    def forget_custom(cls) -> None:
        """Test helper: drop user-defined verbs, keeping the shipped ones.

        The intern table is process-global, which is right — identity is the whole reason `Verb`
        could stop being an enum without breaking `verb is Verb.TEST`. But process-global means a
        verb defined in one test is still defined in the next, and a test asserting that `ls` is
        quiet about custom verbs then fails because an earlier test invented one. `workflow.clear`
        exists for the identical reason on the identical kind of registry.
        """
        for name in [k for k in cls._interned if k not in SHIPPED_VERBS]:
            del cls._interned[name]


#: The verbs the framework ships. Named as a set because `Verb.known()` is open by design and
#: callers sometimes need to ask the narrower question — `ls` prints user-defined verbs that
#: nothing serves, and an unbound *shipped* verb is ordinary rather than a mistake.
SHIPPED_VERBS = (
    "build",
    "test",
    "validate",
    "run",
    "implement",
    "fix",
    "backport",
    "review",
    "triage",
    "debug",
)

for _shipped in SHIPPED_VERBS:
    setattr(Verb, _shipped.upper(), Verb(_shipped))
del _shipped


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


class UngatedAgency(Exception):
    """An adapter lets a model write or execute, and no approval path is configured.

    Beside `UndeclaredBudget` rather than in `middleware/`, because both are startup refusals
    about the shape of a lifecycle, and `core` cannot see the package that implements the gate.
    """


#: Capabilities that need a human in the loop when an agent — rather than a person — decides to
#: use them. `Sandbox` is the answer for a deterministic adapter that executes code; approval is
#: the answer for one that spends money AND can write, because there a model is choosing.
NEEDS_APPROVAL = frozenset({Capability.WRITES_FILES, Capability.EXECUTES_CODE})


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
