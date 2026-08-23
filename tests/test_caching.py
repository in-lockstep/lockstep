"""Skip-on-cached-output: content-addressed keys, the force escape hatch, and live-target staleness."""

from __future__ import annotations

import json

import yaml

from lockstep.emit import compile_spec
from lockstep.emit.caching import cache_spec_for, declared_outputs
from lockstep.emit.context import EmitContext, Pins
from lockstep.spec.load import load_spec
from lockstep.spec.model import Command, Step, StepKind


def jobs_of(root, name):
    return yaml.safe_load(compile_spec(root).files[f".github/workflows/{name}"])["jobs"]


def steps_of(root, workflow, job):
    return jobs_of(root, workflow)[job]["steps"]


def by_id(steps, step_id):
    return next(step for step in steps if step.get("id") == step_id)


def by_name(steps, name):
    return next(step for step in steps if step.get("name") == name)


def context_for(root) -> EmitContext:
    spec = load_spec(root)
    return EmitContext(spec=spec, pins=Pins.load(root, spec), profile=spec.profiles["my-app"])


def test_a_step_declaring_an_output_is_wrapped_in_a_cache_probe(basic_spec_dir):
    steps = steps_of(basic_spec_dir, "generate-tests.yml", "fetch-issues")
    probe = by_id(steps, "cache-fetch-issues")
    assert probe["uses"].startswith("pipeline-fw/pipeline-actions/step-cache@")
    assert probe["with"]["outputs"].strip() == "outputs/jira-issues.json"


def test_the_work_and_its_save_are_gated_on_a_cache_miss(basic_spec_dir):
    steps = steps_of(basic_spec_dir, "generate-tests.yml", "fetch-issues")
    condition = "${{ steps.cache-fetch-issues.outputs.hit != 'true' }}"
    assert by_name(steps, "Fetch issues from Jira")["if"] == condition
    assert by_name(steps, "Publish fetch-issues outputs")["if"] == condition


def test_hooks_are_gated_with_their_step(basic_spec_dir):
    """A cache hit skips the whole step, hooks included — they are part of the step, not extras."""
    steps = steps_of(basic_spec_dir, "discover.yml", "discover-api-surface")
    assert by_name(steps, "post: Discover UI structure")["if"] == (
        "${{ steps.cache-discover-ui-structure.outputs.hit != 'true' }}"
    )


def test_on_failure_is_not_gated_by_the_cache(basic_spec_dir):
    steps = steps_of(basic_spec_dir, "generate-tests.yml", "build-test-manifest")
    assert by_name(steps, "on-failure: Build test manifest")["if"] == "${{ failure() }}"


def test_key_inputs_cover_the_script_and_the_step_definition(basic_spec_dir):
    probe = by_id(steps_of(basic_spec_dir, "generate-tests.yml", "fetch-issues"), "cache-fetch-issues")
    key_inputs = probe["with"]["key-inputs"].split()
    assert "scripts/fetch-issues.py" in key_inputs
    assert ".pipeline/step-defs/generate-tests.fetch-issues.json" in key_inputs


def test_invalidation_cascades_to_upstream_outputs(basic_spec_dir):
    """A step reading an earlier step's output must re-run when that output changes."""
    probe = by_id(
        steps_of(basic_spec_dir, "generate-tests.yml", "build-test-manifest"),
        "cache-build-test-manifest",
    )
    assert "outputs/extracted-stories" in probe["with"]["key-inputs"]


def test_runtime_inputs_join_the_key(basic_spec_dir):
    probe = by_id(steps_of(basic_spec_dir, "generate-tests.yml", "fetch-issues"), "cache-fetch-issues")
    assert probe["with"]["key-extra"] == "${{ inputs.jql }}"


def test_force_and_force_steps_are_threaded_through(basic_spec_dir):
    probe = by_id(steps_of(basic_spec_dir, "generate-tests.yml", "fetch-issues"), "cache-fetch-issues")
    assert probe["with"]["force"] == "${{ inputs.force }}"
    assert probe["with"]["force-steps"] == "${{ inputs.force_steps }}"


