"""Pinning and ejection: keeping what runs equal to what was reviewed."""

from __future__ import annotations

import json

import pytest
import yaml

from lockstep.emit import compile_spec
from lockstep.emit.agentic import AGENT_CALLER_PERMISSIONS
from lockstep.emit.writer import check_plan, write_plan
from lockstep.errors import LockstepError
from lockstep.lifecycle import Ejection, eject, load_pins, pin, stale_ejections, uneject, write_pins
from lockstep.spec.load import load_spec

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


# --- pinning ---------------------------------------------------------------


def test_pinning_records_the_supplied_commit_and_digest(basic_root):
    data, notes, _ = pin(load_spec(basic_root), basic_root, actions_sha=SHA, exec_digest=DIGEST)
    write_pins(basic_root, data)

    stored = load_pins(basic_root)["capabilities"]
    assert stored["actions"]["sha"] == SHA
    assert stored["actions"]["tag"] == "actions-v1.6.2"
    assert stored["exec"]["digest"] == DIGEST
    assert any("supplied" in note for note in notes)


def test_offline_pinning_contacts_nothing(basic_root):
    """`--offline` must not reach the network, so it can run in a sandbox or an air-gapped build."""
    before = load_pins(basic_root)["capabilities"]["actions"]["sha"]
    data, _, _ = pin(load_spec(basic_root), basic_root, offline=True)
    assert data["capabilities"]["actions"]["sha"] == before


def test_pinning_reports_an_unpinned_image(basic_root):
    pins = load_pins(basic_root)
    del pins["capabilities"]["exec"]["digest"]
    write_pins(basic_root, pins)
    _, notes, unresolved = pin(load_spec(basic_root), basic_root, offline=True)
    assert any("--exec-digest" in problem for problem in unresolved)


def test_pinned_output_makes_the_compiler_emit_that_commit(basic_root):
    data, _, _ = pin(load_spec(basic_root), basic_root, actions_sha=SHA, exec_digest=DIGEST)
    write_pins(basic_root, data)
    text = compile_spec(basic_root).files[".github/workflows/discover.yml"]
    assert f"@{SHA}" in text
    assert DIGEST in text


# --- ejection --------------------------------------------------------------


TARGET = ".github/workflows/discover.yml"


def eject_target(root):
    plan = compile_spec(root)
    write_plan(root, plan)
    return eject(root, TARGET, plan.files[TARGET])


def test_ejecting_records_the_file_and_snapshots_its_generation(basic_root):
    base = eject_target(basic_root)
    assert TARGET in Ejection.load(basic_root).files
    assert base.is_file()
    assert base.read_text() == (basic_root / TARGET).read_text()


def test_the_compiler_stops_maintaining_an_ejected_file(basic_root):
    eject_target(basic_root)
    (basic_root / TARGET).write_text("# hand maintained\n", encoding="utf-8")

    write_plan(basic_root, compile_spec(basic_root))
    assert (basic_root / TARGET).read_text() == "# hand maintained\n"
    assert check_plan(basic_root, compile_spec(basic_root)).clean


def test_a_fork_is_reported_once_its_source_moves_on(basic_root):
    """A silent fork becomes indistinguishable from an intentional one; this keeps the debt visible."""
    eject_target(basic_root)
    assert stale_ejections(basic_root, compile_spec(basic_root).files) == []

    command = basic_root / "commands" / "discover.md"
    command.write_text(command.read_text().replace("**Discover UI structure**", "**Map the UI**"))
    assert stale_ejections(basic_root, compile_spec(basic_root).files) == [TARGET]


def test_ejecting_twice_is_refused(basic_root):
    eject_target(basic_root)
    with pytest.raises(LockstepError):
        eject(basic_root, TARGET, "x")


def test_ejecting_something_ungenerated_is_refused(basic_root):
    with pytest.raises(LockstepError):
        eject(basic_root, "README.md", "x")


def test_unejecting_hands_the_file_back(basic_root):
    eject_target(basic_root)
    uneject(basic_root, TARGET)
    assert Ejection.load(basic_root).files == []
    assert not (basic_root / ".pipeline/eject-base" / TARGET).exists()

    (basic_root / TARGET).write_text("# stale\n", encoding="utf-8")
    assert not check_plan(basic_root, compile_spec(basic_root)).clean


def test_unejecting_something_not_ejected_is_refused(basic_root):
    with pytest.raises(LockstepError):
        uneject(basic_root, TARGET)


