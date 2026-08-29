"""in-lockstep — an agentic SDLC framework.

The lifecycle is executable Python, not a rendered manifest: `lockstep.py` at your repository root
IS the thing that runs. Nothing here emits YAML.

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
    TestReport,
    TestSpec,
    ValidateSpec,
    ValidationReport,
    Verb,
    workflow,
)
from .lockstep import Lockstep

# Kept in step with `pyproject.toml` by hand, and by a test — the two are separate declarations
# and a wheel whose `--version` disagrees with its own name is not something a tag check catches.
__version__ = "1.0.0"

__all__ = [
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
    "TestReport",
    "TestSpec",
    "ValidateSpec",
    "ValidationReport",
    "Verb",
    "__version__",
    "workflow",
]
