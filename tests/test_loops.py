"""Convergence loops and partial-failure policy — the two places Actions has no native answer."""

from __future__ import annotations

import pytest
import yaml

from lockstep.emit import compile_spec
from lockstep.errors import EmitError


def workflow(root, name):
    return yaml.safe_load(compile_spec(root).files[f".github/workflows/{name}"])


def jobs_of(root, name):
    return workflow(root, name)["jobs"]


# --- convergence unrolling -------------------------------------------------


def test_a_convergence_loop_unrolls_to_a_bounded_chain(basic_spec_dir):
    """Actions has no `while`, so the bound becomes explicit at compile time."""
    jobs = jobs_of(basic_spec_dir, "validate.yml")
    assert [job for job in jobs if job.startswith("repair-loop")] == [
        "repair-loop-1",
        "repair-loop-2",
        "repair-loop-3",
    ]


def test_each_iteration_is_skipped_once_the_previous_one_converged(basic_spec_dir):
    jobs = jobs_of(basic_spec_dir, "validate.yml")
    assert jobs["repair-loop-1"]["if"] == "${{ inputs.skip_repair != true }}"
    assert jobs["repair-loop-2"]["if"] == (
        "${{ inputs.skip_repair != true && needs.repair-loop-1.outputs.converged != 'true' }}"
    )
    assert jobs["repair-loop-3"]["needs"] == "repair-loop-2"


def test_the_callee_publishes_a_converged_workflow_output(basic_spec_dir):
    data = workflow(basic_spec_dir, "repair.yml")
    call = (data.get("on") or data.get(True))["workflow_call"]
    assert call["outputs"]["converged"]["value"] == "${{ jobs.collect-failures.outputs.converged }}"


def test_the_producing_job_exposes_the_step_output(basic_spec_dir):
    job = jobs_of(basic_spec_dir, "repair.yml")["collect-failures"]
    assert job["outputs"]["converged"] == "${{ steps.check-convergence.outputs.converged }}"


def test_run_steps_carry_ids_so_outputs_and_overlays_can_address_them(basic_spec_dir):
    steps = jobs_of(basic_spec_dir, "repair.yml")["collect-failures"]["steps"]
    assert "check-convergence" in [step.get("id") for step in steps]


def test_converged_from_naming_an_unknown_step_is_refused(basic_root):
    command = basic_root / "commands" / "repair.md"
    command.write_text(
        command.read_text().replace("converged-from: check-convergence", "converged-from: ghost")
    )
    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    assert "known step ids" in excinfo.value.render()


def test_a_single_iteration_keeps_the_plain_job_name(basic_root):
    command = basic_root / "commands" / "validate.md"
    command.write_text(command.read_text().replace("   - max-iterations: 3\n", ""))
    assert "repair-loop" in jobs_of(basic_root, "validate.yml")
    assert "repair-loop-1" not in jobs_of(basic_root, "validate.yml")


def test_command_level_max_iterations_acts_as_the_default(basic_root):
    command = basic_root / "commands" / "validate.md"
    text = command.read_text().replace("   - max-iterations: 3\n", "")
    text = text.replace("    schedule: '0 3 * * *'", "    schedule: '0 3 * * *'\n  max-iterations: 2")
    command.write_text(text)
    jobs = jobs_of(basic_root, "validate.yml")
    assert "repair-loop-2" in jobs
    assert "repair-loop-3" not in jobs


# --- partial-failure policy ------------------------------------------------


def test_min_success_rate_emits_an_explicit_coverage_gate(basic_spec_dir):
    """One failed matrix leg must not decide the pipeline's fate by accident."""
    job = jobs_of(basic_spec_dir, "generate-tests.yml")["verify-extract-stories-from-each-issue"]
    assert job["if"] == "${{ !cancelled() }}"
    run = next(step["run"] for step in job["steps"] if step.get("id", "").startswith("verify-"))
    assert "--min-success-rate=0.8" in run
    assert "--dir=outputs/extracted-stories" in run


def test_the_gate_compares_against_the_expected_item_list(basic_spec_dir):
    job = jobs_of(basic_spec_dir, "generate-tests.yml")["verify-extract-stories-from-each-issue"]
    run = next(step["run"] for step in job["steps"] if step.get("id", "").startswith("verify-"))
    assert "items_extract_stories_from_each_issue" in run
    assert job["needs"] == ["extract-stories-from-each-issue", "fetch-issues"]


def test_downstream_work_depends_on_the_gate_not_the_matrix(basic_spec_dir):
    jobs = jobs_of(basic_spec_dir, "generate-tests.yml")
    assert jobs["build-test-manifest"]["needs"] == "verify-extract-stories-from-each-issue"


def test_without_a_declared_rate_every_leg_must_succeed(basic_root):
    """The default stays strict: plain `needs:` already fails downstream on any failed leg."""
    command = basic_root / "commands" / "generate-tests.md"
    command.write_text(command.read_text().replace("   - min-success-rate: 0.8\n", ""))
    jobs = jobs_of(basic_root, "generate-tests.yml")
    assert not any(job.startswith("verify-") for job in jobs)
    assert jobs["build-test-manifest"]["needs"] == "extract-stories-from-each-issue"
