"""in-lockstep — an agentic SDLC framework.

The lifecycle is executable Python, not a rendered manifest: `lockstep.py` at your repository root
IS the thing that runs. Nothing here emits YAML.

See design/in-lockstep-design.md, and design/adr/0001 for why this replaced a compiler.
"""

from .core import (
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
    TestReport,
    TestSpec,
    ValidateSpec,
    ValidationReport,
    Verb,
    workflow,
)
from .lockstep import Lockstep

__version__ = "0.2.0.dev0"

__all__ = [
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
    "TestReport",
    "TestSpec",
    "ValidateSpec",
    "ValidationReport",
    "Verb",
    "__version__",
    "workflow",
]
