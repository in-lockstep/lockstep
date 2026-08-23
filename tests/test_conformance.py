"""Does the compiled graph behave like the spec it came from?

Golden tests prove the compiler emits the same text twice; they say nothing about whether that text
executes the way the spec reads. These simulate the emitted graph and check the properties that
matter: order follows step order, conditions reach the right jobs, loops terminate, and nothing is
unreachable.
"""

from __future__ import annotations

import itertools

import pytest
import yaml

from lockstep.conformance import UnreadableExpression, evaluate, simulate, topological_order
from lockstep.emit import compile_spec
from lockstep.errors import LockstepError
from lockstep.spec.load import load_spec

FIXTURE = None  # set per-test from the module-scoped fixture directory


def workflows(root):
    return {
        path.rsplit("/", 1)[-1]: yaml.safe_load(text)
        for path, text in compile_spec(root).files.items()
        if path.endswith(".yml")
    }


# --- the expression grammar ------------------------------------------------


def test_every_condition_the_compiler_emits_can_be_evaluated(basic_spec_dir):
    """An expression the simulator cannot read is one nobody has reasoned about."""
    for name, workflow in workflows(basic_spec_dir).items():
        for job_id, job in (workflow.get("jobs") or {}).items():
            condition = job.get("if")
            if condition is None:
                continue
            try:
                evaluate(condition, {}, {})
            except UnreadableExpression as error:  # pragma: no cover - the assert reports it
                pytest.fail(f"{name}:{job_id} emits an unreadable condition: {error.render()}")


def test_unknown_expressions_are_refused_rather_than_guessed():
    with pytest.raises(UnreadableExpression):
        evaluate("${{ github.actor == 'nobody' }}", {}, {})


@pytest.mark.parametrize(
    ("expression", "inputs", "expected"),
    [
        ("${{ inputs.pdf == true }}", {"pdf": True}, True),
        ("${{ inputs.pdf == true }}", {"pdf": False}, False),
        ("${{ inputs.skip == true }}", {}, False),
        ("${{ inputs.skip != true }}", {}, True),
        ("${{ !cancelled() }}", {}, True),
        ("${{ inputs.a != true && inputs.b != true }}", {"a": False, "b": False}, True),
        ("${{ inputs.a != true && inputs.b != true }}", {"a": True, "b": False}, False),
    ],
)
def test_condition_semantics(expression, inputs, expected):
    assert evaluate(expression, inputs, {}) is expected


def test_a_schedule_trigger_sees_declared_defaults(basic_spec_dir):
    """On a schedule there are no inputs at all; a condition must still resolve sensibly."""
    validate = workflows(basic_spec_dir)["validate.yml"]
    assert "repair-loop-1" in simulate(validate).ran


# --- graph shape -----------------------------------------------------------


def test_no_workflow_contains_a_dependency_cycle(basic_spec_dir):
    for workflow in workflows(basic_spec_dir).values():
        topological_order(workflow.get("jobs") or {})


def test_a_cycle_is_reported_rather_than_hanging():
    jobs = {"a": {"needs": "b"}, "b": {"needs": "a"}}
    with pytest.raises(LockstepError) as excinfo:
        topological_order(jobs)
    assert "cycle" in str(excinfo.value)


def test_job_order_follows_spec_step_order(basic_spec_dir):
    """The compiled graph must not quietly reorder the work the spec describes."""
    spec = load_spec(basic_spec_dir)
    for name, command in spec.commands.items():
        workflow = workflows(basic_spec_dir)[f"{name}.yml"]
        order = simulate(workflow).order
        positions = {job: index for index, job in enumerate(order)}

        previous = -1
        for step in command.steps:
            if not step.applies_to("github"):
                continue
            candidates = [job for job in positions if job == step.id or job.startswith(f"{step.id}-")]
            if not candidates:
                continue
            position = min(positions[job] for job in candidates)
            assert position > previous, f"{name}: step {step.label!r} runs out of order"
            previous = position


def test_every_step_that_targets_github_reaches_a_job(basic_spec_dir):
    spec = load_spec(basic_spec_dir)
    for name, command in spec.commands.items():
        jobs = set(workflows(basic_spec_dir)[f"{name}.yml"]["jobs"])
        for step in command.steps:
            if not step.applies_to("github"):
                continue
            reached = step.id in jobs or any(job.startswith(f"{step.id}-") for job in jobs)
            fused = any(
                step.id in str(job.get("steps", ""))
                for job in workflows(basic_spec_dir)[f"{name}.yml"]["jobs"].values()
            )
            assert reached or fused, f"{name}: step {step.label!r} reaches no job"


def test_local_only_steps_reach_no_job(basic_spec_dir):
    jobs = workflows(basic_spec_dir)["generate-tests.yml"]["jobs"]
    assert not any("deploy-the-app-locally" in job for job in jobs)


