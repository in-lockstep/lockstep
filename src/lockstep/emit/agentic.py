"""Agent definitions become gh-aw agentic workflows.

Each agent compiles to a single-item `workflow_call` workflow: fan-out stays in the orchestrator
because gh-aw frontmatter has no matrix, so an agentic workflow is always "one item, one run".
`gh aw compile` turns the emitted markdown into the committed .lock.yml the orchestrator calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import EmitError, UnmappedProvider
from ..spec.model import Agent, Enforce, Profile, SourceFile, Spec
from ..util import yamlio
from ..util.text import slug
from .context import EmitContext
from .fragments import PromptLayers, import_paths, inlined_guardrails
from .mcp import emit_mcp_servers, secrets_used

# The framework's providers, mapped onto gh-aw engines.
ENGINE_BY_PROVIDER = {
    "vertex-claude": "claude",
    "anthropic": "claude",
    "bedrock": "claude",
    "google-gemini": "gemini",
    "openai": "codex",
}
UNMAPPED_PROVIDERS = {"ollama"}

# The credential each engine reads to call its model.
#
# This is gh-aw's contract, not this compiler's: the secret is consumed by the workflows
# `gh aw compile` produces, and nothing lockstep emits ever references it. It is recorded here
# anyway, because a document titled "every secret this pipeline needs" that omits the most sensitive
# one is worse than no document. `capabilities.gh-aw` pins the version this was written against.
#
# `copilot` is absent deliberately: it authenticates with the workflow's GitHub token, so there is
# no separate secret to set.
ENGINE_SECRET = {
    "claude": "ANTHROPIC_API_KEY",
    "codex": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

# The fixed input contract every compiled agent workflow accepts.
WORKFLOW_INPUTS = {
    "item": {"type": "string", "required": False, "default": ""},
    "input_path": {"type": "string", "required": False, "default": ""},
    "output_path": {"type": "string", "required": True},
    "context_dir": {"type": "string", "required": False, "default": ""},
    "context_files": {"type": "string", "required": False, "default": ""},
}


def workflow_filename(agent_name: str, profile: Profile | None = None) -> str:
    base = f"aw-{slug(agent_name)}"
    if profile is not None:
        base = f"{base}--{slug(profile.name)}"
    return f"{base}.md"


def lock_filename(agent_name: str, profile: Profile | None = None) -> str:
    return workflow_filename(agent_name, profile).removesuffix(".md") + ".lock.yml"


def resolve_engine(agent: Agent) -> str:
    if agent.github.engine:
        return agent.github.engine
    provider = agent.provider or "vertex-claude"
    if provider in UNMAPPED_PROVIDERS:
        raise UnmappedProvider(
            f"provider {provider!r} has no gh-aw engine",
            location=agent.src.rel if agent.src else agent.name,
            hint="this agent can only run on the local backend; set github.engine to compile it",
        )
    engine = ENGINE_BY_PROVIDER.get(provider)
    if not engine:
        raise UnmappedProvider(
            f"unknown provider {provider!r}",
            location=agent.src.rel if agent.src else agent.name,
            hint=f"known providers: {', '.join(sorted(ENGINE_BY_PROVIDER))}",
        )
    return engine


def resolve_model(agent: Agent) -> str:
    model = agent.github.model or agent.model
    if model.startswith("${") and model.endswith("}"):
        raise EmitError(
            f"model {model!r} is an environment reference and cannot be resolved at compile time",
            location=agent.src.rel if agent.src else agent.name,
            hint="set github.model on the agent — CI pins the model, it does not look it up at run time",
        )
    return model


@dataclass
class AgentArtifact:
    """A compiled agent workflow, still structured so overlays can patch it."""

    filename: str
    frontmatter: dict[str, Any]
    body: str
    sources: list[SourceFile | None]
    layer_signature: str
    required_secrets: list[str] = field(default_factory=list)
    extra_header: list[str] = field(default_factory=list)
    # The enforcement floor computed from guardrails, re-checked after overlays run.
    enforced: Enforce = field(default_factory=Enforce)
    tool_floor: dict[str, list[str]] = field(default_factory=dict)


def build_agent(
    agent: Agent,
    layers: PromptLayers,
    spec: Spec,
    ctx: EmitContext,
    *,
    filename: str,
) -> AgentArtifact:
    """Build one agentic workflow: frontmatter + inlined guardrails + agent body."""
    enforce = layers.enforce()
    servers = emit_mcp_servers(agent, spec, enforce, ctx.profile)

    if agent.github.max_ai_credits is None:
        raise EmitError(
            "agent has no credit budget",
            location=agent.src.rel if agent.src else agent.name,
            hint="set github.max-ai-credits — on this target a budget is not optional",
        )

    call: dict[str, Any] = {"inputs": dict(WORKFLOW_INPUTS)}
    secrets = secrets_used(servers, ctx.profile)
    if secrets:
        # Never `secrets: inherit` — an undeclared secret must be a build-time error.
        call["secrets"] = {name: {"required": True} for name in secrets}

    network = [] if enforce.network == "deny-all" else ["defaults", *agent.github.network]

    frontmatter: dict[str, Any] = {
        "description": agent.description or f"{agent.name} agent",
        "on": {"workflow_call": call},
        "engine": {"id": resolve_engine(agent)},
        # Top level, not `engine.model`. gh-aw deprecated the nested form and warns on every
        # compile; a deprecation nobody sees is one that becomes a breakage at an upgrade.
        "model": resolve_model(agent),
        "max-turns": agent.max_tool_turns,
        "max-ai-credits": agent.github.max_ai_credits,
        "timeout-minutes": agent.github.timeout_minutes or 20,
        # The agent job never writes. Every side effect goes through a safe output.
        "permissions": "read-all",
        "network": {"allowed": network},
        "steps": [{"uses": ctx.pins.action("restore")}],
        "post-steps": [
            {"uses": ctx.pins.action("save"), "with": {"paths": "${{ inputs.output_path }}"}},
        ],
    }
    # A ceiling on repetition, enforced by gh-aw before the agent starts rather than by anything
    # here. `max-ai-credits` bounds one run; this bounds a day of them, which is the axis a chat-ops
    # command triggered four hundred times in an afternoon actually moves.
    if spec.manifest.per_agent_daily_ai_credits is not None:
        frontmatter["max-daily-ai-credits"] = spec.manifest.per_agent_daily_ai_credits

    if servers:
        frontmatter["mcp-servers"] = servers

    safe_outputs: dict[str, Any] = {"upload-artifact": {"allowed-paths": [f"{ctx.output_dir_env}/**"]}}
    safe_outputs.update(agent.github.safe_outputs)
    frontmatter["safe-outputs"] = safe_outputs

    imports = import_paths(layers)
    if imports:
        frontmatter["imports"] = imports
    if agent.github.raw:
        frontmatter.update(agent.github.raw)

    body_parts: list[str] = []
    guardrails = inlined_guardrails(layers)
    if guardrails:
        body_parts.append(guardrails)
    body_parts.append(agent.body.strip())
    body_parts.append(_io_section())

    sources: list[SourceFile | None] = [agent.src]
    sources.extend(fragment.src for fragment in layers.all)

    return AgentArtifact(
        filename=filename,
        frontmatter=frontmatter,
        body="\n\n".join(part for part in body_parts if part).strip() + "\n",
        sources=sources,
        layer_signature=layers.signature(),
        required_secrets=secrets,
        enforced=enforce,
        tool_floor={name: list(entry.get("allowed", [])) for name, entry in servers.items()},
    )


def verify_enforcement(artifact: AgentArtifact) -> None:
    """Re-assert the guardrail floor after overlays.

    Overlays are a customization tier, not an override of the enforceable half of a guardrail. A
    patch that widens egress, grants write permissions, or re-adds a denied tool is refused here
    rather than quietly shipping — otherwise `enforce:` would be advice, not enforcement.
    """
    front = artifact.frontmatter
    location = artifact.filename

    if artifact.enforced.network == "deny-all":
        allowed = (front.get("network") or {}).get("allowed") or []
        if allowed:
            raise EmitError(
                f"an overlay widened network egress to {allowed} but a guardrail enforces deny-all",
                location=location,
                hint="remove the overlay hunk, or relax `enforce.network` in the guardrail that sets it",
            )

    for key, ceiling, what in (
        ("max-turns", artifact.enforced.max_turns, "tool turns"),
        ("max-ai-credits", artifact.enforced.max_ai_credits, "credits"),
    ):
        if ceiling is None:
            continue
        requested = front.get(key)
        if requested is None or int(requested) > ceiling:
            raise EmitError(
                f"compiled agent asks for {requested} {what}, over the ceiling of {ceiling}",
                location=location,
                hint=f"lower `{key}` on the agent, or relax `enforce.{key}` in the guardrail that "
                "sets it — a sealed guardrail's ceiling is not the consuming repository's to move",
            )

    if front.get("permissions") != "read-all":
        raise EmitError(
            f"compiled agent requests permissions {front.get('permissions')!r}",
            location=location,
            hint="agent jobs are read-only on this target; route writes through safe-outputs",
        )

    for name, floor in artifact.tool_floor.items():
        actual = list((front.get("mcp-servers") or {}).get(name, {}).get("allowed") or [])
        widened = [tool for tool in actual if tool not in floor]
        if widened:
            raise EmitError(
                f"an overlay re-added denied MCP tools {widened} on server {name!r}",
                location=location,
                hint="guardrail `enforce.deny-tools` is a floor, not a default",
            )


def render_agent(artifact: AgentArtifact, ctx: EmitContext) -> str:
    """Serialize a built agent workflow, after any overlays have patched it."""
    header = ctx.header(
        artifact.sources,
        extra=[f"prompt layers: {artifact.layer_signature or '(none)'}", *artifact.extra_header],
    )
    yaml_text = yamlio.annotate_pins(yamlio.dump(artifact.frontmatter), ctx.pins.sha_tags())
    return "\n".join(
        [
            "---",
            "\n".join(f"# {line}" for line in header),
            yaml_text.rstrip("\n"),
            "---",
            "",
            artifact.body.rstrip("\n"),
            "",
        ]
    )


def _io_section() -> str:
    return (
        "## Input\n\n"
        "The item to process:\n\n"
        "${{ inputs.item }}\n\n"
        "Additional input file (may be empty): `${{ inputs.input_path }}`\n"
        "Pre-sliced context for this item (may be empty): `${{ inputs.context_dir }}`\n"
        "Context files on disk (comma-separated, may be empty): `${{ inputs.context_files }}`\n\n"
        "## Output\n\n"
        "Write your result to `${{ inputs.output_path }}`.\n"
    )
