# Extracted from pipeline-framework src/executors/cli_session.py on 2026-08-23.
# Behaviour is preserved verbatim; only imports and configuration plumbing were adapted.
from __future__ import annotations

import asyncio
import os
import subprocess

from .types import ToolResult


class CliSession:
    """Shell command execution for CLI tests."""

    async def execute_tool(self, name: str, params: dict[str, object]) -> ToolResult:
        if name != "run_command":
            return ToolResult(text=f"Unknown CLI tool: {name}")

        command = str(params.get("command", ""))
        timeout = int(str(params.get("timeout", 30)))
        working_dir = str(params.get("working_dir", "")) or None

        # Skip oc/kubectl commands when OCP is not configured
        cmd_stripped = command.lstrip()
        if cmd_stripped.startswith(("oc ", "oc\t", "kubectl ", "kubectl\t")) and not os.getenv("OCP_API_URL"):
            return ToolResult(text="Skipped — OCP not configured (OCP_API_URL not set)")

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=working_dir,
            )

            output_parts = []
            if result.stdout:
                output_parts.append(f"stdout:\n{result.stdout[:4000]}")
            if result.stderr:
                output_parts.append(f"stderr:\n{result.stderr[:2000]}")
            output_parts.append(f"Exit code: {result.returncode}")

            return ToolResult(text="\n".join(output_parts))

        except subprocess.TimeoutExpired:
            return ToolResult(text=f"Command timed out after {timeout}s: {command}")
        except Exception as e:
            return ToolResult(text=f"Command failed: {e}")
