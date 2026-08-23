"""The compile plan: spec + overlays + pins -> a complete set of generated files.

`compile_spec` is a pure function of files on disk. Nothing here reads the environment, the clock,
or the network — that is precisely what makes `lockstep compile --check` a meaningful drift gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..errors import EmitError
from ..spec.load import load_spec
from ..spec.model import Agent, Command, Profile, Spec
from ..util import yamlio
from ..util.hashing import sha_text, short
from ..util.text import slug
from .agentic import (
    AgentArtifact,
    build_agent,
    lock_filename,
    render_agent,
    verify_enforcement,
    workflow_filename,
)
from .context import EmitContext, Pins
from .fragments import emit_fragments, resolve_layers
from .orchestrator import WorkflowResult, emit_command, normalize
from .overlay import Overlay, apply_mapping_ops, apply_prompt_ops, load_overlays
from .validate import validate_workflow

MANIFEST_PATH = ".pipeline/compile-manifest.json"
SECRETS_DOC = "SECRETS.md"


@dataclass
class CompilePlan:
    """Everything the compiler decided, ready to be written or compared."""

    root: Path
    files: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    sources: dict[str, list[str]] = field(default_factory=dict)
    summaries: list[str] = field(default_factory=list)

    def add(self, path: str, content: str, *, sources: list[str] | None = None) -> None:
        self.files[path] = content
        self.sources[path] = sources or []


def compile_spec(root: Path) -> CompilePlan:
    spec = load_spec(root)
    pins = Pins.load(root, spec)
    overlays = load_overlays(root)
    plan = CompilePlan(root=root)

    profiles = spec.compiled_profiles()
    if not profiles:
        raise EmitError(
            "no profiles to compile",
            hint="add a profile under profiles/, or list one in targets.github-agentic.profiles",
        )
    multi = len(profiles) > 1
    commands = _commands_to_compile(spec)

    agentic_count = 0
    deterministic_count = 0

    for profile in profiles:
        ctx = EmitContext(spec=spec, pins=pins, profile=profile, multi_profile=multi)
        layers_by_agent = _resolve_agent_layers(spec, commands, profile)

        agent_files: dict[str, AgentArtifact] = {}
        agent_lock: dict[str, str] = {}
        agent_secrets: dict[str, list[str]] = {}
        fragments: dict[str, str] = {}

        for agent_name, layers in layers_by_agent.items():
            agent = spec.agents[agent_name]
            filename = workflow_filename(agent_name, profile if multi else None)
            artifact = build_agent(agent, layers, spec, ctx, filename=filename)
            agent_files[filename] = artifact
            agent_lock[agent_name] = lock_filename(agent_name, profile if multi else None)
            agent_secrets[agent_name] = artifact.required_secrets
            fragments.update(emit_fragments(layers, ctx))

        sub_workflow = {name: _workflow_filename(name, profile, multi) for name in commands}

        workflows: dict[str, WorkflowResult] = {}
        for name, command in commands.items():
            filename = _workflow_filename(name, profile, multi)
            result = emit_command(
                command,
                spec,
                ctx,
                filename=filename,
                agent_lock=agent_lock,
                agent_secrets=agent_secrets,
                sub_workflow=sub_workflow,
            )
            workflows[filename] = result
            plan.notes.extend(result.notes)
            for path, content in result.step_defs.items():
                plan.add(path, content)
            fused = result.step_count - result.job_count
            plan.summaries.append(
                f"{name}: {_plural(result.step_count, 'step')} -> {_plural(result.job_count, 'job')}"
                + (f" (fusion saved {fused})" if fused > 0 else "")
                + f" · {result.agentic_steps} agentic, {result.deterministic_steps} deterministic"
                + f", {result.cached_steps} cacheable"
            )

        applied = _apply_overlays(overlays, workflows, agent_files, root)

        out = spec.manifest.target.out
        for filename, result in workflows.items():
            command = commands[_command_for(filename, commands, profile, multi)]
            header = ctx.header(
                [command.src, spec.manifest.src],
                extra=_overlay_header(applied.get(f"workflows/{filename}", [])),
            )
            normalized = normalize(result.data)
            validate_workflow(filename, normalized)
            text = yamlio.annotate_pins(yamlio.dump(normalized), pins.sha_tags())
            plan.add(
                f"{out}/{filename}",
                yamlio.with_header(text, header),
                sources=[command.src.stamp()] if command.src else [],
            )
            deterministic_count += sum(
                1 for step in command.steps if step.kind.value in ("script", "builtin")
            )
            agentic_count += sum(1 for step in command.steps if step.kind.value == "agent")

        for filename, artifact in agent_files.items():
            verify_enforcement(artifact)
            artifact.extra_header = _overlay_header(applied.get(f"workflows/{filename}", []))
            plan.add(
                f"{out}/{filename}",
                render_agent(artifact, ctx),
                sources=[s.stamp() for s in artifact.sources if s],
            )

        for relative, content in fragments.items():
            plan.add(f"{out}/{relative}", content)

    _check_unapplied(overlays, plan, spec.manifest.target.out)

    plan.add(f"{spec.manifest.target.out}/.gitattributes", _gitattributes())
    plan.add(SECRETS_DOC, _secrets_doc(spec, profiles))
    plan.add(MANIFEST_PATH, _compile_manifest(plan, spec))

    plan.stats = {
        "workflows": sum(1 for p in plan.files if p.endswith(".yml")),
        "agentic": agentic_count,
        "deterministic": deterministic_count,
        "files": len(plan.files),
    }
    return plan


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _commands_to_compile(spec: Spec) -> dict[str, Command]:
    if spec.manifest.commands:
        missing = set(spec.manifest.commands) - set(spec.commands)
        if missing:
            raise EmitError(
                f"pipeline.yaml lists unknown commands: {', '.join(sorted(missing))}",
                hint=f"known commands: {', '.join(sorted(spec.commands))}",
            )
        return {name: spec.commands[name] for name in spec.manifest.commands}
    return dict(spec.commands)


def _workflow_filename(command: str, profile: Profile, multi: bool) -> str:
    base = slug(command)
    return f"{base}--{slug(profile.name)}.yml" if multi else f"{base}.yml"


def _command_for(filename: str, commands: dict[str, Command], profile: Profile, multi: bool) -> str:
    for name in commands:
        if _workflow_filename(name, profile, multi) == filename:
            return name
    raise EmitError(f"cannot map workflow {filename!r} back to a command")


def _resolve_agent_layers(spec: Spec, commands: dict[str, Command], profile: Profile) -> dict[str, Any]:
    """An agent's compiled identity is agent x resolved layer set; differing sets must not merge."""
    resolved: dict[str, Any] = {}
    origin: dict[str, str] = {}
    for command in commands.values():
        for step in command.steps:
            if step.kind.value != "agent" or not step.applies_to("github"):
                continue
            agent: Agent = spec.agents[step.target]
            layers = resolve_layers(agent, command, profile, spec)
            signature = layers.signature()
            if step.target in resolved and resolved[step.target].signature() != signature:
                raise EmitError(
                    f"agent {step.target!r} resolves to different prompt layers in different commands",
                    hint=(
                        f"`{origin[step.target]}` gives [{resolved[step.target].signature()}] but "
                        f"`{command.name}` gives [{signature}]; align the commands' guardrails, or wait "
                        "for per-command agent variants (Phase 2)"
                    ),
                )
            resolved.setdefault(step.target, layers)
            origin.setdefault(step.target, command.name)
    return resolved


