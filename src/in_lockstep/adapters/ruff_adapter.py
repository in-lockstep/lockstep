"""Validate, over ruff."""

from __future__ import annotations

import json
from typing import ClassVar

from ..core.outcome import Cost, Finding, Outcome, Severity, Status
from ..core.types import Resolution, Validate, ValidationFinding, ValidationReport
from ..core.verbs import Capability, Verb
from . import tooling
from .sandbox import Sandbox

__all__ = ["RuffValidate", "Validate"]


class RuffValidate:
    verb: ClassVar[Verb] = Verb.VALIDATE
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.READS_REPO})

    def __init__(
        self,
        select: list[str] | None = None,
        cwd: str | None = None,
        sandbox: Sandbox | None = None,
    ) -> None:
        self.select = select or []
        self.cwd = cwd
        # ruff loads repository configuration, so this runs out of process too.
        self.sandbox = sandbox or Sandbox()

    def locations(self, root: str) -> tuple[Resolution, ...]:
        """Where ruff will run from, for `ls` and `doctor`: the repository's, not this process's."""
        return (self._ruff(self.cwd or root),)

    def _ruff(self, root: str | None) -> Resolution:
        # Beside the interpreter the suite runs on, before PATH: a repository whose environment is
        # not at `.venv` still has ruff next to its python, and PATH may hold a different one.
        python = tooling.interpreter(root, self.sandbox)
        return tooling.binary("ruff", root, self.sandbox, beside=python.path, probe=("--version",))

    async def invoke(self, ctx: object, inp: Validate) -> Outcome[ValidationReport]:
        repo_root = self.cwd or getattr(getattr(ctx, "repo", None), "root", None)
        resolved = self._ruff(repo_root)
        if resolved.path is None:
            return Outcome.errored(
                f"ruff is not installed for the repository; looked for {', '.join(resolved.tried)}"
            )
        cmd = [resolved.path, "check", "--output-format", "json", *(inp.paths or ("."))]
        rules = [*self.select, *inp.rules]
        if rules:
            cmd += ["--select", ",".join(rules)]
        if inp.fix:
            cmd.append("--fix")

        result = await self.sandbox.run(
            cmd, cwd=self.cwd or getattr(getattr(ctx, "repo", None), "root", None)
        )
        if result.exit_code == 127:
            return Outcome.errored(f"ruff at {resolved.path} could not be run ({resolved.how})")

        raw = result.stdout.strip()
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
