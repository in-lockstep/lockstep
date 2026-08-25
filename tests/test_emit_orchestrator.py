"""Lowering commands onto job graphs: fusion, ordering, fan-out, conditions, least privilege."""

from __future__ import annotations

import pytest
import yaml

from lockstep.emit import compile_spec
from lockstep.emit.orchestrator import group_steps, runner_for
from lockstep.errors import EmitError, SpecError
from lockstep.spec.load import load_spec
from lockstep.spec.parse import parse_steps


def workflow(root, name):
    plan = compile_spec(root)
    return yaml.safe_load(plan.files[f".github/workflows/{name}"])


def jobs_of(root, name):
    return workflow(root, name)["jobs"]


def test_consecutive_script_steps_fuse_into_one_job(basic_spec_dir):
    jobs = jobs_of(basic_spec_dir, "discover.yml")
    assert len(jobs) == 1
    names = [step.get("name") for step in next(iter(jobs.values()))["steps"]]
    assert "Discover API surface" in names
    assert "Discover UI structure" in names


def test_agent_step_breaks_fusion(basic_spec_dir):
    jobs = jobs_of(basic_spec_dir, "generate-tests.yml")
    assert list(jobs) == [
        "discover-application-structure",
        "fetch-issues",
        # Every agent is preceded by a scan of what it is about to read, because the shipped
        # baseline guardrail enforces one. It is a job rather than a step so that a finding stops
        # the agent from starting.
        "scan-extract-stories-from-each-issue",
        "extract-stories-from-each-issue",
        "verify-extract-stories-from-each-issue",
        "build-test-manifest",
    ]


def test_jobs_chain_in_step_order(basic_spec_dir):
    jobs = jobs_of(basic_spec_dir, "generate-tests.yml")
    assert "needs" not in jobs["discover-application-structure"]
    assert jobs["fetch-issues"]["needs"] == "discover-application-structure"
    assert jobs["scan-extract-stories-from-each-issue"]["needs"] == "fetch-issues"
    assert jobs["extract-stories-from-each-issue"]["needs"] == [
        "fetch-issues",
        "scan-extract-stories-from-each-issue",
    ]
    assert jobs["build-test-manifest"]["needs"] == "verify-extract-stories-from-each-issue"


def test_foreach_becomes_a_matrix_fed_by_the_producing_job(basic_spec_dir):
    jobs = jobs_of(basic_spec_dir, "generate-tests.yml")
    producer = jobs["fetch-issues"]
    consumer = jobs["extract-stories-from-each-issue"]

    output_name = next(iter(producer["outputs"]))
    assert "fanout" in producer["outputs"][output_name]
    assert any("pipeline-exec fanout" in step.get("run", "") for step in producer["steps"])

    assert consumer["strategy"]["max-parallel"] == 3
    assert consumer["strategy"]["fail-fast"] is False
    assert consumer["strategy"]["matrix"]["item"] == (
        "${{ fromJSON(needs.fetch-issues.outputs." + output_name + ") }}"
    )


def test_agent_job_calls_the_compiled_lock_file(basic_spec_dir):
    job = jobs_of(basic_spec_dir, "generate-tests.yml")["extract-stories-from-each-issue"]
    assert job["uses"] == "./.github/workflows/aw-story-extractor.lock.yml"
    assert job["with"]["item"] == "${{ toJSON(matrix.item) }}"
    assert job["with"]["output_path"] == "outputs/extracted-stories/${{ matrix.item.key }}.json"


def test_agent_job_receives_only_the_secrets_its_workflow_declares(basic_spec_dir):
    """Never `secrets: inherit`, and never a secret the callee did not ask for.

    Two declare the callee's needs, not one. This compiler writes the MCP-derived secrets into the
    agent's frontmatter; `gh aw compile` adds the engine credential to the lock file it generates.
    A called workflow inherits nothing, so the caller passes both — without the engine one the run
    authorizes, activates, and dies with "None of the following secrets are set".
    """
    from lockstep.emit.agentic import ENGINE_SECRET

    job = jobs_of(basic_spec_dir, "generate-tests.yml")["extract-stories-from-each-issue"]
    assert job["secrets"] == {
        "JIRA_API_TOKEN": "${{ secrets.JIRA_API_TOKEN }}",
        "ANTHROPIC_API_KEY": "${{ secrets.ANTHROPIC_API_KEY }}",
    }
    assert "APP_PASSWORD" not in job["secrets"]
    assert set(job["secrets"]) & set(ENGINE_SECRET.values()), "no engine credential is handed over"


def test_condition_becomes_a_job_level_if(basic_spec_dir):
    job = jobs_of(basic_spec_dir, "generate-tests.yml")["discover-application-structure"]
    assert job["if"] == "${{ inputs.skip_discovery != true }}"


def test_local_only_steps_are_skipped_with_a_note(basic_spec_dir):
    plan = compile_spec(basic_spec_dir)
    assert any("Deploy the app locally" in note for note in plan.notes)
    text = plan.files[".github/workflows/generate-tests.yml"]
    assert "deploy-local.sh" not in text