def _apply_overlays(
    overlays: list[Overlay],
    workflows: dict[str, WorkflowResult],
    agents: dict[str, AgentArtifact],
    root: Path,
) -> dict[str, list[Overlay]]:
    applied: dict[str, list[Overlay]] = {}
    for overlay in overlays:
        target = overlay.target
        name = target.removeprefix("workflows/")
        hits = 0
        if name in workflows:
            hits += apply_mapping_ops(workflows[name].data, overlay.patches, location=overlay.rel)
        elif name in agents:
            artifact = agents[name]
            hits += apply_mapping_ops(artifact.frontmatter, overlay.frontmatter, location=overlay.rel)
            artifact.body, prompt_hits = apply_prompt_ops(
                artifact.body, overlay.prompt, root, location=overlay.rel
            )
            hits += prompt_hits
        else:
            continue
        if hits:
            applied.setdefault(target, []).append(overlay)
    return applied


def _check_unapplied(overlays: list[Overlay], plan: CompilePlan, out: str) -> None:
    """An overlay whose target was never generated is a typo, not a no-op."""
    from ..errors import OverlayAnchorNotFound

    generated = {path.removeprefix(f"{out}/") for path in plan.files}
    for overlay in overlays:
        name = overlay.target.removeprefix("workflows/")
        if name not in generated:
            import difflib

            close = difflib.get_close_matches(name, sorted(generated), n=1)
            raise OverlayAnchorNotFound(
                f"overlay targets {overlay.target!r}, which this compile does not generate",
                location=overlay.rel,
                hint=f"nearest: workflows/{close[0]}" if close else None,
            )


def _overlay_header(overlays: list[Overlay]) -> list[str]:
    if not overlays:
        return []
    return [f"overlays: {' '.join(o.stamp() for o in overlays)}"]


def _gitattributes() -> str:
    return (
        "# GENERATED by lockstep — collapses generated workflows in pull request diffs.\n"
        "*.yml linguist-generated=true\n"
        "*.lock.yml linguist-generated=true\n"
        "aw-*.md linguist-generated=true\n"
        "shared/*.md linguist-generated=true\n"
    )


def _secrets_doc(spec: Spec, profiles: list[Profile]) -> str:
    lines = [
        "<!-- GENERATED by lockstep — do not edit. -->",
        "",
        "# Secrets and variables",
        "",
        "Every secret and variable this pipeline needs, by environment.",
        "",
    ]
    for profile in profiles:
        environment = profile.github.environment or "(repository level)"
        lines.append(f"## Profile `{profile.name}` — environment `{environment}`")
        lines.append("")
        if profile.github.secrets:
            lines.append("### Secrets")
            lines.append("")
            for name in profile.github.secrets:
                lines.append(f"- `{name}`")
            lines.append("")
        if profile.github.vars:
            lines.append("### Variables")
            lines.append("")
            for name in profile.github.vars:
                lines.append(f"- `{name}`")
            lines.append("")
        lines.append("```bash")
        for name in profile.github.secrets:
            env_flag = f" --env {profile.github.environment}" if profile.github.environment else ""
            lines.append(f"gh secret set {name}{env_flag}")
        for name in profile.github.vars:
            env_flag = f" --env {profile.github.environment}" if profile.github.environment else ""
            lines.append(f"gh variable set {name}{env_flag}")
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _compile_manifest(plan: CompilePlan, spec: Spec) -> str:
    entries = {
        path: {"sha": short(sha_text(content)), "sources": plan.sources.get(path, [])}
        for path, content in sorted(plan.files.items())
    }
    payload: dict[str, Any] = {
        "compiler": __version__,
        "pipeline": spec.manifest.name,
        "spec": spec.manifest.spec_version,
        "files": entries,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
