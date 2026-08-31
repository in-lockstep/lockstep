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
    # `platform` was added for one edge: `adapters/backport.py` picks commits with the git surface
    # `platform/scm` owns — `start_point`'s bare-then-remote fallback, `commits_between`'s trailer
    # parsing, `cherry_pick`'s provenance discipline. Duplicating those in adapters would be two
    # writers of the trailer format, which is the failure the ledger module documents at length.
    # Acyclic: nothing in `platform` imports `adapters` back.
    "adapters": {"core", "ai", "adapters", "prompts", "privileged", "platform"},
    "middleware": {"core", "middleware", "privileged"},
    "platform": {"core", "ai", "platform", "privileged"},
    # Doctor asks "are the controls actually in place?", and two of the controls live in other
    # layers: config provenance reads the CI environment through `platform.ci` (hardcoding
    # GITHUB_* here is how the check silently skipped GitLab), and the route check loads the
    # module through `loader` — with `core.workflow`'s snapshot/restore so a diagnostic leaves
    # no registrations behind. Acyclic: none of those import doctor back; only `cli` does.
    # `packs` and `receipt` were added for DOC170-172, which ask what an installed pack may do
    # against what this repository accepted. Both edges are reads of a derivation that already
    # exists: re-deriving the comparison inline here is how two answers to one question start
    # disagreeing, which is the argument `_model_routes` already makes about `table_for`.
    # Acyclic: neither imports doctor back; only `cli` does.
    "doctor": {"ai", "core", "loader", "packs", "platform", "privileged", "prompts", "receipt", "doctor"},
    "evaluation": {"evaluation"},
    # `receipt` derives what a configuration does, so it reads across the layers that hold the
    # declarations: capabilities off `core`, the composed projection off `ai`, prices and egress
    # hosts off `ai`/`privileged`, cases off `evaluation`. Almost `doctor`'s allowance, and beside
    # it on purpose — doctor asks whether the controls are in place, this asks what is configured,
    # and merging them would make one module answer two questions.
    #
    # `lockstep` is the one edge doctor does not take: this takes it because the subject of a
    # receipt IS a configured `Lockstep`, and typing that parameter `Any` to dodge a line in this
    # dict would buy nothing and cost the check mypy makes. Acyclic: the facade cannot import
    # `receipt` — its own allowance below forbids it — and nothing in `lockstep` needs to.
    # `packs` reads distribution metadata and files; it reaches `ai` for `Body` and the
    # frontmatter split, so a pack's guardrail fragment is handled exactly like a shipped one.
    # Nothing deeper: discovery must not be able to bind, and a layer it cannot import is a layer
    # it cannot bind into.
    "packs": {"ai", "packs"},
    "receipt": {"ai", "core", "evaluation", "lockstep", "packs", "privileged", "receipt"},
    # `market` reads catalogs and the receipts they point at. It reaches `privileged` to write the
    # sources file through the redacting sink, and nothing else of ours: a catalog is data about
    # packs, so a layer that could import `packs` or `adapters` could start resolving a listing
    # into something bindable — which is the auto-binding this whole design refuses.
    "market": {"market", "privileged"},
    # `trial` runs a verb against a cassette, so it names an implementation — the same edge
    # `strategies` takes, for the same reason: a measurement that could only reference strings
    # would not be a measurement of anything. It reaches `evaluation` for the corpus contract and
    # `packs` for where a pack keeps its cases. Acyclic: none of those import `trial`.
    "trial": {"adapters", "ai", "core", "evaluation", "packs", "privileged", "trial"},
    # `adapters` was added when the first executable strategy was registered. A registration
    # names an implementation — that is what distinguishes it from a catalogue entry — so a
    # composition root that may not import one can only ever register strings, which is what this
    # file did for a phase. The edge is acyclic: `adapters` takes its registry by injection and
    # never imports `strategies` back.
    "strategies": {"ai", "core", "adapters", "strategies"},
    # `platform` was added for exactly one edge: the pre-run daily spend ceiling reads the
    # ledger, and the ledger is platform's. The facade composes what the layers provide, and a
    # startup refusal that lived anywhere shallower (the CLI) would not cover a programmatic
    # `lockstep.context(...)`. Acyclic: platform never imports the facade back.
    "lockstep": {"core", "platform", "lockstep"},
    # The package facade. It re-exports the public surface, so it reaches almost everything by
    # construction — but it was being SKIPPED rather than allowed, which is different: nothing
    # was checking that `in_lockstep/__init__.py` stayed a facade instead of growing logic.
    # `platform` was added for one edge: the ticket vocabulary (`Ticket`, `TicketSource`, ...) is
    # part of the authoring surface a `lockstep.py` types its workflow parameters with, and a
    # facade that cannot re-export it forces every user file into a deep import. Acyclic:
    # `platform` never imports the facade back.
    "__init__": {"core", "ai", "adapters", "lockstep", "platform", "prompts", "evaluation", "__init__"},
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
        "receipt",
        "packs",
        "market",
        "trial",
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
