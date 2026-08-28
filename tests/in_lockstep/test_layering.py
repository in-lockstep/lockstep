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
    # `privileged` sits beside core rather than above it: redaction and egress run outside the
    # middleware chain, so everything may reach them and they may reach nothing.
    "privileged": set(),
    "core": {"core"},
    "config_ref": set(),
    "ai": {"core", "ai", "privileged"},
    "prompts": {"ai", "prompts"},
    "adapters": {"core", "ai", "adapters", "prompts", "privileged"},
    "middleware": {"core", "middleware"},
    "lockstep": {"core", "lockstep"},
    "cli": {
        "core", "ai", "adapters", "middleware", "lockstep", "prompts", "privileged",
        "config_ref", "cli",
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
            if node.level > depth and node.module:
                layers.add(node.module.split(".")[0])
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
    if allowed is None:
        pytest.skip(f"{layer} has no declared layer policy yet")
    violations = _imported_layers(ast.parse(path.read_text()), path) - allowed
    assert not violations, (
        f"{path.relative_to(SRC)} is in layer {layer!r} and imports {sorted(violations)}. "
        f"{layer!r} may import {sorted(allowed)}. If core needs a capability, add a Protocol to "
        f"core/ports/ and bind an implementation through the container."
    )


def test_core_does_not_import_implementations() -> None:
    """The specific inversion this rule exists to prevent."""
    forbidden = {"platform", "human", "notify", "adapters", "middleware", "ai"}
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
