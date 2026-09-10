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
    # `platform/scm` owns — `start_point`'s full-path-then-as-written probe, `commits_between`'s trailer
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
    # `metrics` is a leaf beside `evaluation`, and for the same reason spelled there: the
    # moment it reaches for a store it stops being testable against a list somebody wrote by
    # hand, and a metrics module you cannot write a fixture for is one nobody checks the
    # arithmetic of. It takes `list[dict]` and returns strings; the caller does the reading,
    # the network and the writing.
    "metrics": {"metrics"},
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
    # `adapters` takes, for the same reason: a measurement that could only reference strings
    # would not be a measurement of anything. It reaches `evaluation` for the corpus contract and
    # `packs` for where a pack keeps its cases. Acyclic: none of those import `trial`.
    "trial": {"adapters", "ai", "core", "evaluation", "packs", "privileged", "trial"},
    # The measuring half of the learning loop. It grades promoted cases and reads the ledger
    # census, which the workflow consuming it may not import; the workflow reaches it through
    # the `Improver` port in `core`. Same shape as `trial`, for the same reason.
    "improver": {"ai", "core", "evaluation", "metrics", "improver"},
    # `adapters` was added when the first executable strategy was registered. A registration
    # names an implementation — that is what distinguishes it from a catalogue entry — so a
    # composition root that may not import one can only ever register strings, which is what this
    # file did for a phase. The edge is acyclic: nothing in `adapters` imports a composition
    # root back.
    # The processes the framework ships for an adopter to register. It names implementations —
    # `Implement`, `Fix`, the artifact readers, the proposal helpers — because a process that could
    # only reference strings would not be a process. Acyclic: none of those imports `workflows`,
    # and nothing inside the framework does either. Only an adopter's own module and `cli` (for
    # `show-workflow`, which reads the source) reach it.
    "workflows": {"core", "adapters", "platform", "workflows"},
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
        # `cli` reads `metrics` and does the two things `metrics` may not: the store read
        # and the write through `privileged.sink`. The edge points down and there is no
        # edge back — `metrics` imports nothing of ours, which is what keeps its arithmetic
        # testable against a list somebody wrote by hand.
        "metrics",
        "receipt",
        "packs",
        "market",
        "trial",
        "improver",
        # `show-workflow` prints the shipped process from `inspect.getsource` on the module that
        # is actually imported, and `init --implement` scaffolds the `register()` call, so the CLI
        # imports what it is about to describe. One direction: `workflows` never imports `cli`.
        "workflows",
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


def test_every_layer_named_in_allowed_exists() -> None:
    """A phantom layer is an allowance that can never be exercised, and never fails.

    `strategies` sat in `ALLOWED["cli"]` from the pivot commit until #270, for a package that was
    never built. It cost nothing and proved nothing, which is the problem: this dict reads as the
    architecture, so a name in it that resolves to nothing means the reader is looking at a plan
    rather than at the tree. The same shape as a gate row citing a gate nobody wrote.

    The other direction is `test_arrows_point_down_only`, which refuses a layer with no entry. Between
    the two, the dict and the tree name the same set.
    """
    named = set(ALLOWED) | {layer for allowed in ALLOWED.values() for layer in allowed}
    missing = {n for n in named if not (SRC / f"{n}.py").exists() and not (SRC / n).is_dir()}
    assert not missing, (
        f"ALLOWED names {sorted(missing)}, which are not packages or modules under src/in_lockstep. "
        f"An allowance for something that does not exist is never exercised and never fails."
    )


