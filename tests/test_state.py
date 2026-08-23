"""The state database rides an artifact between steps, so its scope must be provably safe."""

from __future__ import annotations

import pytest
import yaml

from lockstep.emit import compile_spec
from lockstep.errors import EmitError

STATE_ARG = "--state={state_db}"


def steps_of(root, workflow, job):
    data = yaml.safe_load(compile_spec(root).files[f".github/workflows/{workflow}"])
    return data["jobs"][job]["steps"]


def by_name(steps, name):
    return next(step for step in steps if step.get("name") == name)


def test_state_is_loaded_and_saved_around_the_owning_job(basic_spec_dir):
    steps = steps_of(basic_spec_dir, "discover.yml", "discover-api-surface")
    load, save = by_name(steps, "Load state"), by_name(steps, "Save state")
    assert load["uses"].startswith("pipeline-fw/pipeline-actions/state/load@")
    assert save["uses"].startswith("pipeline-fw/pipeline-actions/state/save@")
    assert load["with"]["path"] == "outputs/.state/user-story-validation.db"
    assert save["if"] == "${{ always() }}"


def test_state_load_precedes_every_step_that_uses_it(basic_spec_dir):
    steps = steps_of(basic_spec_dir, "discover.yml", "discover-api-surface")
    names = [step.get("name") for step in steps]
    assert names.index("Load state") < names.index("Discover UI structure") < names.index("Save state")


def test_state_db_token_expands_to_a_workspace_path(basic_spec_dir):
    steps = steps_of(basic_spec_dir, "discover.yml", "discover-api-surface")
    run = by_name(steps, "Discover UI structure")["run"]
    assert "--state=outputs/.state/user-story-validation.db" in run
    assert "{state_db}" not in run


def test_state_keep_is_passed_through_as_retention(basic_root):
    command = basic_root / "commands" / "discover.md"
    command.write_text(command.read_text().replace("state: true", "state: keep"))
    steps = steps_of(basic_root, "discover.yml", "discover-api-surface")
    assert by_name(steps, "Save state")["with"]["retain"] is True


def test_state_spanning_two_jobs_is_refused(basic_root):
    """Artifact-mediated state is last-writer-wins; spanning jobs would silently lose writes."""
    command = basic_root / "commands" / "generate-tests.md"
    text = command.read_text().replace("---\nname: generate-tests", "---\nname: generate-tests\nstate: true")
    text = text.replace(
        '   - args: --output={output_dir}/jira-issues.json --jql="{jql}"',
        '   - args: --output={output_dir}/jira-issues.json --jql="{jql}" ' + STATE_ARG,
    )
    text = text.replace(
        "   - args: --input={output_dir}/extracted-stories --output={output_dir}/test-manifest.json",
        "   - args: --input={output_dir}/extracted-stories "
        "--output={output_dir}/test-manifest.json " + STATE_ARG,
    )
    command.write_text(text)

    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    rendered = excinfo.value.render()
    assert "different jobs" in rendered
    assert "last-writer-wins" in rendered


def test_state_inside_a_foreach_is_refused(basic_root):
    command = basic_root / "commands" / "generate-tests.md"
    text = command.read_text().replace("---\nname: generate-tests", "---\nname: generate-tests\nstate: true")
    text = text.replace(
        "   - output: {output_dir}/extracted-stories",
        "   - output: {output_dir}/extracted-stories\n   - db: " + "{state_db}",
    )
    command.write_text(text)

    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    assert "run in parallel" in excinfo.value.render()


def test_declaring_state_without_using_it_is_reported(basic_root):
    command = basic_root / "commands" / "discover.md"
    command.write_text(command.read_text().replace(" --state={state_db}", ""))
    plan = compile_spec(basic_root)
    assert any("declared but no step references" in note for note in plan.notes)
    assert "state/load" not in plan.files[".github/workflows/discover.yml"]


def test_commands_without_state_emit_no_state_steps(basic_spec_dir):
    text = compile_spec(basic_spec_dir).files[".github/workflows/generate-tests.yml"]
    assert "state/load" not in text
