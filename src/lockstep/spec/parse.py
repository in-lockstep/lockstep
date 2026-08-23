"""Markdown definition parsing.

The step grammar is a faithful port of the pipeline-framework runtime's parser, extended with the
keys the GitHub target needs (`id`, `targets`, `min-success-rate`, `job-boundary`). Keeping the
grammar identical is what lets the same command file drive both backends.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from ..errors import BadStepSyntax, SpecError
from ..util.hashing import sha_file, short
from ..util.text import slug, uniquify
from .model import (
    Agent,
    AgentGithub,
    Command,
    CommandGithub,
    Condition,
    Enforce,
    Foreach,
    Fragment,
    McpServer,
    Parameter,
    Profile,
    ProfileDeploy,
    ProfileGithub,
    ProfileReports,
    Propose,
    SourceFile,
    Step,
    StepKind,
)

FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?", re.DOTALL)

# `1. **Label** -> kind: target`   (both the unicode arrow and ASCII `->` are accepted)
STEP = re.compile(
    r"(\d+)\.\s+\*\*(.+?)\*\*\s*(?:→|->)\s*(agent|script|builtin|command):\s*(.+)",
    re.IGNORECASE,
)
SUBKEY = re.compile(r"^\s+-\s+([\w][\w-]*):\s*(.*)$")
CONDITION = re.compile(r"^\s*\(if\s+(.+?)\)\s*$", re.IGNORECASE)
FOREACH = re.compile(r"^\s*(\w+)\s+in\s+(.+?)\s*$", re.IGNORECASE)

HOOK_KEYS = {"pre", "post", "on-failure", "on_failure"}
STRUCTURAL_KEYS = {
    "foreach",
    "foreach-key",
    "parallel",
    "input",
    "output",
    "context-files",
    "id",
    "targets",
    "min-success-rate",
    "job-boundary",
    "max-iterations",
    "fingerprint",
} | HOOK_KEYS


def read_source(path: Path, root: Path) -> SourceFile:
    """Read a definition file into frontmatter + body, stamped with its content hash."""
    raw = path.read_text(encoding="utf-8")
    match = FRONTMATTER.match(raw)
    metadata: dict[str, Any] = {}
    body = raw
    if match:
        loaded = yaml.safe_load(match.group(1)) or {}
        if not isinstance(loaded, dict):
            raise SpecError(
                "frontmatter must be a YAML mapping",
                location=str(path.relative_to(root)),
            )
        metadata = loaded
        body = raw[match.end() :]
    return SourceFile(
        path=path,
        rel=str(path.relative_to(root)),
        sha=short(sha_file(path)),
        metadata=metadata,
        body=body.strip("\n"),
    )


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _parse_condition(raw: str) -> Condition:
    text = raw.strip()
    negated = False
    if text.lower().startswith("not "):
        negated = True
        text = text[4:].strip()
    return Condition(flag=text.strip(), negated=negated)


def _parse_foreach(raw: str, key_field: str) -> Foreach:
    match = FOREACH.match(raw)
    if not match:
        raise BadStepSyntax(
            f"cannot parse foreach expression {raw!r}",
            hint="expected `foreach: <var> in <path-to-json-array>`",
        )
    return Foreach(var=match.group(1), source=match.group(2).strip(), key_field=key_field)


def parse_steps(body: str, *, location: str) -> list[Step]:
    """Parse the numbered step list out of a command body."""
    steps: list[Step] = []
    current: Step | None = None
    pending: dict[str, str] = {}

    def finish() -> None:
        nonlocal current, pending
        if current is None:
            return
        _apply_subkeys(current, pending, location)
        steps.append(current)
        current = None
        pending = {}

    for line in body.splitlines():
        stripped = line.strip()
        step_match = STEP.match(stripped)
        if step_match:
            finish()
            current = Step(
                number=int(step_match.group(1)),
                label=step_match.group(2).strip(),
                kind=StepKind(step_match.group(3).strip().lower()),
                target=step_match.group(4).strip(),
            )
            inline = CONDITION.search(stripped)
            if inline:
                current.condition = _parse_condition(inline.group(1))
                current.target = CONDITION.sub("", current.target).strip()
            continue

        if current is None:
            continue

        cond_match = CONDITION.match(stripped)
        if cond_match:
            current.condition = _parse_condition(cond_match.group(1))
            continue

        sub_match = SUBKEY.match(line)
        if sub_match:
            pending[sub_match.group(1).lower()] = sub_match.group(2).strip()

    finish()
    _assign_ids(steps)
    return steps


def _apply_subkeys(step: Step, pending: dict[str, str], location: str) -> None:
    key_field = pending.pop("foreach-key", "key")
    for key, value in pending.items():
        if key == "foreach":
            step.foreach = _parse_foreach(value, key_field)
        elif key == "parallel":
            step.parallel = int(value) if value.isdigit() else 0
        elif key == "input":
            step.input = value
        elif key == "output":
            step.output = value
        elif key == "context-files":
            step.context_files = [p.strip() for p in value.split(",") if p.strip()]
        elif key == "id":
            step.id = slug(value)
            step.explicit_id = True
        elif key == "targets":
            step.targets = [t.strip() for t in value.strip("[]").split(",") if t.strip()]
        elif key == "min-success-rate":
            try:
                step.min_success_rate = float(value)
            except ValueError as exc:
                raise BadStepSyntax(
                    f"min-success-rate must be a number, got {value!r}",
                    location=f"{location} step {step.number}",
                ) from exc
        elif key == "fingerprint":
            step.fingerprint = value
        elif key == "max-iterations":
            step.max_iterations = int(value) if value.isdigit() else 0
        elif key == "job-boundary":
            step.job_boundary = value.strip().lower() in ("true", "yes", "1")
        elif key == "pre":
            step.pre = value
        elif key == "post":
            step.post = value
        elif key in ("on-failure", "on_failure"):
            step.on_failure = value
        else:
            step.args[key] = value


def _assign_ids(steps: list[Step]) -> None:
    """Derive stable job/step ids. Explicit `id:` wins so display-name edits never move an anchor."""
    taken: set[str] = set()
    for step in steps:
        if step.explicit_id:
            if step.id in taken:
                raise SpecError(f"duplicate step id {step.id!r}")
            taken.add(step.id)
    for step in steps:
        if not step.explicit_id:
            step.id = uniquify(slug(step.label), taken)


def parse_command(src: SourceFile) -> Command:
    meta = src.metadata
    params: list[Parameter] = []
    for entry in meta.get("parameters", []) or []:
        if isinstance(entry, dict):
            default = entry.get("default")
            params.append(
                Parameter(
                    name=str(entry.get("name", "")),
                    description=str(entry.get("description", "")),
                    default=None if default is None else str(default),
                )
            )
    gh_raw = meta.get("github", {}) or {}
    github = CommandGithub(
        triggers=gh_raw.get("triggers", {}) or {},
        runs_on=str(gh_raw.get("runs-on", "") or ""),
        timeout_minutes=gh_raw.get("timeout-minutes"),
        max_iterations=int(gh_raw.get("max-iterations", 1) or 1),
        concurrency=gh_raw.get("concurrency"),
        converged_from=str(gh_raw.get("converged-from", "") or ""),
        raw=gh_raw.get("raw", {}) or {},
    )
    propose_raw = gh_raw.get("propose") or {}
    if propose_raw:
        github.propose = Propose(
            source=str(propose_raw.get("source", "") or ""),
            destination=str(propose_raw.get("destination", "") or ""),
            branch=str(propose_raw.get("branch", "pipeline/generated") or "pipeline/generated"),
            title=str(propose_raw.get("title", "Generated pipeline artifacts") or ""),
            labels=str(propose_raw.get("labels", "pipeline,generated") or ""),
        )

    state = str(meta.get("state", "")).lower()
    return Command(
        name=str(meta.get("name") or Path(src.rel).stem),
        description=str(meta.get("description", "")),
        parameters=params,
        guardrails=_as_list(meta.get("guardrails")),
        state=state if state in ("true", "keep") else "",
        steps=parse_steps(src.body, location=src.rel),
        github=github,
        src=src,
    )


def parse_agent(src: SourceFile) -> Agent:
    meta = src.metadata
    gh_raw = meta.get("github", {}) or {}
    github = AgentGithub(
        engine=str(gh_raw.get("engine", "") or ""),
        model=str(gh_raw.get("model", "") or ""),
        max_ai_credits=gh_raw.get("max-ai-credits"),
        network=_as_list(gh_raw.get("network")),
        safe_outputs=gh_raw.get("safe-outputs", {}) or {},
        timeout_minutes=gh_raw.get("timeout-minutes"),
        raw=gh_raw.get("raw", {}) or {},
    )
    return Agent(
        name=str(meta.get("name") or Path(src.rel).stem),
        description=str(meta.get("description", "")),
        model=str(meta.get("model", "") or ""),
        provider=str(meta.get("provider", "") or ""),
        max_tool_turns=int(meta.get("max_tool_turns", 0) or 0),
        guardrails=_as_list(meta.get("guardrails")) or ["common"],
        skills=_as_list(meta.get("skills")),
        mcp=_as_list(meta.get("mcp")),
        body=src.body,
        github=github,
        src=src,
    )


def parse_fragment(src: SourceFile, kind: str) -> Fragment:
    enforce_raw = src.metadata.get("enforce", {}) or {}
    enforce = Enforce(
        permissions=str(enforce_raw.get("permissions", "") or ""),
        network=str(enforce_raw.get("network", "") or ""),
        deny_tools=_as_list(enforce_raw.get("deny-tools")),
    )
    name = str(src.metadata.get("name") or Path(src.rel).stem)
    return Fragment(name=name, kind=kind, body=src.body, enforce=enforce, src=src)


def parse_profile(src: SourceFile) -> Profile:
    meta = src.metadata
    values: dict[str, str] = {}
    for line in src.body.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()

    gh_raw = meta.get("github", {}) or {}
    deploy_raw = gh_raw.get("deploy", {}) or {}
    reports_raw = gh_raw.get("reports", {}) or {}
    github = ProfileGithub(
        environment=str(gh_raw.get("environment", "") or ""),
        secrets=_as_list(gh_raw.get("secrets")),
        vars=_as_list(gh_raw.get("vars")),
        deploy=ProfileDeploy(
            mode=str(deploy_raw.get("mode", "external") or "external"),
            services=deploy_raw.get("services", {}) or {},
            cli=deploy_raw.get("cli", {}) or {},
            healthcheck=str(deploy_raw.get("healthcheck", "") or ""),
        ),
        reports=ProfileReports(
            branch=str(reports_raw.get("branch", "") or ""),
            path=str(reports_raw.get("path", "runs") or "runs"),
            retain=int(reports_raw.get("retain", 90) or 90),
        ),
    )
    return Profile(
        name=str(meta.get("name") or Path(src.rel).stem),
        description=str(meta.get("description", "")),
        contexts=_as_list(meta.get("contexts")),
        exclude_guardrails=_as_list(meta.get("exclude_guardrails")),
        values=values,
        github=github,
        src=src,
    )


def parse_mcp_servers(data: dict[str, Any]) -> dict[str, McpServer]:
    servers: dict[str, McpServer] = {}
    for name, entry in (data.get("servers") or {}).items():
        servers[name] = McpServer(
            name=name,
            command=str(entry.get("command", "") or ""),
            args=[str(a) for a in entry.get("args", []) or []],
            env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
            tools=[str(t) for t in entry.get("tools", []) or []],
            container=str(entry.get("container", "") or ""),
            inline=bool(entry.get("inline", False)),
        )
    return servers
