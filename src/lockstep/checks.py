"""Quality and readiness checks.

Two questions, deliberately separated. `lint` asks whether the *spec* is well built — is every agent
evaluated, every script tested, is AI being spent on work a script should do. `doctor` asks whether
the *target* will accept it — are the secrets declared, the refs pinned, the permissions minimal.

A spec can be excellent and still un-deployable, and vice versa; conflating them makes both easier
to ignore.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .emit.agentic import ENGINE_BY_PROVIDER, UNMAPPED_PROVIDERS
from .emit.builtins import EXTERNAL_ACTIONS
from .emit.context import Pins
from .emit.validate import MAX_JOB_MINUTES
from .spec.model import Spec, StepKind

# Work an agent should never be doing: deterministic transformations cost tokens, vary run to run,
# and are the exact thing a script does better.
DETERMINISTIC_WORK = (
    "sort",
    "filter",
    "deduplicate",
    "dedupe",
    "convert format",
    "parse json",
    "validate schema",
)


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    severity: Severity
    code: str
    message: str
    location: str = ""
    hint: str = ""

    def render(self) -> str:
        head = f"{self.severity.value}: {self.code}: "
        head += f"{self.location} — {self.message}" if self.location else self.message
        if self.hint:
            head += f"\n       {self.hint}"
        return head


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, severity: Severity, code: str, message: str, *, location: str = "", hint: str = "") -> None:
        self.findings.append(Finding(severity, code, message, location, hint))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        if not self.findings:
            return "  no findings"
        return "\n".join(f"  {finding.render()}" for finding in self.findings)


# --- lint: is the spec well built? -----------------------------------------


def lint(spec: Spec) -> Report:
    report = Report()
    _check_agents_have_evals(spec, report)
    _check_scripts_have_tests(spec, report)
    _check_deterministic_first(spec, report)
    _check_foreach_context(spec, report)
    return report


def _check_agents_have_evals(spec: Spec, report: Report) -> None:
    for name, agent in sorted(spec.agents.items()):
        cases = spec.root / "evals" / name / "cases"
        if not cases.is_dir() or not any(cases.glob("*.json")):
            report.add(
                Severity.ERROR,
                "LNT001",
                f"agent {name!r} has no eval cases",
                location=agent.src.rel if agent.src else name,
                hint=f"add cases under evals/{name}/cases/ — an agent without evals cannot be "
                "changed safely, and the eval gate has nothing to gate on",
            )


def _check_scripts_have_tests(spec: Spec, report: Report) -> None:
    tests_dir = spec.root / "tests"
    tested = {p.name for p in tests_dir.rglob("test_*.py")} if tests_dir.is_dir() else set()
    seen: set[str] = set()
    for command in spec.commands.values():
        for step in command.steps:
            if step.kind is not StepKind.SCRIPT or step.target in seen:
                continue
            seen.add(step.target)
            stem = Path(step.target).stem.replace("-", "_")
            if f"test_{stem}.py" not in tested:
                report.add(
                    Severity.WARNING,
                    "LNT002",
                    f"script {step.target!r} has no unit test",
                    location=command.src.rel if command.src else command.name,
                    hint=f"add tests/test_{stem}.py — script steps run on every execution, so a "
                    "regression here is silent and permanent",
                )


def _check_deterministic_first(spec: Spec, report: Report) -> None:
    for name, agent in sorted(spec.agents.items()):
        body = agent.body.lower()
        matched = [phrase for phrase in DETERMINISTIC_WORK if phrase in body]
        if matched and agent.max_tool_turns == 0:
            report.add(
                Severity.WARNING,
                "LNT003",
                f"agent {name!r} appears to do deterministic work ({', '.join(matched)})",
                location=agent.src.rel if agent.src else name,
                hint="AI decides what to do; scripts do it. Deterministic transformations belong in "
                "a script step, which costs nothing and cannot vary between runs",
            )


def _check_foreach_context(spec: Spec, report: Report) -> None:
    for command in spec.commands.values():
        for step in command.steps:
            if step.kind is StepKind.AGENT and step.foreach and not step.parallel:
                report.add(
                    Severity.WARNING,
                    "LNT004",
                    f"foreach step {step.label!r} runs one item at a time",
                    location=f"{command.src.rel if command.src else command.name} step {step.number}",
                    hint="set `parallel:` — matrix legs are independent, and serialising them costs "
                    "wall-clock for nothing",
                )


# --- doctor: will the target accept it? ------------------------------------


def doctor(spec: Spec, root: Path) -> Report:
    report = Report()
    pins = Pins.load(root, spec)
    _check_pins(pins, report)
    _check_engines(spec, report)
    _check_budgets(spec, report)
    _check_secrets(spec, report)
    _check_mcp_allowlists(spec, report)
    _check_timeouts(spec, report)
    _check_extensions(spec, report)
    return report


def _check_pins(pins: Pins, report: Report) -> None:
    if not pins.actions_sha:
        report.add(
            Severity.ERROR,
            "DOC001",
            "capability actions are not pinned to a commit",
            hint="run `lockstep pin` — a floating tag can be moved under a pipeline that already "
            "passed review",
        )
    if not pins.exec_digest:
        report.add(
            Severity.ERROR,
            "DOC002",
            "the executor image is not pinned by digest",
            hint="run `lockstep pin`, or record capabilities.exec.digest in .pipeline/pins.lock",
        )
    for action in sorted(EXTERNAL_ACTIONS):
        if not pins.external.get(action):
            report.add(
                Severity.ERROR,
                "DOC012",
                f"external action {action!r} is not pinned",
                hint="run `lockstep pin` — a third-party action left on a tag can be replaced under "
                "a pipeline that already passed review",
            )
    if not pins.gh_aw_version:
        report.add(
            Severity.WARNING,
            "DOC003",
            "gh-aw is not pinned",
            hint="set capabilities.gh-aw in pipeline.yaml so lock files stay reproducible",
        )


def _check_engines(spec: Spec, report: Report) -> None:
    for name, agent in sorted(spec.agents.items()):
        provider = agent.provider or "vertex-claude"
        if agent.github.engine:
            continue
        if provider in UNMAPPED_PROVIDERS:
            report.add(
                Severity.ERROR,
                "DOC004",
                f"agent {name!r} uses provider {provider!r}, which has no engine on this target",
                location=agent.src.rel if agent.src else name,
                hint="this agent can only run on the local backend; set github.engine to compile it",
            )
        elif provider not in ENGINE_BY_PROVIDER:
            report.add(
                Severity.ERROR,
                "DOC005",
                f"agent {name!r} uses unknown provider {provider!r}",
                location=agent.src.rel if agent.src else name,
                hint=f"known providers: {', '.join(sorted(ENGINE_BY_PROVIDER))}",
            )


def _check_budgets(spec: Spec, report: Report) -> None:
    for name, agent in sorted(spec.agents.items()):
        if agent.github.max_ai_credits is None:
            report.add(
                Severity.ERROR,
                "DOC006",
                f"agent {name!r} has no credit budget",
                location=agent.src.rel if agent.src else name,
                hint="set github.max-ai-credits — an unbounded agent on a schedule is an unbounded bill",
            )
    if spec.manifest.per_run_ai_credits is None:
        report.add(
            Severity.WARNING,
            "DOC007",
            "no per-run credit budget",
            hint="set budgets.per_run_ai_credits in pipeline.yaml so a runaway run fails loudly",
        )


def _check_secrets(spec: Spec, report: Report) -> None:
    from .emit.profiles import ENV_REF

    for name, profile in sorted(spec.profiles.items()):
        declared = set(profile.github.secrets) | set(profile.github.vars)
        for key, raw in profile.values.items():
            match = ENV_REF.match(raw.strip())
            if match and match.group(1) not in declared:
                report.add(
                    Severity.ERROR,
                    "DOC008",
                    f"profile {name!r} reads {match.group(1)!r} for {key!r}, which it does not declare",
                    location=profile.src.rel if profile.src else name,
                    hint="add it to github.secrets or github.vars; the compiler will not guess where "
                    "a credential lives",
                )
        if profile.github.secrets and not profile.github.environment:
            report.add(
                Severity.WARNING,
                "DOC009",
                f"profile {name!r} uses secrets but declares no environment",
                location=profile.src.rel if profile.src else name,
                hint="a GitHub Environment scopes the secrets and can require approval before a run "
                "touches production",
            )


def _check_mcp_allowlists(spec: Spec, report: Report) -> None:
    for name, server in sorted(spec.mcp_servers.items()):
        if not server.tools:
            report.add(
                Severity.ERROR,
                "DOC010",
                f"MCP server {name!r} declares no tools",
                location="mcp/servers.json",
                hint="list its tools; the compiler turns that list into the gateway allow-list, and "
                "an empty list means the agent gets whatever the server offers",
            )


def _check_extensions(spec: Spec, report: Report) -> None:
    """An extension builtin is taken on trust; say so, and say how to verify it."""
    extensions = spec.manifest.extensions
    if extensions.builtins and not extensions.packages:
        report.add(
            Severity.ERROR,
            "DOC013",
            f"{len(extensions.builtins)} extension builtin(s) declared but no package provides them",
            hint="list the distributions under `extensions.packages` so a generated repository "
            "installs them; otherwise the workflow will fail with `No such command`",
        )
    if extensions.builtins:
        report.add(
            Severity.WARNING,
            "DOC014",
            f"extension builtins are not verifiable here: {', '.join(sorted(extensions.builtins))}",
            hint="run `pipeline-exec list-commands` in CI with the extension installed to prove "
            "they exist before a scheduled run finds out they do not",
        )


def _check_timeouts(spec: Spec, report: Report) -> None:
    for name, command in sorted(spec.commands.items()):
        timeout = command.github.timeout_minutes
        if timeout and timeout > MAX_JOB_MINUTES:
            report.add(
                Severity.ERROR,
                "DOC011",
                f"command {name!r} declares a {timeout}-minute timeout",
                location=command.src.rel if command.src else name,
                hint=f"a single job may not exceed {MAX_JOB_MINUTES} minutes; fan the work out instead",
            )