#: Statements inside `cli.py`'s module-level helpers — the functions that are not click commands.
#: Pinned at the measured value, and two-sided, for the reason `.coverage-floor` is: a
#: one-directional ratchet with a stale floor is a dead gate.
#:
#: Statements rather than lines, and helpers rather than the file. A line count is what the
#: trampoline gate tried first and it measured the wrong thing — it went red when a workflow
#: carried its record out as a bundle, one more invocation and no more logic, and the tempting fix
#: was to raise the number. It would be worse here: this codebase asks for dense comments, so a
#: line count would charge a contributor for following the house style. An AST statement count
#: cannot see a comment or a docstring at all.
#:
#: The commands are excluded because their statements are argument handling, which is the thing
#: this module is FOR and should be free to grow. What is bounded is the other 900.
#: 900 -> 930 with #163: `_improve_measure` composes the learning loop's defaults -- binds the
#: adapter and the corpus port the module left unbound, routes the drafting model, registers the
#: shipped process -- and hands the run to `_run_registered`. Composition and translation, the
#: same shape as `implement`'s; what a proposal MEANS lives in `workflows/improve.py` and
#: `improver.py`, where a test reaches it without a CliRunner.
#: 930 -> 965 with #204, all three of them rendering or composing. `_scaffold_review` writes the
#: `/review` trampoline and prints what it wrote, the same shape as the two write-verb scaffolds;
#: `_guardrail_chains` renders one line per distinct stack now that a `Lens` may carry its own,
#: where `ls` used to print the first lens's chain as everybody's; `_route_flag` renders the note
#: beside a route to a lens nothing binds. What a lens MEANS -- which stack, which ceilings, which
#: route wins -- lives in `prompts/review.py` (`Lens.stack`, `Lens.under`) and
#: `ai/bootstrap.py` (`routed_model`), where `test_lens.py` reaches it without a CliRunner.
#: 965 -> 967 with #275, rendering: `_route_flag` says `unchecked` beside a lens route when the
#: bound adapter states no lens set, where silence would have read as checked and fine.
#: 967 -> 973 with #289, composing the record: `_provenance` reads what `lockstep.identity`
#: claims into the record beside `ci_actor`, and `history --explain` renders the line. What an
#: identity IS -- configured, never invented -- lives in `platform/identity.py`, where
#: `test_identity.py` reaches it without a CliRunner.
#: 973 -> 994 with #294, composing and rendering: `_report_host` finds the host once for both
#: questions `--scm` asks, `_bundles_line` renders how many run records are still in artifacts or
#: a dash with the reason, and `_provenance` carries the host's run id into the record. Which
#: artifacts are outstanding and what "once" means live in `platform/ledger/reconcile.py`, where
#: `test_reconcile.py` reaches them against two real git ledgers and a stubbed host.
#: 994 -> 997 with #295, rendering: `_history_line` names the rewrites somebody acknowledged
#: beside the ones nobody has. What an acknowledgement is, and what it covers, lives in
#: `platform/ledger/history.py`, where `test_history.py` reaches it without a CliRunner.
#: 997 -> 1008 with #307, rendering: `_ledger_line` names the ref `report` read and the records
#: each side holds that the other does not, or a dash. Which ref a read resolves to, what a pull
#: does and how divergence is counted live in `platform/ledger/history.py` (`resolved`, `pull`,
#: `divergence`), where `test_history.py` reaches them over a bare origin without a CliRunner.
#: 1008 -> 1032 with #311, rendering: the text half of `show-prompt` moved out of the command
#: body into `_show_prompt_text` so one `BodyNotFound` handler covers projection and text alike,
#: and `improve --explain` says where a body the loop may propose to lives when a tier-1 path
#: refuses. What a body resolves to, and where a house one lives, live in `ai/prompt.py` and the
#: guard, where `test_prompt_composition.py` and `test_cli.py`'s guard test reach them.
#: 1032 -> 1086 with #316, composing and rendering: `_render_trampoline` fills a trampoline's
#: provider placeholders from `_ci_recipe`'s table, `_provider_for` reads the verb's route off the
#: module, `_verb_config` fits a write-verb block to what detection found, `_sandbox_image` maps
#: the one detected stack to an image, and `init` refuses outside the root and names what to
#: decide. What detection finds -- the lint script, the lockfiles, the manifests one level down --
#: lives in `lockstep.py` and `core/context.py`, where `test_detect.py` reaches it without a
#: CliRunner.
#: 1086 -> 1090 with #308, rendering: `_verb_config` writes the Test rebind with a container as
#: the commented line an adopter completes, and the things-to-decide list names the image a
#: model-staged test needs. Whether a runner would put a staged file on the host is decided in
#: `adapters/sandbox.py` (`host_fallback`) and `adapters/worktree.py` (`staged_refusal`), where
#: `test_controls.py` and the strategy tests reach it without a CliRunner.
#: 1090 -> 1166 with #314, composing and rendering, and mostly an accounting change: the body of
#: `review` -- the invoker, the bind, the run, the printed verdict, the ledger line, the comment
#: -- moved into `_review_one` so it runs once per `--aspect`, and a command's body was never
#: counted while a helper's is. The logic that is new is the loop, `_worse` and `--blocked-ok`'s
#: branch in `_exit_for`, each a decision that used to be shell in a workflow and now has a
#: test; `_gitlab_things_to_decide` renders facts the scaffold already states. What a blocked
#: run means stays `_workflow_verdict`'s; what a lens is stays `platform/chatops.py`'s.
#: 1166 -> 1238 with Phase 5's park (PR-12), composing and rendering: `resume` is a command, but
#: `_continuation_for` resolves the id a barrier names against the registry and refuses by name,
#: `_resumption_kwargs` finds the continuation's `Resumption` parameter by annotation,
#: `_mark_parked`/`_clear_parked` put the park where the person acts and take it off again, and
#: `_ls_parked` renders the barriers the shared store holds. What a park means, what a tick does
#: to a record and which write launches the continuation live in `core/human.py` and
#: `platform/barrier.py`, where `test_core.py` and `test_barrier.py` reach them over a bare origin
#: without a CliRunner.
#: 1238 -> 1247 with PR-13, composing: `_ensure_review_bound` binds the shipped `AiReview` under
#: the module's ceilings when `run` dispatches a workflow and the module bound no `Review`, the
#: binding `review` already made a few lines into its own body. Which lenses run, and how a
#: fan-out joins them, live in `workflows/review.py`, where `test_enforced_review.py` reaches
#: them with a stub adapter.
#: 1247 -> 1263 with PR-16, composing: `_eval_judge` loads the module, binds the shipped judge
#: when none is bound (`_ensure_judge_bound`, the `_ensure_review_bound` shape), registers the
#: shipped `judge/corpus` workflow when the module did not, and hands the corpus to
#: `_run_registered`. What the judge is asked, what is replayed and what a verdict settles live in
#: `improver.py` and `workflows/judge.py`, where `test_judge_loop.py` reaches them without a
#: CliRunner.
#: 1263 -> 1277 with the first sidecar, translating: `_eval_run` reads the verdicts the judge
#: kept beside each case and hands the one whose key is this rubric over this answer to `grade`,
#: and prints how many it replayed. Which key a verdict answers to is `improver.py`'s
#: (`corpus_rubrics`, `known_verdicts`); whether a kept verdict settles or fails a case is
#: `grade`'s and `summarize`'s.
#: 1277 -> 1327 with PR-18, composing and translating: `_around_report` loads the module once
#: and calls three things in order, `_resolve_merge` turns `#N` or a sha into a commit and a
#: tz-aware moment (the host's answer or git's, each refused by name), and `_subject_of_merge`
#: turns a merge's touched files into the one declared body's label or a refusal that names
#: `--subject`. Which records fall in which window is `windows_around`'s, the arithmetic and the
#: epoch refusal are `compare()`'s, and the text is `metrics.around_lines`'s, where
#: `test_platform.py` reaches all three with a list.
#: 1327 -> 1341 with the fix to `GATE-LEDGER-2`'s finding, composing: `_steps_with_subjects`
#: stamps each review lens step of a workflow record with the subject `_write_ledger` already
#: computes for a bespoke review run, on the lens's routed model. What a subject IS stays
#: `evaluation.subject`'s; which record a step becomes in a window is `windows_around`'s.
#: 1341 -> 1351 for `GATE-RECORD-6`, translating: `_turns_taken` reads `turns` and `tool_calls`
#: off the run's `Spend` for the three record writers and `--explain` renders the line. What a
#: turn is and what it charges stays `Spend.charge_turn`'s, where `test_core.py` reaches it.
#: 1351 -> 1360 for `GATE-SEARCH-1`, composing: `provision` runs the framework's own installs
#: (`_framework_provisions` gathers what bound adapters name in `provisions`) after the adopter's
#: bound step, and no longer returns early when nothing is bound to `Provision`. What an install
#: is and where it goes stays `adapters.graft`'s, where `test_graft.py` reaches it.
#: 1360 -> 1366 for #419, COMPOSING: `selfcheck` wires a provisioned copy of the working tree and
#: a runner into the two requests it already dispatched. What it means to run over a copy is
#: `working_copy`'s, what to install is `prepared`'s, and where to run is `own_code_runner`'s --
#: all in `adapters/worktree.py`, where a test reaches them without a `CliRunner`. Six statements
#: here are the wiring that has to live at the composition root by definition, and two of them are
#: translations: paths named against the repository made relative to it before they are handed to
#: a verb running over a copy, and a container asked for only where a `Provision` exists to fill it.
CLI_HELPER_STATEMENTS = 1366