def test_the_registry_explains_itself(basic_root):
    eject_target(basic_root)
    text = (basic_root / ".pipeline/ejected.yaml").read_text()
    assert text.startswith("#")
    assert yaml.safe_load(text)["files"] == [TARGET]


# --- generated CI ----------------------------------------------------------


def test_the_generated_repo_gates_itself(basic_spec_dir):
    ci = yaml.safe_load(compile_spec(basic_spec_dir).files[".github/workflows/pipeline-ci.yml"])
    assert set(ci["jobs"]) == {"drift", "lint", "doctor", "scripts"}
    drift = " ".join(step.get("run", "") for step in ci["jobs"]["drift"]["steps"])
    assert "--check" in drift
    assert "--fail-on-blocking" in drift


def test_the_gate_installs_the_pinned_compiler_rather_than_the_project(basic_spec_dir):
    """A check must not execute project-defined build hooks in order to run."""
    ci = yaml.safe_load(compile_spec(basic_spec_dir).files[".github/workflows/pipeline-ci.yml"])
    install = " ".join(step.get("run", "") for step in ci["jobs"]["drift"]["steps"])
    assert "uv tool install" in install
    assert "uv sync" not in install


def test_ci_runs_on_spec_changes(basic_spec_dir):
    ci = yaml.safe_load(compile_spec(basic_spec_dir).files[".github/workflows/pipeline-ci.yml"])
    paths = (ci.get("on") or ci.get(True))["pull_request"]["paths"]
    assert "agents/**" in paths
    assert "overlays/**" in paths


def test_ci_runs_on_the_tests_it_runs(basic_spec_dir):
    """The scripts job runs `pytest tests/`; a change to those tests has to reach it."""
    ci = yaml.safe_load(compile_spec(basic_spec_dir).files[".github/workflows/pipeline-ci.yml"])
    assert "tests/**" in (ci.get("on") or ci.get(True))["pull_request"]["paths"]


def test_watched_paths_join_the_trigger(basic_root):
    """Normally the spec is the only input. A repository that builds its own compiler has two."""
    manifest = basic_root / "pipeline.yaml"
    manifest.write_text(
        manifest.read_text().replace(
            "    profiles: [my-app]",
            "    profiles: [my-app]\n    watch: [src/**, pyproject.toml]",
        ),
        encoding="utf-8",
    )
    ci = yaml.safe_load(compile_spec(basic_root).files[".github/workflows/pipeline-ci.yml"])
    paths = (ci.get("on") or ci.get(True))["pull_request"]["paths"]
    assert {"src/**", "pyproject.toml"} <= set(paths)


def test_ci_is_read_only(basic_spec_dir):
    ci = yaml.safe_load(compile_spec(basic_spec_dir).files[".github/workflows/pipeline-ci.yml"])
    assert ci["permissions"] == {"contents": "read"}
    for job in ci["jobs"].values():
        assert job.get("permissions", {}).get("contents") == "read"
        assert set(job.get("permissions", {})) <= {"contents"}


def test_pins_are_recorded_as_json_for_review(basic_root):
    data, _, _ = pin(load_spec(basic_root), basic_root, actions_sha=SHA, exec_digest=DIGEST)
    path = write_pins(basic_root, data)
    assert json.loads(path.read_text())["capabilities"]["actions"]["sha"] == SHA


# --- report publishing -----------------------------------------------------


def report_job(root):
    data = yaml.safe_load(compile_spec(root).files[".github/workflows/validate.yml"])
    return data["jobs"]["render-and-publish-the-report"]


def test_a_report_step_publishes_when_the_profile_says_where(basic_spec_dir):
    """Artifacts expire; a dashboard nobody can open in three months cannot show a trend."""
    publish = next(
        step for step in report_job(basic_spec_dir)["steps"] if "publish-report" in str(step.get("uses"))
    )
    assert publish["with"]["branch"] == "reports"
    assert publish["with"]["source"] == "outputs/runs/current"
    assert publish["with"]["retain"] == "30"


def test_the_report_is_published_even_when_the_run_failed(basic_spec_dir):
    """A failing run is exactly the one whose report someone needs to read."""
    publish = next(
        step for step in report_job(basic_spec_dir)["steps"] if "publish-report" in str(step.get("uses"))
    )
    assert publish["if"] == "${{ always() }}"


def test_publishing_is_the_only_write_the_pipeline_performs(basic_spec_dir):
    data = yaml.safe_load(compile_spec(basic_spec_dir).files[".github/workflows/validate.yml"])
    writers = [
        name
        for name, job in data["jobs"].items()
        if (job.get("permissions") or {}).get("contents") == "write"
    ]
    assert writers == ["render-and-publish-the-report"]
    assert data["permissions"] == {"contents": "read"}


