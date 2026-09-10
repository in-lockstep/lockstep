"""The CI job and a model's run build their environment the same way, and the model's path is run.

GATE-CI-5 claims the `sandbox` job dispatches `selfcheck` "so the image, the mounts and the
environment are the ones the binding declares and cannot drift from what a model's `run_tests`
gets". For the whole of #419 to #434 that was false and nothing noticed: the job's tree came from
`prepared`, a model's came from `materialize`, and `materialize` had no environment in it after the
mounted host `.venv` went away. The job was green while every `/implement` collected zero tests.

The gate checked that the job dispatched through the binding and that the binding named an image.
Both were true. Neither was the claim.

Two tests, and the split is deliberate. The first is free and runs everywhere, so the class of
defect fails the build on the edit. The second dispatches through a model's own path into a real
container, which is the honest discharge of what the row says and skips by name where the image is
not pulled -- including inside the `sandbox` job itself, where there is no runtime.
"""

from __future__ import annotations

import ast
import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "in_lockstep"

#: Where each side of the claim builds its tree. The job's path is `selfcheck`, which `cli.py`
#: registers as a workflow and the workflow file dispatches; a model's is `_test_runner`, the
#: `run_tests` tool. These two are the pair the row is about.
THE_JOBS_PATH = (SRC / "cli.py", "selfcheck")
A_MODELS_PATH = (SRC / "adapters" / "ai" / "strategy.py", "_test_runner")


def _tree_builders(path: Path, function: str) -> set[str]:
    """Every context manager an `async with` in `function` opens, by name.

    Read from the source rather than by running either path, so this costs nothing and fails on the
    edit rather than on the next paid run.
    """
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        # Sync OR async, and everything nested inside. `_test_runner` is a sync factory whose work
        # is an `async def run` closure it returns -- looking only for `AsyncFunctionDef` found
        # nothing there, and the non-vacuity assertion below is what said so rather than the walk
        # quietly agreeing that a function with no worktree builds its tree correctly.
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or node.name != function:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.AsyncWith):
                continue
            for item in inner.items:
                call = item.context_expr
                if isinstance(call, ast.Call):
                    func = call.func
                    found.add(func.id if isinstance(func, ast.Name) else getattr(func, "attr", ""))
    return found


def test_gate_ci_5_the_job_and_a_models_run_build_their_tree_the_same_way() -> None:
    """The "cannot drift" clause, made checkable.

    Not "both provision" -- that is `GATE-SANDBOX-2`'s test and it would pass on two different
    functions that happened to agree today. This asserts the two paths name the SAME builder, so a
    change to how one gets its environment cannot leave the other behind without failing here.
    """
    jobs = _tree_builders(*THE_JOBS_PATH)
    models = _tree_builders(*A_MODELS_PATH)

    assert jobs, f"{THE_JOBS_PATH[1]} opens no worktree; this test is looking at the wrong function"
    assert models, f"{A_MODELS_PATH[1]} opens no worktree; this test is looking at the wrong function"
    assert "prepared" in jobs & models, (
        f"the job's path builds its tree with {sorted(jobs)} and a model's with {sorted(models)}. "
        f"GATE-CI-5 says the CI job cannot drift from what a model's run gets; two different "
        f"builders is exactly that drift, and it went unnoticed from #419 to #434."
    )


def test_the_walk_finds_a_function_that_builds_its_tree_differently(tmp_path: Path) -> None:
    """The negative control. An empty walk satisfies a set-membership assertion for free, and this
    gate has already been green through the thing it was written to catch."""
    other = tmp_path / "drifted.py"
    other.write_text(
        "async def selfcheck(ctx, paths):\n    async with materialize(root, staged) as tree:\n        pass\n"
    )

    assert _tree_builders(other, "selfcheck") == {"materialize"}
    assert "prepared" not in _tree_builders(other, "selfcheck")


def _image_present(image: str) -> str | None:
    """The runtime, if `image` is already pulled here. The live clause runs where it is and skips
    by name everywhere else -- which is the honest state, and is written in the row."""
    from in_lockstep.adapters.sandbox import Sandbox

    runtime = Sandbox().runtime()
    if runtime is None:
        return None
    done = subprocess.run([runtime, "image", "exists", image], capture_output=True)
    if done.returncode != 0:
        done = subprocess.run([runtime, "image", "inspect", image], capture_output=True)
    return runtime if done.returncode == 0 else None


def _this_repositorys_workshop_image() -> str:
    from in_lockstep.core.workflow import restore, snapshot
    from in_lockstep.loader import load

    state = snapshot()
    try:
        module, _ref = load(str(ROOT))
        commands = getattr(module.lockstep.workshop, "commands", None)
        return str(getattr(getattr(commands, "inner", commands), "image", "") or "")
    finally:
        restore(state)


def test_gate_ci_5_a_models_own_test_path_reaches_a_decided_verdict() -> None:
    """The live clause: one staged failing test, dispatched through `run_tests` itself.

    This is what the CI job's claim amounts to and what no test asserted. The job proves
    `selfcheck`; a model's run takes `_test_runner`, and between #419 and #434 that path collected
    zero tests on every invocation while the job stayed green. Asserting a DECIDED verdict is the
    point -- a suite that collected nothing comes back undecided, which is the shape the whole
    failure wore.
    """
    from in_lockstep.adapters.ai.strategy import _test_runner
    from in_lockstep.ai.builtins import Workspace
    from in_lockstep.core.context import Approval
    from in_lockstep.core.types import ChangeAuthor, FileChange
    from in_lockstep.core.workflow import restore, snapshot
    from in_lockstep.loader import load

    image = _this_repositorys_workshop_image()
    if not image or _image_present(image) is None:
        pytest.skip(
            f"GATE-CI-5's live clause not checked: {image or 'the workshop image'} is not pulled "
            f"here. It runs where the image is, and is skipped inside the sandbox job itself, "
            f"which has no container runtime"
        )

    state = snapshot()
    try:
        module, _ref = load(str(ROOT))
        lockstep = module.lockstep
        # `--approved-by`, never `--approve`: `ApprovalGate` gates on EXECUTES_CODE, and nobody is
        # watching a test run.
        ctx: Any = lockstep.context("gate-ci-5-live", approval=Approval(by="gate-ci-5"))
        workspace = Workspace(root=lockstep.repo.root)
        workspace.changes.append(
            FileChange(
                path="tests/in_lockstep/test_gate_ci_5_probe.py",
                contents="def test_gate_ci_5_probe_is_red() -> None:\n    assert False, 'red on purpose'\n",
                author=ChangeAuthor.AGENT,
            )
        )
        answer = asyncio.run(
            _test_runner(ctx, lockstep.repo.root, workspace)(("tests/in_lockstep/test_gate_ci_5_probe.py",))
        )
    finally:
        restore(state)

    assert "NOTHING WAS COLLECTED" not in answer, (
        f"a model's own test path collected nothing, so it decides nothing about any change it "
        f"stages -- which is what the CI job goes green about. It said: {answer}"
    )
    assert "1 failed" in answer, f"the staged test was not run where a model's run runs it: {answer}"
