"""Load a whole pipeline spec from a directory tree."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from .. import library
from ..errors import MissingDefinition, SpecError
from ..util.hashing import sha_file, short
from .model import (
    INHERITED_DIR,
    LOCKSTEP_DIR,
    Agent,
    Capabilities,
    Command,
    CommandUse,
    EvalConfig,
    Extensions,
    InheritsAuth,
    Manifest,
    Sandbox,
    SourceFile,
    Spec,
    StepKind,
    TargetConfig,
)
from .parse import (
    parse_agent,
    parse_command,
    parse_fragment,
    parse_mcp_servers,
    parse_profile,
    read_source,
)

MANIFEST_NAME = "pipeline.yaml"


def _load_dir(home: Path, subdir: str) -> list[SourceFile]:
    directory = home / subdir
    if not directory.is_dir():
        return []
    return [read_source(p, home) for p in sorted(directory.rglob("*.md"))]


def _fragment_name(src: SourceFile, subdir: str) -> str:
    """`skills/test/common.md` is referenced as `test/common`."""
    rel = Path(src.rel).relative_to(subdir)
    return str(rel.with_suffix("")).replace("\\", "/")


def find_home(root: Path) -> tuple[Path, bool]:
    """Where this repository keeps its pipeline.

    `.lockstep/` when it is there, the repository root otherwise. A repository that exists for the
    pipeline can keep everything at the root; one that already has source of its own puts the whole
    pipeline in one directory, and nothing about the spec changes either way.
    """
    if (root / LOCKSTEP_DIR / MANIFEST_NAME).is_file():
        return root / LOCKSTEP_DIR, True
    return root, False


def load_manifest(home: Path, root: Path) -> Manifest:
    path = home / MANIFEST_NAME
    if not path.is_file():
        raise MissingDefinition(
            f"no {MANIFEST_NAME} in {root} or {root / LOCKSTEP_DIR}",
            hint="run `lockstep init`, or add a pipeline.yaml — at the repository root for a "
            "dedicated pipeline repository, or in .lockstep/ to add one to an existing project",
        )
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    caps_raw = data.get("capabilities", {}) or {}
    target_raw = (data.get("targets", {}) or {}).get("github-agentic", {}) or {}
    sandbox_raw = target_raw.get("sandbox", {}) or {}
    budgets = data.get("budgets", {}) or {}
    evals_raw = data.get("evals", {}) or {}
    auth_raw = data.get("inherits-auth", {}) or {}
    extensions_raw = data.get("extensions", {}) or {}

    commands_raw = data.get("commands", {}) or {}
    uses = {
        str(name): CommandUse(
            source=str((entry or {}).get("from", "") or ""),
            add_guardrails=[str(v) for v in ((entry or {}).get("add-guardrails") or [])],
            add_skills=[str(v) for v in ((entry or {}).get("add-skills") or [])],
            agents={
                str(agent): dict(fields or {})
                for agent, fields in ((entry or {}).get("agents") or {}).items()
            },
        )
        for name, entry in commands_raw.items()
    }

    manifest = Manifest(
        spec_version=int(data.get("spec", 1) or 1),
        name=str(data.get("name", "") or root.name),
        inherits={str(k): str(v) for k, v in (data.get("inherits", {}) or {}).items()},
        uses=uses,
        capabilities=Capabilities(
            actions=str(caps_raw.get("actions", "") or ""),
            exec=str(caps_raw.get("exec", "") or ""),
            exec_image=str(caps_raw.get("exec-image", "") or ""),
            compiler=str(caps_raw.get("compiler", "") or ""),
            gh_aw=str(caps_raw.get("gh-aw", "") or ""),
        ),
        target=TargetConfig(
            out=str(target_raw.get("out", ".github/workflows")),
            fuse_script_steps=bool(target_raw.get("fuse-script-steps", True)),
            default_runs_on=str(target_raw.get("default-runs-on", "ubuntu-24.04")),
            shard_threshold=int(target_raw.get("shard-threshold", 20)),
            profiles=[str(p) for p in (target_raw.get("profiles", []) or [])],
            watch=[str(p) for p in (target_raw.get("watch", []) or [])],
            sandbox=Sandbox(
                capabilities=[str(c) for c in (sandbox_raw.get("capabilities") or [])],
                memory=str(sandbox_raw.get("memory", "") or ""),
                cpus=str(sandbox_raw.get("cpus", "") or ""),
                pids=int(sandbox_raw["pids"]) if sandbox_raw.get("pids") is not None else None,
                user=str(sandbox_raw.get("user", "") or ""),
            ),
        ),
        per_run_ai_credits=budgets.get("per_run_ai_credits"),
        per_agent_daily_ai_credits=budgets.get("per_agent_daily_ai_credits"),
        evals=EvalConfig(
            judge=str(evals_raw.get("judge", "") or ""),
            min_pass_rate=(
                float(evals_raw["min-pass-rate"]) if evals_raw.get("min-pass-rate") is not None else None
            ),
            min_score=(float(evals_raw["min-score"]) if evals_raw.get("min-score") is not None else None),
            on_prompt_change=bool(evals_raw.get("on-prompt-change", True)),
        ),
        inherits_auth=InheritsAuth(
            token=str(auth_raw.get("token", "") or ""),
            app_id=str(auth_raw.get("app-id", "") or ""),
            private_key=str(auth_raw.get("private-key", "") or ""),
        ),
        commands=data.get("commands", {}) or {},
        extensions=Extensions(
            builtins=[str(name) for name in (extensions_raw.get("builtins") or [])],
            packages=[str(name) for name in (extensions_raw.get("packages") or [])],
        ),
        src=SourceFile(
            path=path,
            rel=str(path.relative_to(root)),
            sha=short(sha_file(path)),
            metadata=data,
            body="",
        ),
    )
    if manifest.spec_version != 1:
        raise SpecError(
            f"unsupported spec version {manifest.spec_version}",
            hint="this compiler understands `spec: 1`",
        )
    return manifest


def load_manifest_only(root: Path) -> Spec:
    """The manifest, with no definitions loaded.

    `lockstep pin` needs this: it resolves the refs that `lockstep fetch` then uses, so it cannot
    require the fetched trees to already be there.
    """
    root = root.resolve()
    home, nested = find_home(root)
    return Spec(root=root, manifest=load_manifest(home, root), in_lockstep_dir=nested)


def load_spec(root: Path) -> Spec:
    """Read every definition under `root`, plus everything it inherits, into a validated Spec."""
    root = root.resolve()
    home, nested = find_home(root)
    manifest = load_manifest(home, root)
    spec = Spec(root=root, manifest=manifest, in_lockstep_dir=nested)

    # Inherited definitions load first so a local file of the same name is a collision the loader can
    # see and refuse, rather than an overwrite whose direction depends on dictionary order.
    # Declaration order, not alphabetical: `inherits:` is the authority order for the standards
    # every agent receives, and a repository should be able to decide it rather than discover it.
    for alias in manifest.inherits:
        _load_definitions(spec, home / INHERITED_DIR / alias, alias=alias, home=home)
    _load_definitions(spec, home, alias="", home=home)

    _resolve_uses(spec)
    _validate(spec)
    return spec


def _load_definitions(spec: Spec, directory: Path, *, alias: str, home: Path) -> None:
    """Load one definition tree into the spec, namespaced by `alias` when it is an inherited one."""
    if alias and not directory.is_dir():
        source = spec.manifest.inherits[alias]
        raise MissingDefinition(
            f"{alias!r} is inherited from {source} but has not been fetched",
            location=str(directory.relative_to(home)),
            hint="run `lockstep fetch` — inherited definitions are resolved state, like a virtualenv, "
            "so they are not committed",
        )

    def scoped(name: str) -> str:
        return f"{alias}/{name}" if alias else name

    def stamped(src: SourceFile) -> SourceFile:
        """Provenance says which upstream a definition arrived from, and at what content.

        Without the alias, a generated file's `sources:` line claims the consumer wrote a guardrail
        it only inherited — and the diff that matters when a standard changes is unattributable.
        """
        return replace(src, rel=f"{alias}:{src.rel}") if alias else src

    for src in _load_dir(directory, "commands"):
        command = parse_command(stamped(src))
        command.name = scoped(command.name)
        if alias:
            _scope_command(command, alias)
        spec.commands[command.name] = command

    for src in _load_dir(directory, "agents"):
        agent = parse_agent(stamped(src))
        agent.name = scoped(agent.name)
        agent.inherited_from = alias
        # A definition resolves its references inside its own tree. Cross-alias references are not a
        # thing in this version: an inherited pipeline is self-contained, and anything organization-
        # wide reaches it by being sealed rather than by being named.
        agent.guardrails = [scoped(name) for name in agent.guardrails]
        agent.skills = [scoped(name) for name in agent.skills]
        spec.agents[agent.name] = agent

    for src in _load_dir(directory, "profiles"):
        # Profiles are the one thing never inherited: they hold one deployment's secrets and choose
        # its contexts, and neither is knowable upstream.
        if alias:
            continue
        profile = parse_profile(src)
        spec.profiles[profile.name] = profile

    for subdir, bucket in (
        ("guardrails", spec.guardrails),
        ("skills", spec.skills),
        ("contexts", spec.contexts),
    ):
        for src in _load_dir(directory, subdir):
            fragment = parse_fragment(stamped(src), subdir.rstrip("s"))
            fragment.name = scoped(_fragment_name(src, subdir))
            fragment.inherited_from = alias
            if not alias:
                # Sealing your own guardrail seals it against yourself, which means nothing.
                fragment.sealed = False
            if fragment.name in bucket:
                raise SpecError(
                    f"{fragment.kind} {fragment.name!r} is defined twice",
                    location=src.rel,
                    hint="a local definition cannot take the name of an inherited one; rename yours",
                )
            bucket[fragment.name] = fragment

    if alias:
        _merge_extensions(spec, directory, alias, home)
        return

    mcp_path = directory / "mcp" / "servers.json"
    if mcp_path.is_file():
        spec.mcp_servers = parse_mcp_servers(json.loads(mcp_path.read_text(encoding="utf-8")))


def _scope_command(command: Command, alias: str) -> None:
    """Rewrite an inherited command's references into its own namespace.

    Its scripts live in the fetched tree rather than beside the consumer's own, and the agents and
    sub-commands it names are the ones its own repository defined — not whatever a consumer happens
    to have called the same thing.
    """
    command.guardrails = [f"{alias}/{name}" for name in command.guardrails]
    for step in command.steps:
        if step.kind is StepKind.SCRIPT:
            step.target = f"{INHERITED_DIR}/{alias}/{step.target}"
        elif step.kind in (StepKind.AGENT, StepKind.COMMAND):
            step.target = f"{alias}/{step.target}"


def _merge_extensions(spec: Spec, directory: Path, alias: str, home: Path) -> None:
    """An inherited pipeline brings the builtins it needs, and the package that provides them."""
    inherited = load_manifest(directory, directory).extensions
    for name in inherited.builtins:
        if name not in spec.manifest.extensions.builtins:
            spec.manifest.extensions.builtins.append(name)
    for package in inherited.packages:
        # `name @ file://./extensions` is relative to the tree that declared it.
        rerooted = package.replace("file://./", f"file://./{INHERITED_DIR}/{alias}/")
        if rerooted not in spec.manifest.extensions.packages:
            spec.manifest.extensions.packages.append(rerooted)


def _resolve_uses(spec: Spec) -> None:
    """Bind each `commands:` entry that names an inherited command to the definition it wants."""
    for name, use in spec.manifest.uses.items():
        if not use.source:
            continue
        command = _find_inherited(spec, name, use.source)
        bound = replace(command, name=name)
        bound.guardrails = [*command.guardrails, *use.add_guardrails]
        spec.commands[name] = bound
        reachable = {step.target for step in bound.steps if step.kind is StepKind.AGENT}
        for target in sorted(reachable):
            agent = spec.agents.get(target)
            if agent is None:
                continue
            agent.guardrails = [*agent.guardrails, *use.add_guardrails]
            agent.skills = [*agent.skills, *use.add_skills]

        for agent_name, fields in use.agents.items():
            target = agent_name if agent_name in spec.agents else f"{use.source}/{agent_name}"
            agent = spec.agents.get(target)
            if agent is None or target not in reachable:
                raise MissingDefinition(
                    f"command {name!r} tunes agent {agent_name!r}, which it does not run",
                    hint=f"agents in this command: {', '.join(sorted(reachable)) or '(none)'}",
                )
            _tune(agent, fields, where=f"pipeline.yaml commands.{name}.agents.{agent_name}")


def _tune(agent: Agent, fields: dict[str, Any], *, where: str) -> None:
    """Move an inherited agent's dials, within the limits the publishing repository declared."""
    for field_name, value in fields.items():
        band = agent.bands.get(field_name)
        if band is None:
            raise SpecError(
                f"{field_name} is fixed by {agent.inherited_from or 'this pipeline'}",
                location=where,
                hint=f"agent {agent.name!r} publishes no band for it. Tunable here: "
                + (", ".join(sorted(agent.bands)) or "(nothing)"),
            )
        if not band.permits(value):
            raise SpecError(
                f"{field_name}: {value!r} is outside the band {band.describe()}",
                location=where,
                hint=f"{agent.inherited_from or 'the pipeline'} publishes that range for "
                f"{agent.name!r}; ask them to widen it rather than working around it",
            )
        # Recorded before it is applied, so the compile manifest can report what a fleet has tuned
        # even where somebody set a field back to its default.
        if agent.tuned.get(field_name, value) != value:
            raise SpecError(
                f"{field_name} is tuned twice for agent {agent.name!r}, to different values",
                location=where,
                hint="two commands cannot run one agent configured differently; the compiler already "
                "refuses an agent that resolves to different prompt layers, for the same reason",
            )
        agent.tuned[field_name] = value
        _apply(agent, field_name, value)


