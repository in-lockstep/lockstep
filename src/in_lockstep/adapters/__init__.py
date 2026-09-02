"""Deterministic adapters over real tools.

The verb interfaces are exported beside their shipped implementations, because a lifecycle file
binds one to the other — `lockstep.bind(Test, PytestTest())` — and should not have to know which
implementation module happens to declare the interface it is binding.
"""

from .command import Build, CommandBuild, CommandRun, CommandTest, CommandValidate, Run, parse_junit
from .detected import detected_bindings
from .pytest_adapter import PytestTest, Test
from .ruff_adapter import RuffValidate, Validate

__all__ = [
    "Build",
    "CommandBuild",
    "CommandRun",
    "CommandTest",
    "CommandValidate",
    "PytestTest",
    "Run",
    "RuffValidate",
    "Test",
    "Validate",
    "detected_bindings",
    "parse_junit",
]