#: How far below the pin the count may drift before the pin itself is stale. Same shape as the
#: coverage ratchet's two points: moving logic out is the point, and the reward for doing it is
#: being asked to write down that it happened.
CLI_HELPER_SLACK = 40


def _helper_statements(path: Path) -> int:
    import ast

    tree = ast.parse(path.read_text())

    def is_command(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        for decorator in fn.decorator_list:
            node = decorator.func if isinstance(decorator, ast.Call) else decorator
            label = ast.unparse(node)
            if "command" in label or "group" in label:
                return True
        return False

    total = 0
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or is_command(node):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.stmt):
                continue
            # A docstring is an expression statement holding a string, and this codebase writes
            # long ones on purpose. Counting them would make the rule argue against the style.
            if isinstance(inner, ast.Expr) and isinstance(inner.value, ast.Constant):
                if isinstance(inner.value.value, str):
                    continue
            total += 1
    return total


def test_the_composition_root_does_not_grow_logic() -> None:
    """`cli` is wide by design and bounded by nothing, so logic accumulates there unnoticed.

    The layering gate above enforces direction and acyclicity. Neither bounds width, and `cli`
    reaches 18 of the other 20 packages where the next widest reaches 8 — correctly, because a
    composition root is the one place that may name every implementation. The cost is that this is
    the one module where a lifecycle decision can be added and no gate says anything: `_eval_subject`
    and `_case_key` are identity decisions living in the argument-parsing layer, and each arrived
    by a defensible local argument (#239).

    So the bound is not a refactor and does not ask for one. It asks that the next such decision
    come with the sentence explaining it, the way `MAX_STATEMENTS` does for a trampoline — a cap
    works when it forces an argument each time it bites, and does not when it is raised on sight.

    Two-sided. Over the pin, say why the composition root needed more logic, or move it down. Under
    it by more than the slack, lower the pin and take the credit: a floor nobody lowers is how a
    ratchet becomes a formality.
    """
    count = _helper_statements(SRC / "cli.py")
    assert count <= CLI_HELPER_STATEMENTS, (
        f"cli.py's helpers hold {count} statements, over the pinned {CLI_HELPER_STATEMENTS}. "
        f"If this is a decision about what a run MEANS -- identity, keying, what a census counts, "
        f"what a workflow dispatches to -- it belongs where a workflow can reach it and a test can "
        f"run it without a CliRunner. If it is parsing, composing, rendering or translating, raise "
        f"the pin in the same commit and say which."
    )
    assert count >= CLI_HELPER_STATEMENTS - CLI_HELPER_SLACK, (
        f"cli.py's helpers hold {count} statements, {CLI_HELPER_STATEMENTS - count} below the "
        f"pinned {CLI_HELPER_STATEMENTS}. Lower the pin to {count}: logic left the composition "
        f"root, which is the direction this is for, and a floor nobody lowers stops being one."
    )