def test_no_publishing_without_a_declared_branch(basic_root):
    profile = basic_root / "profiles" / "my-app.md"
    profile.write_text(profile.read_text().replace("    branch: reports\n", "    branch: ''\n"))
    text = compile_spec(basic_root).files[".github/workflows/validate.yml"]
    assert "publish-report" not in text
    assert "contents: write" not in text


# --- proposing generated work ----------------------------------------------


def example_workflow():
    from pathlib import Path

    example = Path(__file__).parent.parent / "examples" / "httpbin"
    return yaml.safe_load(compile_spec(example).files[".github/workflows/validate-api.yml"])


def test_generated_artifacts_are_proposed_rather_than_committed():
    """The agent writes them once, a human reviews once, and every run after that costs nothing."""
    job = example_workflow()["jobs"]["propose-generated-artifacts"]
    propose = next(step for step in job["steps"] if step.get("id") == "propose")
    assert propose["with"]["source"] == "outputs/test-scripts"
    assert propose["with"]["destination"] == "test-scripts"
    assert propose["with"]["branch"] == "pipeline/contract-tests"


def test_only_publishing_jobs_run_code_with_a_write_token():
    """Two jobs write: one publishes a report, one opens a pull request. Nothing else executes
    anything while holding one.

    Agent-calling jobs hold `issues: write` as well, and they are a different thing. A job with
    `uses:` and no `steps:` runs no code, so it cannot spend a permission — it can only hand it to
    the workflow it calls, whose own agent job is `read-all` and is asserted separately. gh-aw's
    generated `conclusion` and `safe_outputs` jobs require it, and without it GitHub refuses the
    whole workflow at startup.
    """
    workflow = example_workflow()
    executing = {
        name: job["permissions"]
        for name, job in workflow["jobs"].items()
        if "write" in str(job.get("permissions", "")) and "steps" in job
    }
    assert set(executing) == {"render-and-publish-the-report", "propose-generated-artifacts"}
    assert executing["propose-generated-artifacts"] == {
        "contents": "write",
        "pull-requests": "write",
    }

    # And the pass-through grant is exactly the contract, never wider.
    for name, job in workflow["jobs"].items():
        if "uses" in job and "write" in str(job.get("permissions", "")):
            assert job["permissions"] == AGENT_CALLER_PERMISSIONS, name


def test_test_execution_never_shares_a_job_with_a_write_token():
    """A write token in the job that runs test scripts would widen the blast radius for nothing."""
    workflow = example_workflow()
    for name, job in workflow["jobs"].items():
        if "write" not in str(job.get("permissions", "")):
            continue
        runs = " ".join(step.get("run", "") for step in job.get("steps", []))
        assert "test-runner" not in runs, f"{name} both executes tests and holds a write token"


def test_the_agent_that_generates_them_can_write_nothing():
    from pathlib import Path

    example = Path(__file__).parent.parent / "examples" / "httpbin"
    agent = compile_spec(example).files[".github/workflows/aw-test-writer.md"]
    front = yaml.safe_load(agent.split("---")[1])
    assert front["permissions"] == {"actions": "read", "contents": "read"}, "the agent can write"


def test_no_proposal_job_without_a_declared_destination(basic_spec_dir):
    workflow = yaml.safe_load(compile_spec(basic_spec_dir).files[".github/workflows/validate.yml"])
    assert "propose-generated-artifacts" not in workflow["jobs"]


def test_a_work_item_reference_reaches_the_proposing_action(tmp_path):
    """A commit nobody can trace back to what asked for it is what `issue-from` prevents.

    The value is a path rather than a string because the parameter is what somebody typed and the
    file is what the tracker answered — a run invoked with `412`, or with a URL, records `#412`.
    """
    import shutil
    from pathlib import Path

    root = tmp_path / "httpbin"
    shutil.copytree(Path(__file__).parent.parent / "examples" / "httpbin", root)
    command = next(p for p in (root / "commands").glob("*.md") if "propose:" in p.read_text())
    command.write_text(
        command.read_text().replace(
            "  propose:\n", '  propose:\n    issue-from: "{output_dir}/issue.json"\n', 1
        ),
        encoding="utf-8",
    )
    workflow = yaml.safe_load(compile_spec(root).files[".github/workflows/validate-api.yml"])
    step = workflow["jobs"]["propose-generated-artifacts"]["steps"][-1]
    assert step["with"]["issue-from"] == "outputs/issue.json"