def step_named(job, name):
    return next(step for step in job["steps"] if step.get("name") == name)


def test_parameters_and_profile_values_are_substituted(basic_spec_dir):
    jobs = jobs_of(basic_spec_dir, "generate-tests.yml")
    run = step_named(jobs["fetch-issues"], "Fetch issues from Jira")["run"]
    assert '--jql="${{ inputs.jql }}"' in run
    assert "{jql}" not in run

    discover = jobs_of(basic_spec_dir, "discover.yml")["discover-api-surface"]
    assert "--api-url=${{ vars.API_URL }}" in step_named(discover, "Discover API surface")["run"]


def test_profile_env_is_exported_under_both_prefixes(basic_spec_dir):
    env = jobs_of(basic_spec_dir, "discover.yml")["discover-api-surface"]["env"]
    assert env["MY_API_URL"] == "${{ vars.API_URL }}"
    assert env["PROFILE_API_URL"] == "${{ vars.API_URL }}"
    assert env["MY_PASSWORD"] == "${{ secrets.APP_PASSWORD }}"


def test_jobs_consuming_secrets_declare_an_environment(basic_spec_dir):
    job = jobs_of(basic_spec_dir, "discover.yml")["discover-api-surface"]
    assert job["environment"] == "my-app-staging"


def test_workflow_defaults_to_read_only_permissions(basic_spec_dir):
    assert workflow(basic_spec_dir, "generate-tests.yml")["permissions"] == {"contents": "read"}


def test_hooks_become_steps_with_the_right_gating(basic_spec_dir):
    steps = jobs_of(basic_spec_dir, "generate-tests.yml")["build-test-manifest"]["steps"]
    failure_step = next(s for s in steps if s.get("name", "").startswith("on-failure"))
    assert failure_step["if"] == "${{ failure() }}"
    save = next(s for s in steps if s.get("id") == "save-workspace")
    assert save["if"] == "${{ always() }}"


def test_schedule_trigger_is_emitted(basic_spec_dir):
    data = workflow(basic_spec_dir, "generate-tests.yml")
    # YAML 1.1 readers parse the `on` key as the boolean True; both spellings are the same key.
    triggers = data.get("on") or data.get(True)
    assert triggers["schedule"] == [{"cron": "0 2 * * 1-5"}]
    assert "workflow_dispatch" in triggers
    assert "workflow_call" in triggers


@pytest.mark.parametrize(
    ("script", "expected"),
    [("a.py", "uv run python3"), ("a.sh", "bash"), ("a.ts", "npx tsx"), ("a.js", "node")],
)
def test_runner_is_chosen_by_extension(script, expected):
    assert runner_for(script) == expected


def test_unknown_extension_is_a_compile_error():
    with pytest.raises(EmitError):
        runner_for("a.exe")


def test_condition_on_an_undeclared_parameter_fails(basic_root):
    command = basic_root / "commands" / "generate-tests.md"
    command.write_text(command.read_text().replace("(if not --skip-discovery)", "(if --nonexistent)"))
    with pytest.raises(SpecError) as excinfo:
        compile_spec(basic_root)
    assert "nonexistent" in str(excinfo.value)


def test_condition_change_splits_a_fused_group():
    steps = parse_steps(
        """
1. **A** → script: a.py
2. **B** → script: b.py
   (if --pdf)
3. **C** → script: c.py
   (if --pdf)
""",
        location="t.md",
    )
    groups = group_steps(steps, fuse=True)
    assert [len(g.steps) for g in groups] == [1, 2]


def test_fusion_can_be_disabled(basic_root):
    manifest = basic_root / "pipeline.yaml"
    manifest.write_text(manifest.read_text().replace("fuse-script-steps: true", "fuse-script-steps: false"))
    assert len(jobs_of(basic_root, "discover.yml")) == 2


def test_load_spec_is_pure_of_environment(basic_spec_dir, monkeypatch):
    monkeypatch.setenv("APP_URL", "http://should-not-be-read")
    spec = load_spec(basic_spec_dir)
    assert spec.profiles["my-app"].values["url"] == "${APP_URL}"


# --- extension builtins ----------------------------------------------------


def test_an_undeclared_extension_builtin_is_refused(basic_root):
    command = basic_root / "commands" / "repair.md"
    command.write_text(command.read_text().replace("builtin: collect-failures", "builtin: jira-fetch"))
    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    assert "extensions.builtins" in excinfo.value.render()


def test_a_declared_extension_builtin_compiles(basic_root):
    """The compiler cannot install an extension, so declaring it is how the spec vouches for it."""
    manifest = basic_root / "pipeline.yaml"
    manifest.write_text(
        manifest.read_text() + "\nextensions:\n  builtins: [jira-fetch]\n  packages: [ext==1.0]\n"
    )
    command = basic_root / "commands" / "repair.md"
    command.write_text(command.read_text().replace("builtin: collect-failures", "builtin: jira-fetch"))

    jobs = jobs_of(basic_root, "repair.yml")
    runs = " ".join(step.get("run", "") for job in jobs.values() for step in job.get("steps", []) or [])
    assert "pipeline-exec jira-fetch" in runs
