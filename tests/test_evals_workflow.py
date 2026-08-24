"""The workflow that runs an agent against its eval cases.

`lockstep lint` refuses an agent with no cases on the grounds that an agent nobody evaluates cannot
be changed safely. That argument only held once something ran them.

The property these tests protect above the others: an eval runs the agent through the *same*
contract every other agent step uses — `input_path` in, `output_path` out, the same compiled
workflow. A suite that ran the agent some other way would be evidence about the harness.
"""

from __future__ import annotations

import json

import pytest
import yaml

from lockstep.emit import compile_spec
from lockstep.emit.evals import PROMPT_PATHS, agents_with_cases
from lockstep.spec.load import load_spec

SUITE = ".github/workflows/evals.yml"
AGENT = "story-extractor"


def suite(root):
    return yaml.safe_load(compile_spec(root).files[SUITE])


def triggers(root):
    workflow = suite(root)
    return workflow.get("on") or workflow.get(True)


def add_judge(root, agent, *, min_pass_rate=None):
    manifest = root / "pipeline.yaml"
    block = f"\nevals:\n  judge: {agent}\n"
    if min_pass_rate is not None:
        block += f"  min-pass-rate: {min_pass_rate}\n"
    manifest.write_text(manifest.read_text() + block, encoding="utf-8")


# --- which agents get a suite ------------------------------------------------


def test_an_agent_with_cases_gets_one(basic_spec_dir):
    assert AGENT in agents_with_cases(load_spec(basic_spec_dir))
    assert f"run-{AGENT}" in suite(basic_spec_dir)["jobs"]


def test_an_agent_with_no_cases_gets_nothing(basic_root):
    """Nothing to run. LNT001 is what complains about that; the suite just has no job."""
    for case in (basic_root / "evals" / AGENT / "cases").glob("*.json"):
        case.unlink()
    assert agents_with_cases(load_spec(basic_root)) == []
    assert SUITE not in compile_spec(basic_root).files


def test_an_inherited_agent_is_evaluated_by_whoever_published_it(consumer_root):
    """Running somebody else's cases re-tests their lens from the outside, and pays for it."""
    spec = load_spec(consumer_root)
    assert any(agent.inherited_from for agent in spec.agents.values())
    assert all("/" not in name for name in agents_with_cases(spec))


# --- the agent runs the ordinary way ----------------------------------------


def test_the_suite_calls_the_same_compiled_workflow_the_pipeline_calls(basic_spec_dir):
    job = suite(basic_spec_dir)["jobs"][f"run-{AGENT}"]
    assert job["uses"] == f"./.github/workflows/aw-{AGENT}.lock.yml"
    assert set(job["with"]) == {"input_path", "output_path"}


def test_one_run_per_case(basic_spec_dir):
    job = suite(basic_spec_dir)["jobs"][f"run-{AGENT}"]
    assert job["strategy"]["matrix"]["case"] == f"${{{{ fromJSON(needs.cases-{AGENT}.outputs.cases) }}}}"
    assert job["strategy"]["fail-fast"] is False


def test_a_failing_case_does_not_take_the_report_down_with_it(basic_spec_dir):
    """A case whose agent run failed is a case the suite should report on."""
    assert suite(basic_spec_dir)["jobs"][f"grade-{AGENT}"]["if"] == "${{ !cancelled() }}"


# --- the trigger -------------------------------------------------------------


def test_the_suite_never_runs_on_every_push(basic_spec_dir):
    """It spends credits. Dispatch, or a change to the prompt layers it covers."""
    on = triggers(basic_spec_dir)
    assert "push" not in on
    assert "workflow_dispatch" in on


def test_it_runs_when_a_prompt_layer_changes(basic_spec_dir):
    paths = triggers(basic_spec_dir)["pull_request"]["paths"]
    assert set(paths) == set(PROMPT_PATHS)


def test_the_trigger_can_be_turned_off(basic_root):
    manifest = basic_root / "pipeline.yaml"
    manifest.write_text(manifest.read_text() + "\nevals:\n  on-prompt-change: false\n", encoding="utf-8")
    assert "pull_request" not in triggers(basic_root)


# --- the judge is optional ---------------------------------------------------


def test_without_a_judge_there_is_no_judging_job(basic_spec_dir):
    jobs = suite(basic_spec_dir)["jobs"]
    assert not [name for name in jobs if name.startswith(("judge-", "prep-"))]


def test_without_a_judge_grading_asks_for_no_verdicts(basic_spec_dir):
    run = _grade_command(basic_spec_dir)
    assert "--judgements" not in run


def test_declaring_a_judge_adds_the_pairing_and_judging_jobs(basic_root):
    add_judge(basic_root, AGENT)
    jobs = suite(basic_root)["jobs"]
    assert f"prep-{AGENT}" in jobs
    assert f"judge-{AGENT}" in jobs
    assert "--judgements" in _grade_command(basic_root)


def test_the_judge_is_skipped_when_no_case_carries_a_rubric(basic_root):
    """Not a failure — a suite whose cases are all deterministic has nothing to judge."""
    add_judge(basic_root, AGENT)
    assert suite(basic_root)["jobs"][f"judge-{AGENT}"]["if"] == (
        f"${{{{ needs.prep-{AGENT}.outputs.pending != '[]' }}}}"
    )


def test_a_judge_naming_an_agent_that_does_not_exist_is_ignored(basic_root):
    """A typo should not silently produce a job calling a workflow nobody generated."""
    add_judge(basic_root, "no-such-agent")
    assert f"judge-{AGENT}" not in suite(basic_root)["jobs"]


def test_a_minimum_pass_rate_reaches_the_grader(basic_root):
    add_judge(basic_root, AGENT, min_pass_rate=0.9)
    assert "--min-pass-rate=0.9" in _grade_command(basic_root)


def _grade_command(root):
    job = suite(root)["jobs"][f"grade-{AGENT}"]
    return " ".join(step.get("run", "") for step in job["steps"])


# --- it is real output ------------------------------------------------------


def test_the_suite_is_generated_output_like_everything_else(basic_spec_dir):
    files = compile_spec(basic_spec_dir).files
    attributes = files[".github/workflows/.gitattributes"]
    assert "evals.yml linguist-generated=true" in attributes


def test_every_command_the_suite_runs_is_one_the_compiler_knows(basic_spec_dir):
    from lockstep.emit.builtins import AVAILABLE, INTERNAL

    for job in suite(basic_spec_dir)["jobs"].values():
        for step in job.get("steps", []) or []:
            run = step.get("run", "")
            if run.startswith("pipeline-exec "):
                assert run.split()[1] in AVAILABLE | INTERNAL, run


@pytest.fixture
def consumer_root(tmp_path):
    import shutil

    from lockstep.lifecycle import fetch
    from lockstep.spec.load import load_manifest_only

    fixtures = __import__("pathlib").Path(__file__).parent / "fixtures"
    for name in ("upstream-standards", "upstream-review", "consumer"):
        shutil.copytree(fixtures / name, tmp_path / name)
    root = tmp_path / "consumer"
    fetch(load_manifest_only(root), root)
    return root


def test_the_grade_report_lands_somewhere_a_later_step_can_read(basic_spec_dir):
    assert f"--output=outputs/evals/{AGENT}.json" in _grade_command(basic_spec_dir)


def test_case_inputs_and_answers_do_not_share_a_directory(basic_spec_dir):
    """An answer written over its own input is a case that grades itself."""
    run_job = suite(basic_spec_dir)["jobs"][f"run-{AGENT}"]["with"]
    assert run_job["input_path"] != run_job["output_path"]
    assert json.dumps(run_job).count("/inputs/") == 1
