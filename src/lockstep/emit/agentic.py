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
from .profiles import resolve_value

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

# What a job calling a compiled agent workflow has to grant it.
#
# A reusable workflow can never hold more permission than its caller, and this was emitted with
# none — so every scope but `contents` was `none` and GitHub refused the workflow before running
# anything: "The nested job 'activation' is requesting 'actions: read', but is only allowed
# 'actions: none'." No pipeline with an agent in it could ever have started.
#
# The three scopes are what `gh aw compile` produces jobs for. `activation` and `conclusion` read
# the Actions API to find the run they belong to; everything reads contents; and `conclusion` and
# `safe_outputs` write issues to report what the run produced and what it could not do. The set does
# not vary with declared safe outputs — those two jobs exist regardless.
#
# **This is not a hole in the floor.** The `agent` job is `read-all`, which `assert_floor` still
# checks after overlays, and the writes belong to gh-aw's own deterministic jobs. That is the
# safe-output model — an agent produces a request, machinery it does not control performs it — and
# the caller has to pass through enough permission for that machinery to exist at all.
#
# Pinned in spirit to `capabilities.gh-aw`, like ENGINE_SECRET above. A version needing a fourth
# scope would reintroduce exactly this failure, in production, so
# `test_agent_permissions.py` compares this against every committed `.lock.yml` and fails the build
# instead.
AGENT_CALLER_PERMISSIONS = {"actions": "read", "contents": "read", "issues": "write"}

# What the agent job itself gets, and it is deliberately not `read-all`.
#
# `read-all` was the old floor and it could not work as one. A called workflow may not exceed its
# caller, and GitHub expands `read-all` to *every* scope — it named artifact-metadata, attestations,
# checks, code-quality, deployments, discussions, drives, models, packages, pages, pull-requests,
# repository-projects, statuses and vulnerability-alerts when it refused the run. Matching that from
# a caller means enumerating a list GitHub keeps adding to, and every new scope would break startup
# again the day it shipped.
#
# So the agent asks for what it uses instead: the two scopes `gh aw compile`'s own jobs read. This
# is strictly narrower than before — read on two scopes rather than on twenty — which makes the
# workable answer also the safer one. Agents here reach nothing else through the workflow token;
# the MCP servers in this repository and its examples are local `filesystem` and `git` processes,
# and anything talking to an API carries its own credential.
AGENT_PERMISSIONS = {"actions": "read", "contents": "read"}

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


def _observability(spec: Spec, ctx: EmitContext) -> dict[str, Any]:
    """Point gh-aw's own exporter at the collector this pipeline already named.

    gh-aw emits the spans that actually describe an agent — model, tokens, finish reason, tool
    calls, under the GenAI semantic conventions — and it does that natively once `observability.otlp`
    is set. Nothing here could produce those from the outside.

    So the compiler wires it rather than reimplementing it. A user cannot do this by hand: agents are
    generated, and the frontmatter it would go in is overwritten on every compile. Configuring the
    collector twice, once for the framework's metrics and once per agent for the spans, is exactly
    the kind of thing that ends with half the telemetry silently going nowhere.

    Setting it also has a side effect worth knowing: gh-aw adds the collector's host to the agent's
    egress allow-list. An agent that could not reach the collector would export nothing, and a
    pipeline whose network policy is computed would otherwise have to be told about it separately.
    """
    config = spec.manifest.otel
    if not config.to_endpoint or not config.endpoint:
        return {}

    location = spec.manifest.src.rel if spec.manifest.src else "pipeline.yaml"
    endpoint: dict[str, Any] = {"url": resolve_value(config.endpoint, ctx.profile, location=location)}
    if config.headers:
        endpoint["headers"] = {
            name: resolve_value(value, ctx.profile, location=location)
            for name, value in sorted(config.headers.items())
        }
    otlp: dict[str, Any] = {"endpoint": [endpoint]}
    if config.service_name:
        # A namespace rather than a service name: gh-aw names the service after the workflow, which
        # is what keeps one agent's spans distinguishable from another's. Overwriting that with one
        # name for the whole pipeline would collapse exactly the distinction worth having.
        otlp["resource-attributes"] = {"service.namespace": config.service_name}
    return {"otlp": otlp}


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
    # The collector, if this pipeline exports to one. gh-aw allow-lists the host itself when the URL
    # is a literal it can read at compile time — it cannot when the URL is a runtime expression, and
    # the result is a firewall that drops the telemetry without failing anything. Added here so the
    # policy says what it means either way. A `deny-all` guardrail still wins: a pipeline forbidden
    # to reach the network does not get an exception carved for its own metrics.
    host = spec.manifest.otel.collector_host
    if network and spec.manifest.otel.to_endpoint and host and host not in network:
        network.append(host)

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
        "permissions": dict(AGENT_PERMISSIONS),
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

    observability = _observability(spec, ctx)
    if observability:
        frontmatter["observability"] = observability

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

    # The floor is "no scope is writable", not "the permissions equal one particular literal".
    # `read-all` used to be that literal and is now refused with everything else that is not an
    # explicit read-only map: it cannot be granted by a calling job without enumerating every scope
    # GitHub has, so an agent asking for it is an agent that cannot start.
    granted = front.get("permissions")
    if not isinstance(granted, dict) or any(level != "read" for level in granted.values()):
        raise EmitError(
            f"compiled agent requests permissions {granted!r}",
            location=location,
            hint="agent jobs are read-only on this target: give an explicit map of read scopes and "
            "route writes through safe-outputs",
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