def test_step_definitions_are_generated_and_track_the_spec(basic_root):
    plan = compile_spec(basic_root)
    path = ".pipeline/step-defs/generate-tests.fetch-issues.json"
    before = json.loads(plan.files[path])
    assert before["kind"] == "script"

    command = basic_root / "commands" / "generate-tests.md"
    command.write_text(command.read_text().replace("--jql=", "--query="))
    after = json.loads(compile_spec(basic_root).files[path])
    assert after != before


def test_output_dir_alone_never_triggers_a_skip(basic_spec_dir):
    """A directory always exists, so treating it as a skip signal would skip work that never ran."""
    ctx = context_for(basic_spec_dir)
    step = Step(
        number=1,
        label="Report",
        kind=StepKind.SCRIPT,
        target="scripts/x.py",
        id="report",
        args={"args": "--run-dir=outputs/runs/latest --output-dir=outputs"},
    )
    command = Command(name="c")
    assert declared_outputs(step, ctx, command) == []
    assert cache_spec_for(step, command, ctx, {}) is None


def test_a_step_with_no_declared_output_is_not_cached(basic_spec_dir):
    ctx = context_for(basic_spec_dir)
    step = Step(number=1, label="Notify", kind=StepKind.SCRIPT, target="scripts/x.py", id="notify")
    assert cache_spec_for(step, Command(name="c"), ctx, {}) is None


def test_nested_command_steps_are_not_cached(basic_spec_dir):
    job = jobs_of(basic_spec_dir, "generate-tests.yml")["discover-application-structure"]
    assert "uses" in job
    assert ".pipeline/step-defs/generate-tests.discover-application-structure.json" not in (
        compile_spec(basic_spec_dir).files
    )


def test_a_live_target_fingerprint_joins_the_key(basic_spec_dir):
    """A staging redeploy changes no repo file; without this the pipeline serves stale discovery."""
    steps = steps_of(basic_spec_dir, "discover.yml", "discover-api-surface")
    fingerprint = by_id(steps, "fingerprint-discover-api-surface")
    assert "openapi.json" in fingerprint["run"]
    assert "${{ vars.API_URL }}" in fingerprint["run"]

    probe = by_id(steps, "cache-discover-api-surface")
    assert probe["with"]["key-extra"] == "${{ steps.fingerprint-discover-api-surface.outputs.value }}"


def test_fingerprint_failure_refuses_to_serve_a_cached_result(basic_spec_dir):
    run = by_id(
        steps_of(basic_spec_dir, "discover.yml", "discover-api-surface"),
        "fingerprint-discover-api-surface",
    )["run"]
    assert "set -euo pipefail" in run
    assert "exit 1" in run


def test_the_fingerprint_runs_before_the_probe(basic_spec_dir):
    steps = steps_of(basic_spec_dir, "discover.yml", "discover-api-surface")
    ids = [step.get("id") for step in steps]
    assert ids.index("fingerprint-discover-api-surface") < ids.index("cache-discover-api-surface")


def test_the_profile_fingerprint_is_part_of_the_key_prefix(basic_root):
    def prefix(root):
        probe = by_id(steps_of(root, "generate-tests.yml", "fetch-issues"), "cache-fetch-issues")
        return probe["with"]["key-prefix"]

    before = prefix(basic_root)
    profile = basic_root / "profiles" / "my-app.md"
    profile.write_text(profile.read_text().replace("auth_method=jwt", "auth_method=basic"))
    assert prefix(basic_root) != before


def test_cacheable_steps_are_counted_in_the_summary(basic_spec_dir):
    summaries = compile_spec(basic_spec_dir).summaries
    assert any("discover: 2 steps -> 1 job" in line and "2 cacheable" in line for line in summaries)
    assert any("generate-tests:" in line and "2 cacheable" in line for line in summaries)
