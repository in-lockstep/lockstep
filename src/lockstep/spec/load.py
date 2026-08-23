"""Load a whole pipeline spec from a directory tree."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from ..errors import MissingDefinition, SpecError
from ..util.hashing import sha_file, short
from .model import (
    Capabilities,
    Extensions,
    Manifest,
    SourceFile,
    Spec,
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


def _load_dir(root: Path, subdir: str) -> list[SourceFile]:
    directory = root / subdir
    if not directory.is_dir():
        return []
    return [read_source(p, root) for p in sorted(directory.rglob("*.md"))]


def _fragment_name(src: SourceFile, subdir: str) -> str:
    """`skills/test/common.md` is referenced as `test/common`."""
    rel = Path(src.rel).relative_to(subdir)
    return str(rel.with_suffix("")).replace("\\", "/")


def load_manifest(root: Path) -> Manifest:
    path = root / MANIFEST_NAME
    if not path.is_file():
        raise MissingDefinition(
            f"no {MANIFEST_NAME} found in {root}",
            hint="run `lockstep init` or add a pipeline.yaml manifest",
        )
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    caps_raw = data.get("capabilities", {}) or {}
    target_raw = (data.get("targets", {}) or {}).get("github-agentic", {}) or {}
    budgets = data.get("budgets", {}) or {}
    extensions_raw = data.get("extensions", {}) or {}

    manifest = Manifest(
        spec_version=int(data.get("spec", 1) or 1),
        name=str(data.get("name", "") or root.name),
        capabilities=Capabilities(
            actions=str(caps_raw.get("actions", "") or ""),
            exec=str(caps_raw.get("exec", "") or ""),
            compiler=str(caps_raw.get("compiler", "") or ""),
            gh_aw=str(caps_raw.get("gh-aw", "") or ""),
        ),
        target=TargetConfig(
            out=str(target_raw.get("out", ".github/workflows")),
            fuse_script_steps=bool(target_raw.get("fuse-script-steps", True)),
            default_runs_on=str(target_raw.get("default-runs-on", "ubuntu-24.04")),
            shard_threshold=int(target_raw.get("shard-threshold", 20)),
            profiles=[str(p) for p in (target_raw.get("profiles", []) or [])],
        ),
        per_run_ai_credits=budgets.get("per_run_ai_credits"),
        commands=data.get("commands", {}) or {},
        extensions=Extensions(
            builtins=[str(name) for name in (extensions_raw.get("builtins") or [])],
            packages=[str(name) for name in (extensions_raw.get("packages") or [])],
        ),
        src=SourceFile(
            path=path,
            rel=MANIFEST_NAME,
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


def load_spec(root: Path) -> Spec:
    """Read every definition under `root` into a validated Spec."""
    root = root.resolve()
    manifest = load_manifest(root)
    spec = Spec(root=root, manifest=manifest)

    for src in _load_dir(root, "commands"):
        command = parse_command(src)
        spec.commands[command.name] = command
    for src in _load_dir(root, "agents"):
        agent = parse_agent(src)
        spec.agents[agent.name] = agent
    for src in _load_dir(root, "profiles"):
        profile = parse_profile(src)
        spec.profiles[profile.name] = profile
    for subdir, bucket in (
        ("guardrails", spec.guardrails),
        ("skills", spec.skills),
        ("contexts", spec.contexts),
    ):
        for src in _load_dir(root, subdir):
            fragment = parse_fragment(src, subdir.rstrip("s"))
            fragment.name = _fragment_name(src, subdir)
            bucket[fragment.name] = fragment

    mcp_path = root / "mcp" / "servers.json"
    if mcp_path.is_file():
        spec.mcp_servers = parse_mcp_servers(json.loads(mcp_path.read_text(encoding="utf-8")))

    _validate(spec)
    return spec


def _validate(spec: Spec) -> None:
    """Cross-reference checks that must fail at compile time, not at 2am in a scheduled run."""
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
                script = spec.root / step.target
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
            if name not in spec.skills:
                raise MissingDefinition(f"unknown skill {name!r}", location=loc)
        for name in agent.mcp:
            if name not in spec.mcp_servers:
                raise MissingDefinition(f"unknown MCP server {name!r}", location=loc)

    for profile in spec.profiles.values():
        loc = profile.src.rel if profile.src else profile.name
        for name in profile.contexts:
            if name not in spec.contexts:
                raise MissingDefinition(f"unknown context {name!r}", location=loc)
