"""A suite a model's run dispatches runs in a tree that has an environment.

GATE-SANDBOX-2. The model-facing strategies materialise a worktree and dispatch `Test` into it.
`materialize` yields HEAD plus the staged change and nothing else; `prepared` runs the repository's
own `Provision` over HEAD first, so the tree carries the suite's dependencies.

Until this test existed the two were used inconsistently: every `Validate` site called `prepared`
and every `Test` site called `materialize`, which was invisible for as long as this repository's
`Test` binding mounted the host's `.venv`. #419 dropped that mount and #429 moved `Test` onto the
workshop's image, and from then on every `/implement` and `/fix` ran pytest in a container where
this project had never been installed: ZERO tests collected, a run that decided nothing, and
`tdd.not_red` telling the model its tests had passed. Run 34498550853, $10.18, on a change whose
tests were genuinely red.

Asserted over the source rather than by running a container, so it costs nothing and fails on the
edit rather than on the next paid run.

**Over the whole package, not over the strategies.** The first version of this walked
`adapters/ai/` alone, which is where the six model-facing dispatches live -- and missed
`worktree.py::verdict_over_staged`, the suite run whose result rides into the proposal and decides
whether a change request is marked ready. A rule aimed at the place the bug was found rather than
at the property is a rule that catches the instance and not the class.

`materialize` itself is not the problem and is not going anywhere: it is the primitive `prepared`
is built on, and `backport` and the Graft index use it correctly, because neither runs a suite and
neither needs an environment. What this refuses is dispatching a SUITE from one.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "in_lockstep"

#: What dispatching the suite looks like at a call site. `_test_spec` is `fix.py`'s spelling, which
#: builds a `Test` with the reproducer's paths on it.
DISPATCHES_A_SUITE = ("Test", "_test_spec")


def _context_manager_names(node: ast.AsyncWith) -> set[str]:
    """Every function called in the `async with` header, by name."""
    names = set()
    for item in node.items:
        call = item.context_expr
        if isinstance(call, ast.Call):
            func = call.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _dispatches_a_suite(node: ast.AST) -> bool:
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            func = inner.func
            named = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if named in DISPATCHES_A_SUITE:
                return True
    return False


def _suites_dispatched_from_a_bare_worktree(where: Path = SRC) -> list[str]:
    """Each `async with materialize(...)` whose body runs the suite, as `file:line`.

    Takes its directory so the negative control can point it at a fixture. A walk that can only
    ever look at one place is a walk nobody can prove finds anything.
    """
    found = []
    for path in sorted(where.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.AsyncWith):
                continue
            if "materialize" not in _context_manager_names(node):
                continue
            if any(_dispatches_a_suite(stmt) for stmt in node.body):
                found.append(f"{path.name}:{node.lineno}")
    return found


def test_gate_sandbox_2_a_models_suite_is_never_dispatched_from_an_unprovisioned_tree() -> None:
    """GATE-SANDBOX-2. The property, walked from the source rather than from a list here, so a
    seventh call site added later is under the rule instead of joining the six that were not."""
    assert _suites_dispatched_from_a_bare_worktree() == []


def test_the_walk_finds_a_suite_dispatched_from_a_bare_worktree(tmp_path: Path) -> None:
    """The negative control. Without it the assertion above passes over a walk that finds nothing
    because it looks in the wrong place, which is the vacuous shape that has bitten twice here."""
    (tmp_path / "regressed.py").write_text(
        "async def run():\n"
        "    async with materialize(root, staged) as tree:\n"
        "        outcome = await ctx.do(Test(root=tree))\n"
    )

    assert _suites_dispatched_from_a_bare_worktree(tmp_path) == ["regressed.py:2"]


def test_every_model_facing_suite_dispatch_provisions_first() -> None:
    """The positive half, and the one that would have caught the regression: the sites exist and
    they are `prepared`. An empty walk satisfies the assertion above for free."""
    provisioned = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.AsyncWith) and "prepared" in _context_manager_names(node):
                if any(_dispatches_a_suite(stmt) for stmt in node.body):
                    provisioned.append(f"{path.name}:{node.lineno}")
    # Seven: `run_tests`, tdd's red, green and revert control, fix's reproducer and green, and
    # `verdict_over_staged` -- the one the first version of this test was scoped too narrowly to see.
    assert len(provisioned) >= 7, f"only {len(provisioned)} provisioned dispatch(es): {provisioned}"
