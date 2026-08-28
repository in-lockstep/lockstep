"""Validate, over ruff."""

from __future__ import annotations

import asyncio
import json
from typing import ClassVar

from ..core.outcome import Cost, Finding, Outcome, Severity, Status
from ..core.types import ValidateSpec, ValidationFinding, ValidationReport
from ..core.verbs import Capability, Verb


class Validate:
    """The verb interface."""


class RuffValidate:
    verb: ClassVar[Verb] = Verb.VALIDATE
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.READS_REPO})

    def __init__(self, select: list[str] | None = None, cwd: str | None = None) -> None:
        self.select = select or []
        self.cwd = cwd

    async def invoke(self, ctx: object, inp: ValidateSpec) -> Outcome[ValidationReport]:
        cmd = ["ruff", "check", "--output-format", "json", *(inp.paths or ("."))]
        rules = [*self.select, *inp.rules]
        if rules:
            cmd += ["--select", ",".join(rules)]
        if inp.fix:
            cmd.append("--fix")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.cwd or getattr(getattr(ctx, "repo", None), "root", None),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
        except FileNotFoundError:
            return Outcome.errored("ruff is not installed")

        raw = stdout.decode(errors="replace").strip()
        try:
            entries = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            return Outcome.errored(f"ruff produced unparseable output: {raw[:200]}")

        findings = tuple(
            ValidationFinding(
                rule=str(e.get("code") or ""),
                message=str(e.get("message") or ""),
                path=str(e.get("filename") or ""),
                line=(e.get("location") or {}).get("row"),
            )
            for e in entries
        )
        report = ValidationReport(findings=findings)

        return Outcome(
            status=Status.SUCCEEDED if report.clean else Status.FAILED,
            value=report,
            findings=tuple(
                Finding(
                    id=f"validate.{f.rule.lower()}" if f.rule else "validate.finding",
                    message=f.message,
                    severity=Severity.ERROR,
                    path=f.path,
                    line=f.line,
                    blocking=True,
                )
                for f in findings[:25]
            ),
            cost=Cost(),
        )
