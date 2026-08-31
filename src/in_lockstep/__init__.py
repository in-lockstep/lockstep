"""in-lockstep — an agentic SDLC framework.

The lifecycle is executable Python, not a rendered manifest: `.lockstep/lockstep.py` IS the
thing that runs. Nothing here emits YAML.

See design/in-lockstep-design.md, and design/adr/0001 for why this replaced a compiler.
"""

from .core import (
    Capability,
    ChangeGuard,
    ChangeSet,
    Container,
    Cost,
    FileChange,
    Finding,
    Outcome,
    Policy,
    PolicyStack,
    RunContext,
    Severity,
    Spend,
    Status,
    Test,
    TestReport,
    Validate,
    ValidationReport,
    Verb,
    workflow,
)
from .core.spend import Budget
from .lockstep import Lockstep, Workshop

# The ticket vocabulary is part of the authoring surface: a workflow signature says
# `tickets: TicketSource` and a request says `ticket=await tickets.get(...)`, so these names
# belong at the root rather than behind `in_lockstep.platform.tickets`.
from .platform.tickets import Ticket, TicketDraft, TicketSource, TicketState, TicketType

# Kept in step with `pyproject.toml` by hand, and by a test — the two are separate declarations
# and a wheel whose `--version` disagrees with its own name is not something a tag check catches.
__version__ = "1.0.0"

__all__ = [
    "Budget",
    "Capability",
    "ChangeGuard",
    "ChangeSet",
    "Container",
    "Cost",
    "FileChange",
    "Finding",
    "Lockstep",
    "Outcome",
    "Policy",
    "PolicyStack",
    "RunContext",
    "Severity",
    "Spend",
    "Status",
    "Test",
    "TestReport",
    "Ticket",
    "TicketDraft",
    "TicketSource",
    "TicketState",
    "TicketType",
    "Validate",
    "ValidationReport",
    "Verb",
    "Workshop",
    "__version__",
    "workflow",
]
