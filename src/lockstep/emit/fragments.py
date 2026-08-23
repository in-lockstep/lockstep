"""Prompt-layer flattening.

Composition order — guardrails, agent body, skills, contexts — is a framework invariant. It is
resolved and hashed here, at compile time, because that is the only place it can be guaranteed:
guardrails are inlined at the top of the generated body rather than trusted to import merge order,
and every flattened fragment is also written to shared/ so the layering stays auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..spec.model import Agent, Command, Enforce, Fragment, Profile, Spec
from ..util.text import slug
from .context import EmitContext

SHARED_DIR = "shared"


@dataclass
class PromptLayers:
    guardrails: list[Fragment] = field(default_factory=list)
    skills: list[Fragment] = field(default_factory=list)
    contexts: list[Fragment] = field(default_factory=list)

    @property
    def all(self) -> list[Fragment]:
        return [*self.guardrails, *self.skills, *self.contexts]

    def enforce(self) -> Enforce:
        """Merge the enforceable half of every guardrail into one set of substrate constraints."""
        merged = Enforce()
        for fragment in self.guardrails:
            if fragment.enforce.permissions:
                merged.permissions = fragment.enforce.permissions
            if fragment.enforce.network:
                merged.network = fragment.enforce.network
            for tool in fragment.enforce.deny_tools:
                if tool not in merged.deny_tools:
                    merged.deny_tools.append(tool)
        return merged

    def signature(self) -> str:
        """Identity of this layer set — two commands resolving the same layers share an agent file."""
        return "|".join(f"{f.kind}:{f.name}" for f in self.all)


def resolve_layers(agent: Agent, command: Command | None, profile: Profile, spec: Spec) -> PromptLayers:
    """Merge guardrails from agent + command, minus profile exclusions; contexts from the profile."""
    layers = PromptLayers()
    seen: set[str] = set()

    names = list(agent.guardrails)
    if command:
        names.extend(command.guardrails)
    for name in names:
        if name in seen or name in profile.exclude_guardrails:
            continue
        fragment = spec.guardrails.get(name)
        if fragment:
            seen.add(name)
            layers.guardrails.append(fragment)

    for name in agent.skills:
        fragment = spec.skills.get(name)
        if fragment:
            layers.skills.append(fragment)

    for name in profile.contexts:
        fragment = spec.contexts.get(name)
        if fragment:
            layers.contexts.append(fragment)

    return layers


def fragment_filename(fragment: Fragment) -> str:
    return f"{SHARED_DIR}/{fragment.kind}-{slug(fragment.name)}.md"


def import_paths(layers: PromptLayers) -> list[str]:
    """Skills then contexts, in order. Guardrails are inlined, not imported."""
    return [fragment_filename(f) for f in (*layers.skills, *layers.contexts)]


def render_fragment(fragment: Fragment, ctx: EmitContext) -> str:
    header = ctx.header([fragment.src], extra=[f"prompt layer: {fragment.kind} / {fragment.name}"])
    lines = [f"<!-- {line} -->" for line in header]
    return "\n".join(lines) + "\n\n" + fragment.body.strip() + "\n"


def emit_fragments(layers: PromptLayers, ctx: EmitContext) -> dict[str, str]:
    """Flatten every prompt layer into shared/*.md, keyed by path relative to the workflows dir."""
    return {fragment_filename(f): render_fragment(f, ctx) for f in layers.all}


def inlined_guardrails(layers: PromptLayers) -> str:
    """Guardrail text for the top of a generated agent body, verbatim and in order."""
    if not layers.guardrails:
        return ""
    blocks = [
        "<!-- Guardrails are inlined first, verbatim: their position is a security property "
        "and is not delegated to import merge order. -->"
    ]
    for fragment in layers.guardrails:
        blocks.append(f"<!-- guardrail: {fragment.name} -->\n{fragment.body.strip()}")
    return "\n\n".join(blocks)
