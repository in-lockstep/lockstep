# Extracted from pipeline-framework src/executors/types.py on 2026-08-23.
# Behaviour is preserved verbatim; only imports and configuration plumbing were adapted.
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ToolExecutor(Protocol):
    """Interface for tool executors (browser, API, CLI)."""

    async def execute_tool(self, name: str, params: dict[str, object]) -> ToolResult: ...


@dataclass
class ToolResult:
    text: str = ""
    image: bytes | None = None
    screenshot_path: str | None = None


@dataclass
class ScriptStep:
    step: int = 0
    tool: str = ""
    action: str = ""
    params: dict[str, object] = field(default_factory=dict)
    expected: str = ""


@dataclass
class ExecutedStep:
    phase: str = ""  # setup | test | teardown
    step_number: int = 0
    tool: str = ""
    action: str = ""
    expected: str = ""
    result: str = ""
    status: str = ""  # passed | failed | warn | skipped
    retried: bool = False
    screenshot_path: str | None = None


@dataclass
class TestScript:
    story_id: str = ""
    summary: str = ""
    description: str = ""
    test_type: str = ""  # api | ui | cli
    tags: list[str] = field(default_factory=list)
    setup_steps: list[ScriptStep] = field(default_factory=list)
    test_steps: list[ScriptStep] = field(default_factory=list)
    teardown_steps: list[ScriptStep] = field(default_factory=list)
    execution_tier: int = 1
    heal_regen_count: int = 0


@dataclass
class TestResult:
    story_id: str = ""
    passed: bool = False
    summary: str = ""
    executed_steps: list[ExecutedStep] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