def _apply(agent: Agent, field_name: str, value: Any) -> None:
    if field_name == "max-ai-credits":
        agent.github.max_ai_credits = int(value)
    elif field_name == "timeout-minutes":
        agent.github.timeout_minutes = int(value)
    elif field_name == "model":
        agent.model = str(value)


def _find_inherited(spec: Spec, name: str, source: str) -> Command:
    if "/" in source:
        found = spec.commands.get(source)
        if found is None:
            raise MissingDefinition(
                f"command {name!r} is `from: {source}`, which no inherited pipeline defines",
                hint=f"known: {', '.join(sorted(n for n in spec.commands if '/' in n)) or '(none)'}",
            )
        return found

    candidates = sorted(n for n in spec.commands if n.startswith(f"{source}/"))
    if not candidates:
        raise MissingDefinition(
            f"command {name!r} is `from: {source}`, which is not an alias in `inherits:`",
            hint=f"declared aliases: {', '.join(sorted(spec.manifest.inherits)) or '(none)'}",
        )
    if len(candidates) > 1:
        raise SpecError(
            f"`from: {source}` is ambiguous — that pipeline defines {len(candidates)} commands",
            hint=f"name one: {', '.join(candidates)}",
        )
    return spec.commands[candidates[0]]


def _validate(spec: Spec) -> None:
    """Cross-reference checks that must fail at compile time, not at 2am in a scheduled run."""
    for name in sorted(spec.guardrails):
        if name not in library.guardrails():
            continue
        # Silently dropping the file would be the worse outcome: an author would see their guardrail
        # in the repository and not in the prompt.
        fragment = spec.guardrails[name]
        raise SpecError(
            f"guardrail {name!r} has the same name as one the compiler ships",
            location=fragment.src.rel if fragment.src else "",
            hint="the shipped baseline is inlined into every agent already; rename yours",
        )

    for command in spec.commands.values():
        for step in command.steps:
            loc = f"{command.src.rel if command.src else command.name} step {step.number}"
            if step.kind.value == "agent" and step.target not in spec.agents:
                raise MissingDefinition(
                    f"step references unknown agent {step.target!r}",
                    location=loc,
                    hint=f"known agents: {', '.join(sorted(spec.agents)) or '(none)'}",
                )
            if step.kind.value == "command" and step.target not in spec.commands:
                raise MissingDefinition(
                    f"step references unknown command {step.target!r}",
                    location=loc,
                    hint=f"known commands: {', '.join(sorted(spec.commands)) or '(none)'}",
                )
            if step.kind.value == "script":
                script = spec.home / step.target
                if not script.exists():
                    raise MissingDefinition(
                        f"script {step.target!r} does not exist",
                        location=loc,
                    )

    for agent in spec.agents.values():
        loc = agent.src.rel if agent.src else agent.name
        for name in agent.guardrails:
            if name not in spec.guardrails:
                raise MissingDefinition(f"unknown guardrail {name!r}", location=loc)
        for name in agent.skills:
            if name not in spec.skills and name not in library.skills():
                raise MissingDefinition(
                    f"unknown skill {name!r}",
                    location=loc,
                    hint=f"shipped skills: {', '.join(sorted(library.skills()))}",
                )
        for name in agent.mcp:
            if name not in spec.mcp_servers:
                raise MissingDefinition(f"unknown MCP server {name!r}", location=loc)

    for profile in spec.profiles.values():
        loc = profile.src.rel if profile.src else profile.name
        for name in profile.exclude_guardrails:
            excluded = spec.guardrails.get(name)
            if excluded is not None and excluded.sealed:
                raise SpecError(
                    f"guardrail {name!r} is sealed and cannot be excluded",
                    location=loc,
                    hint=f"it is a standard {excluded.inherited_from!r} publishes, not a default; "
                    "take it up with whoever owns that repository",
                )
        for name in profile.contexts:
            if name not in spec.contexts:
                raise MissingDefinition(f"unknown context {name!r}", location=loc)
