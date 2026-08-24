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
    BANDABLE,
    Agent,
    AgentGithub,
    Band,
    ChatCommand,
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


MEMBERSHIP = re.compile(r"\A(?P<value>[\w.-]+)\s+in\s+\{(?P<step>[\w-]+)\.(?P<output>[\w-]+)\}\Z")


def _parse_condition(raw: str) -> Condition:
    text = raw.strip()
    membership = MEMBERSHIP.match(text)
    if membership:
        return Condition(
            value=membership.group("value"),
            step_id=membership.group("step"),
            output=membership.group("output"),
        )
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
        elif key == "emits":
            step.emits = value.strip()
        elif key == "uses-compiler":
            step.uses_compiler = value.strip().lower() in ("true", "yes", "1")
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
    command_raw = gh_raw.get("command") or {}
    if command_raw:
        github.command = ChatCommand(
            name=str(command_raw.get("name", "") or ""),
            events=[str(e) for e in (command_raw.get("events") or ["issue_comment"])],
            roles=[str(r) for r in (command_raw.get("roles") or ["admin", "maintain", "write"])],
            associations=[
                str(a).upper()
                for a in (command_raw.get("associations") or ["OWNER", "MEMBER", "COLLABORATOR"])
            ],
            arguments=[str(a) for a in (command_raw.get("arguments") or [])],
            reaction=str(command_raw.get("reaction", "eyes") or "eyes"),
        )

    propose_raw = gh_raw.get("propose") or {}
    if propose_raw:
        github.propose = Propose(
            source=str(propose_raw.get("source", "") or ""),
            destination=str(propose_raw.get("destination", "") or ""),
            branch=str(propose_raw.get("branch", "pipeline/generated") or "pipeline/generated"),
            title=str(propose_raw.get("title", "Generated pipeline artifacts") or ""),
            labels=str(propose_raw.get("labels", "pipeline,generated") or ""),
            base=str(propose_raw.get("base", "") or ""),
            reuse_branch=bool(propose_raw.get("reuse-branch", False)),
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


def _band(value: Any, field_name: str, location: str) -> tuple[Any, Band | None]:
    """A scalar is fixed. A mapping carrying `default:` publishes how far a consumer may move it."""
    if not isinstance(value, dict):
        return value, None
    if "default" not in value:
        raise SpecError(
            f"{field_name} is a mapping with no `default:`",
            location=location,
            hint="write a scalar to fix the value, or `{ default: …, max: … }` to publish a band",
        )
    band = Band(
        default=value["default"],
        minimum=value.get("min"),
        maximum=value.get("max"),
        allow=[str(entry) for entry in (value.get("allow") or [])],
    )
    if band.allow and str(band.default) not in band.allow:
        raise SpecError(
            f"{field_name} has a default outside its own `allow:` list",
            location=location,
        )
    if not band.allow and band.minimum is None and band.maximum is None:
        raise SpecError(
            f"{field_name} publishes a band with no limits",
            location=location,
            hint="add `min:`/`max:` for a number or `allow:` for a choice — an unlimited band is a "
            "field a consumer may set to anything, which is not what publishing one means",
        )
    return band.default, band


def _refuse_capability_bands(meta: dict[str, Any], gh_raw: dict[str, Any], where: str) -> None:
    """A `default:` on a field no band may govern is somebody trying to publish one anyway.

    Caught here rather than left to fail later as a type error, because the error is the whole point:
    the answer to "can consumers raise max_tool_turns" is no, and it should say why.
    """
    for source in (meta, gh_raw):
        for name, value in source.items():
            if isinstance(value, dict) and "default" in value and name not in BANDABLE:
                raise SpecError(
                    f"{name} cannot be banded",
                    location=where,
                    hint="a band governs cost and latency, never capability. Bandable: "
                    + ", ".join(BANDABLE)
                    + f". {name} changes what this agent can do, so a consumer who needs a "
                    "different value needs a different agent",
                )


def parse_agent(src: SourceFile) -> Agent:
    meta = src.metadata
    gh_raw = meta.get("github", {}) or {}
    where = src.rel
    _refuse_capability_bands(meta, gh_raw, where)
    bands: dict[str, Band] = {}

    def banded(raw: Any, name: str) -> Any:
        value, band = _band(raw, name, where)
        if band is not None:
            bands[name] = band
        return value

    github = AgentGithub(
        engine=str(gh_raw.get("engine", "") or ""),
        model=str(gh_raw.get("model", "") or ""),
        max_ai_credits=banded(gh_raw.get("max-ai-credits"), "max-ai-credits"),
        network=_as_list(gh_raw.get("network")),
        safe_outputs=gh_raw.get("safe-outputs", {}) or {},
        timeout_minutes=banded(gh_raw.get("timeout-minutes"), "timeout-minutes"),
        raw=gh_raw.get("raw", {}) or {},
    )
    return Agent(
        name=str(meta.get("name") or Path(src.rel).stem),
        description=str(meta.get("description", "")),
        model=str(banded(meta.get("model", "") or "", "model")),
        provider=str(meta.get("provider", "") or ""),
        max_tool_turns=int(meta.get("max_tool_turns", 0) or 0),
        guardrails=_as_list(meta.get("guardrails")) or ["common"],
        skills=_as_list(meta.get("skills")),
        mcp=_as_list(meta.get("mcp")),
        body=src.body,
        github=github,
        src=src,
        bands=bands,
    )


def _scan_mode(raw: dict[str, Any], src: SourceFile) -> str:
    value = str(raw.get("scan-input", "") or "").strip().lower()
    if value and value not in ("warn", "block"):
        raise SpecError(
            f"enforce.scan-input is {value!r}",
            location=src.rel,
            hint="`warn` reports what it found; `block` fails the run. There is deliberately no "
            "third setting — a scanner that silently rewrote its input would be changing what a "
            "reviewer approved",
        )
    return value


def _ceiling(raw: dict[str, Any], key: str, src: SourceFile) -> int | None:
    """One ceiling from an `enforce:` block, refusing anything that is not a positive whole number.

    A ceiling of zero would forbid the thing outright while reading like an omission, and a value
    the parser silently coerced is a limit nobody can predict from the file.
    """
    if key not in raw or raw[key] is None:
        return None
    try:
        value = int(raw[key])
    except (TypeError, ValueError):
        raise SpecError(
            f"enforce.{key} is {raw[key]!r}, which is not a number",
            location=src.rel,
            hint="a ceiling is a positive whole number; remove the key to set no ceiling",
        ) from None
    if value < 1:
        raise SpecError(
            f"enforce.{key} is {value}, which forbids rather than limits",
            location=src.rel,
            hint="remove the key to set no ceiling; a ceiling of zero would read like an omission",
        )
    return value


def parse_fragment(src: SourceFile, kind: str) -> Fragment:
    enforce_raw = src.metadata.get("enforce", {}) or {}
    enforce = Enforce(
        permissions=str(enforce_raw.get("permissions", "") or ""),
        network=str(enforce_raw.get("network", "") or ""),
        deny_tools=_as_list(enforce_raw.get("deny-tools")),
        scan_input=_scan_mode(enforce_raw, src),
        max_turns=_ceiling(enforce_raw, "max-turns", src),
        max_ai_credits=_ceiling(enforce_raw, "max-ai-credits", src),
        per_run_ai_credits=_ceiling(enforce_raw, "per-run-ai-credits", src),
    )
    name = str(src.metadata.get("name") or Path(src.rel).stem)
    return Fragment(
        name=name,
        kind=kind,
        body=src.body,
        enforce=enforce,
        src=src,
        sealed=bool(src.metadata.get("sealed", False)),
    )


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
