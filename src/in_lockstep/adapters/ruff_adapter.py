"""Validate, over ruff."""

from __future__ import annotations

import json
from typing import ClassVar

from ..core.outcome import Cost, Finding, Outcome, Severity, Status
from ..core.types import Resolution, Validate, ValidationFinding, ValidationReport
from ..core.verbs import Capability, Verb
from . import tooling
from .sandbox import Runner, Sandbox

__all__ = ["RuffValidate", "Validate"]

#: Where ruff looks when the request names nothing. A typed tuple rather than a literal in the
#: argv, because the literal used to be `(".")` -- a parenthesised string, not a tuple -- and it
#: unpacked into one argument only because `.` is one character long. `("./src")` in the same
#: position would have reached ruff as `. / s r c`: five paths nobody wrote, and a run that stayed
#: green because `.` was still among them (#246). With the annotation, a string here is refused by
#: mypy before it is ever unpacked.
DEFAULT_PATHS: tuple[str, ...] = (".",)


class RuffValidate:
    verb: ClassVar[Verb] = Verb.VALIDATE
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.READS_REPO})
    #: `Validate(fix=True)` means something here: ruff repairs what it can and reports the rest.
    #: Declared rather than assumed, because a strategy runs the fixer over a model's staged files
    #: before spending a turn on them, and "run this linter with --fix" is not a thing that can be
    #: guessed about an arbitrary `CommandValidate` -- a repository's `make lint` may take no such
    #: flag, or may take one that does something else entirely.
    fixes: ClassVar[bool] = True
    #: ruff is a tool, not a target: it lints exactly the paths it is given, so a run can be
    #: scoped to what a session staged rather than to the whole tree.
    takes_paths: ClassVar[bool] = True

    def __init__(
        self,
        select: list[str] | None = None,
        cwd: str | None = None,
        sandbox: Runner | None = None,
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
        # `inp.root` (a materialised worktree) wins over the bound `cwd` wins over the repo's
        # root, the same precedence `PytestTest` uses and for the same caller: a session checking
        # what it has staged rather than what is committed.
        cwd = self.cwd or repo_root
        paths = tuple(inp.paths or DEFAULT_PATHS)
        if inp.root:
            top = getattr(getattr(ctx, "repo", None), "root", None)
            cwd, package = tooling.within(inp.root, self.cwd, top)
            paths = tooling.rebase(paths, package)
        paths = tooling.relative(paths, cwd)
        cmd = [resolved.path, "check", "--output-format", "json", *paths]
        rules = [*self.select, *inp.rules]
        if rules:
            cmd += ["--select", ",".join(rules)]
        if inp.fix:
            cmd.append("--fix")

        result = await self.sandbox.run(cmd, cwd=cwd)
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
