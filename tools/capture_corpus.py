"""Capture the composed-prompt characterization corpus, while the compiler still runs.

This is Phase-0 tooling for the pivot (design/in-lockstep-design.md v0.5). It exists because the
composition order — guardrails -> body -> skills -> contexts — and the enforce() ceiling merge are
properties of `src/lockstep/emit/fragments.py`, which the pivot deletes. Once it is gone the
invariant is unrecoverable, so it is frozen here first and the new composer is held to it.

Two artifacts per (profile, agent):

  projection  the ordered section-identity list, INCLUDING an explicit `body:` sentinel at its
              position. `PromptLayers.signature()` omits the body because the body is not a
              Fragment — so signature() alone cannot detect the body moving relative to the layers,
              which is the single most likely composition regression (R6-QA-6).

  composed    the full text the model is given, guardrails inlined verbatim first.

Plus the merged `enforce()` ceilings, which are what GATE-POLICY-1 holds the new PolicyStack to:
deny-all is an irreversible floor, ceilings take the lowest not the last, scan is strictest-wins,
deny-tools union. That equivalence is testable against this compiler today.

Run:  uv run python tools/capture_corpus.py
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from lockstep.emit.fragments import inlined_guardrails, resolve_layers
from lockstep.emit.plan import _resolve_agent_layers
from lockstep.spec.load import load_spec

OUT = Path("tests/characterization")


def projection(layers, agent_name: str) -> list[str]:
    """Ordered section identity with the body at its real position."""
    return [
        *[f"guardrail:{f.name}" for f in layers.guardrails],
        f"body:{agent_name}",
        *[f"skill:{f.name}" for f in layers.skills],
        *[f"context:{f.name}" for f in layers.contexts],
    ]


def composed(layers, agent) -> str:
    """Guardrails inlined first, then the body, then skills and contexts in order."""
    parts = []
    guards = inlined_guardrails(layers)
    if guards:
        parts.append(guards)
    parts.append(agent.body.strip())
    for fragment in (*layers.skills, *layers.contexts):
        parts.append(f"<!-- {fragment.kind}: {fragment.name} -->\n{fragment.body.strip()}")
    return "\n\n".join(parts) + "\n"


def main(root: str = ".", label: str = "") -> int:
    spec = load_spec(Path(root))
    OUT.mkdir(parents=True, exist_ok=True)
    index: dict[str, dict] = {}

    for profile in spec.compiled_profiles():
        commands = {n: c for n, c in spec.commands.items()}
        for agent_name, layers in sorted(_resolve_agent_layers(spec, commands, profile).items()):
            agent = spec.agents[agent_name]
            scope = label or profile.name
            key = f"{scope}/{agent_name}"
            text = composed(layers, agent)
            path = OUT / "prompts" / scope / f"{agent_name}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            index[key] = {
                "projection": projection(layers, agent_name),
                "signature_without_body": layers.signature(),
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "enforce": asdict(layers.enforce()),
            }

    out = OUT / (f"corpus-{label}.json" if label else "corpus.json")
    out.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    print(f"captured {len(index)} composed prompts -> {OUT}")
    for key in sorted(index):
        print(f"  {key}: {len(index[key]['projection'])} sections")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(*sys.argv[1:]))