def test_no_job_is_unreachable(basic_spec_dir):
    """A job that cannot run under any input combination is dead weight nobody notices."""
    for name, workflow in workflows(basic_spec_dir).items():
        jobs = workflow.get("jobs") or {}
        declared = ((workflow.get("on") or workflow.get(True) or {}).get("workflow_dispatch") or {}).get(
            "inputs"
        ) or {}
        flags = [key for key, spec in declared.items() if spec.get("type") == "boolean"]

        reachable: set[str] = set()
        for combination in itertools.product([True, False], repeat=len(flags)):
            inputs = dict(zip(flags, combination, strict=True))
            reachable |= simulate(workflow, inputs).ran
        assert reachable == set(jobs), f"{name}: unreachable jobs {sorted(set(jobs) - reachable)}"


# --- behaviour under conditions -------------------------------------------


def test_skipping_the_repair_loop_skips_every_iteration(basic_spec_dir):
    validate = workflows(basic_spec_dir)["validate.yml"]
    ran = simulate(validate, {"skip_repair": True}).ran
    assert not any(job.startswith("repair-loop") for job in ran)


def test_a_convergence_loop_stops_once_it_converges(basic_spec_dir):
    """The whole point of unrolling: later iterations must not run after convergence is reported."""
    validate = workflows(basic_spec_dir)["validate.yml"]
    ran = simulate(validate, {}, {"repair-loop-1": {"converged": "true"}}).ran
    assert "repair-loop-1" in ran
    assert "repair-loop-2" not in ran
    assert "repair-loop-3" not in ran


def test_an_unconverged_loop_runs_to_its_bound(basic_spec_dir):
    validate = workflows(basic_spec_dir)["validate.yml"]
    ran = simulate(validate, {}, {f"repair-loop-{n}": {"converged": "false"} for n in (1, 2)}).ran
    assert {"repair-loop-1", "repair-loop-2", "repair-loop-3"} <= ran


def test_skipping_discovery_still_runs_everything_downstream(basic_spec_dir):
    generate = workflows(basic_spec_dir)["generate-tests.yml"]
    ran = simulate(generate, {"skip_discovery": True}).ran
    assert "discover-application-structure" not in ran
    assert "fetch-issues" in ran
    assert "extract-stories-from-each-issue" in ran


def test_a_skipped_dependency_skips_what_depends_on_it():
    workflow = {
        "jobs": {
            "a": {"if": "${{ inputs.go == true }}"},
            "b": {"needs": "a"},
            "c": {"needs": "b"},
        }
    }
    assert simulate(workflow, {"go": False}).order == []
    assert simulate(workflow, {"go": True}).order == ["a", "b", "c"]


def test_the_coverage_gate_still_runs_after_a_failed_leg(basic_spec_dir):
    """`!cancelled()` is what lets the gate decide, instead of the matrix deciding for it."""
    generate = workflows(basic_spec_dir)["generate-tests.yml"]
    gate = generate["jobs"]["verify-extract-stories-from-each-issue"]
    assert evaluate(gate["if"], {}, {}) is True


# --- the worked example ----------------------------------------------------

EXAMPLE = None


@pytest.fixture
def httpbin_example():
    from pathlib import Path

    return Path(__file__).parent.parent / "examples" / "httpbin"


def test_the_example_pipeline_compiles_and_is_reachable(httpbin_example):
    workflow = workflows(httpbin_example)["validate-api.yml"]
    assert simulate(workflow).order == [
        "list-endpoints",
        "write-a-contract-test-for-each-endpoint",
        "verify-write-a-contract-test-for-each-endpoint",
        "check-the-generated-tests-are-well-formed",
        "render-and-publish-the-report",
        "propose-generated-artifacts",
    ]


def test_skipping_generation_skips_its_coverage_gate_too(httpbin_example):
    """With nothing generated there is nothing to judge; the gate would fail at 0% coverage."""
    workflow = workflows(httpbin_example)["validate-api.yml"]
    ran = simulate(workflow, {"skip_generation": True}).ran
    assert "write-a-contract-test-for-each-endpoint" not in ran
    assert "verify-write-a-contract-test-for-each-endpoint" not in ran
    assert "check-the-generated-tests-are-well-formed" in ran


def test_the_example_runs_the_committed_tests_without_any_agent(httpbin_example):
    """Steady state: the expensive step is skipped and the run costs nothing."""
    workflow = workflows(httpbin_example)["validate-api.yml"]
    ran = simulate(workflow, {"skip_generation": True}).ran
    agentic = {name for name, job in workflow["jobs"].items() if "aw-" in str(job.get("uses", ""))}
    assert not (ran & agentic)
