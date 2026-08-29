"""The layering rule, enforced.

"Arrows point down only" is the rule the whole architecture rests on, and it is exactly the kind
of rule that erodes silently: one import added under deadline, and the god object at the centre of
the framework depends on the packages it is supposed to abstract over.

`RunContext` is where it would break first. It names an SCM, a ticket source, a ledger and a
notifier — and if those names resolved to implementations rather than protocols, `core` would
import `platform`, `human` and `notify`. So `core` may import `core.ports` and nothing else
outward, and this test is what holds it.

Written as a test rather than a separate linter config so it runs in the same command as
everything else. A gate in a tool nobody runs is not a gate.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "in_lockstep"

# Which packages a layer may import from. Downward only.
ALLOWED: dict[str, set[str]] = {
    # `privileged` runs outside the middleware chain, so everything may reach it. It may reach
    # `core` for vocabulary (Capability, and nothing else) but never an implementation package —
    # and core never imports it back, so the edge is acyclic.
    "privileged": {"core"},
    "core": {"core"},
    "config_ref": set(),
    "loader": {"config_ref", "loader"},
    # The transport is a leaf: it imports provider SDKs and itself, and nothing else of ours.
    # It used to sit at `ai/llm/`, where "nothing above `ai` reaches into the transport" was a
    # convention this test could not see. As a sibling layer with an empty allowance, both
    # directions are enforced — it cannot grow an edge back into the framework, and `ai` is the
    # only layer that may reach it.
    #
    # That last clause was written here before it was true: `cli` imported `Model` and
    # `LLMProvider` straight from `llm`, and `ALLOWED["cli"]` was widened to let it, which made
    # this comment assert an invariant the dict below it did not hold. The
    # names are re-exported from `ai.bootstrap` now, so the allowance could shrink to match.
    "llm": {"llm"},
    "ai": {"core", "ai", "llm", "privileged"},
    "prompts": {"ai", "prompts"},
    "adapters": {"core", "ai", "adapters", "prompts", "privileged"},
    "middleware": {"core", "middleware", "privileged"},
    "platform": {"core", "ai", "platform", "privileged"},
    "doctor": {"ai", "privileged", "prompts", "doctor"},
    "evaluation": {"evaluation"},
    # `adapters` was added when the first executable strategy was registered. A registration
    # names an implementation — that is what distinguishes it from a catalogue entry — so a
    # composition root that may not import one can only ever register strings, which is what this
    # file did for a phase. The edge is acyclic: `adapters` takes its registry by injection and
    # never imports `strategies` back.
    "strategies": {"ai", "core", "adapters", "strategies"},
    "lockstep": {"core", "lockstep"},
    # The package facade. It re-exports the public surface, so it reaches almost everything by
    # construction — but it was being SKIPPED rather than allowed, which is different: nothing
    # was checking that `in_lockstep/__init__.py` stayed a facade instead of growing logic.
    "__init__": {"core", "ai", "adapters", "lockstep", "prompts", "evaluation", "__init__"},
    "cli": {
        "core",
        "ai",
        "adapters",
        "middleware",
        "lockstep",
        "prompts",
        "privileged",
        "config_ref",
        "loader",
        "platform",
        "doctor",
        "evaluation",
        "strategies",
        "cli",
    },
}


def _layer_of(path: Path) -> str:
    rel = path.relative_to(SRC)
    return rel.parts[0] if len(rel.parts) > 1 else rel.stem


def _imported_layers(tree: ast.AST, path: Path) -> set[str]:
    depth = len(path.relative_to(SRC).parts) - 1
    layers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module and node.module.startswith("in_lockstep."):
                    layers.add(node.module.split(".")[1])
                continue
            # A relative import that walks up to the package root names a sibling layer.
            if node.level > depth:
                if node.module:
                    layers.add(node.module.split(".")[0])
                else:
                    # `from . import doctor` carries no module, and skipping it left a hole in
                    # this gate wide enough for a whole package to cross a layer through. Only
                    # names that are actually modules count — `from . import __version__` pulls a
                    # value out of the package __init__ and crosses nothing.
                    for alias in node.names:
                        name = alias.name.split(".")[0]
                        if (SRC / f"{name}.py").exists() or (SRC / name).is_dir():
                            layers.add(name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("in_lockstep."):
                    layers.add(alias.name.split(".")[1])
    return layers


MODULES = sorted(p for p in SRC.rglob("*.py"))


@pytest.mark.parametrize("path", MODULES, ids=[str(p.relative_to(SRC)) for p in MODULES])
def test_arrows_point_down_only(path: Path) -> None:
    layer = _layer_of(path)
    allowed = ALLOWED.get(layer)
    # An undeclared layer used to `skip`, which meant a new top-level package silently received
    # no layering coverage at all and the suite still went green. A gate whose failure mode is
    # "quietly checks nothing" is the failure mode this whole file exists to prevent, so an
    # undeclared layer is now a failure that forces someone to write down where it sits.
    assert allowed is not None, (
        f"{layer!r} has no entry in ALLOWED. Add one saying what it may import — an omission "
        f"reads as permission here, and nothing else in this file would notice."
    )
    violations = _imported_layers(ast.parse(path.read_text()), path) - allowed
    assert not violations, (
        f"{path.relative_to(SRC)} is in layer {layer!r} and imports {sorted(violations)}. "
        f"{layer!r} may import {sorted(allowed)}. If core needs a capability, add a Protocol to "
        f"core/ports/ and bind an implementation through the container."
    )


def test_core_does_not_import_implementations() -> None:
    """The specific inversion this rule exists to prevent."""
    forbidden = {"platform", "human", "notify", "adapters", "middleware", "ai", "llm"}
    for path in (SRC / "core").rglob("*.py"):
        imported = _imported_layers(ast.parse(path.read_text()), path)
        assert not (imported & forbidden), f"core/{path.name} imports {sorted(imported & forbidden)}"


def test_ports_declares_protocols_only() -> None:
    """core/ports is the outward edge; it must not grow an implementation."""
    tree = ast.parse((SRC / "core" / "ports" / "__init__.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
            is_protocol = "Protocol" in bases
            is_exception = any(b.endswith("Exception") or b.endswith("Error") for b in bases)
            is_constants = not node.bases
            assert is_protocol or is_exception or is_constants, (
                f"{node.name} in core/ports is neither a Protocol, an exception, nor constants"
            )
