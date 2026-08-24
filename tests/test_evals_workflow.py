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


def add_judge(root, agent, *, min_pass_rate=None, min_score=None):
    manifest = root / "pipeline.yaml"
    block = f"\nevals:\n  judge: {agent}\n"
    if min_pass_rate is not None:
        block += f"  min-pass-rate: {min_pass_rate}\n"
    if min_score is not None:
        block += f"  min-score: {min_score}\n"
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


def test_a_score_floor_reaches_the_grader(basic_root):
    """The gate a pass rate cannot express: everything passing, all of it answered less well."""
    add_judge(basic_root, AGENT, min_score=4)
    assert "--min-score=4.0" in _grade_command(basic_root)


def test_no_score_floor_unless_one_is_declared(basic_spec_dir):
    assert "--min-score" not in _grade_command(basic_spec_dir)


def test_every_case_gets_somewhere_to_put_a_fixture(basic_spec_dir):
    """A case with a tree of source needs it laid down before the agent job starts."""
    job = suite(basic_spec_dir)["jobs"][f"cases-{AGENT}"]
    command = " ".join(step.get("run", "") for step in job["steps"])
    assert f"--repo-dir=outputs/evals/{AGENT}/repos" in command


def test_a_fixture_does_not_land_where_the_inputs_or_answers_go(basic_spec_dir):
    """One directory per purpose: a checkout written over an answer is a case grading itself."""
    jobs = suite(basic_spec_dir)["jobs"]
    cases = " ".join(step.get("run", "") for step in jobs[f"cases-{AGENT}"]["steps"])
    repos = [word for word in cases.split() if word.startswith("--repo-dir=")][0]
    inputs = [word for word in cases.split() if word.startswith("--output-dir=")][0]
    assert repos.split("=", 1)[1] != inputs.split("=", 1)[1]
    assert repos.split("=", 1)[1] not in json.dumps(jobs[f"run-{AGENT}"]["with"])


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
def adopter_like(consumer_root):
    """A consumer whose profile carries a context — which reaches every inherited agent."""
    context = consumer_root / "contexts" / "codebase.md"
    context.parent.mkdir(exist_ok=True)
    context.write_text(
        "---\nname: codebase\ndescription: What this repo decided\n---\n\nAll DB access is in src/repo.py.\n",
        encoding="utf-8",
    )
    for profile in (consumer_root / "profiles").glob("*.md"):
        text = profile.read_text()
        if "contexts:" in text:
            profile.write_text(text.replace("contexts: [", "contexts: [codebase, ", 1), encoding="utf-8")
        else:
            profile.write_text(text.replace("---\n", "---\ncontexts: [codebase]\n", 1), encoding="utf-8")
    return consumer_root


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


# --- verifying that a change was an improvement ------------------------------
#
# A retro agent can say what to try; it cannot say whether the attempt worked. The suite can, and
# these hold the workflow that lets it: record a baseline off the default branch, compare a
# candidate against it, and refuse a change that broke a case the previous prompt always passed.


def with_history(root, extra=""):
    manifest = root / "pipeline.yaml"
    manifest.write_text(
        manifest.read_text() + "\nhistory:\n  branch: pipeline-history\n" + extra, encoding="utf-8"
    )
    lock = root / ".pipeline" / "pins.lock"
    data = json.loads(lock.read_text())
    for action in ("actions/download-artifact", "actions/upload-artifact"):
        data["external"][action] = {"sha": "0" * 40, "tag": "v5"}
    lock.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return root


def grade_steps(root):
    return suite(root)["jobs"][f"grade-{AGENT}"]["steps"]


def test_without_retention_there_is_nothing_to_compare_against(basic_spec_dir):
    """The comparison needs a baseline, and a baseline needs somewhere durable to have been kept."""
    names = " ".join(
        step.get("name", "") for step in suite(basic_spec_dir)["jobs"][f"grade-{AGENT}"]["steps"]
    )
    assert "Compare" not in names


def test_a_candidates_scores_never_become_the_baseline(basic_root):
    """Otherwise a regression establishes itself as normal simply by being merged."""
    steps = {s.get("name"): s for s in grade_steps(with_history(basic_root))}
    assert steps["Record this run as a baseline"]["if"] == "${{ github.event_name != 'pull_request' }}"
    assert steps["Publish the baseline"]["if"] == "${{ github.event_name != 'pull_request' }}"


def test_the_baseline_is_published_before_the_comparison_can_fail(basic_root):
    """A run on the default branch is the prompt that now ships, whether or not it scored worse.

    Publishing after the comparison would leave a regressed merge unrecorded, and the next change
    comparing against something two versions old.
    """
    order = [s.get("name") for s in grade_steps(with_history(basic_root)) if s.get("name")]
    assert order.index("Publish the baseline") < order.index("Compare against the prompt this replaces")


def test_the_comparison_fingerprints_the_prompt_it_scored(basic_root):
    """Two runs are only comparable when nothing that could move behaviour differs."""
    steps = {s.get("name"): s for s in grade_steps(with_history(basic_root))}
    assert (
        f"--prompt-file=.github/workflows/aw-{AGENT}.md"
        in steps["Compare against the prompt this replaces"]["run"]
    )


def test_the_gate_fires_only_on_a_pull_request(basic_root):
    """A scheduled baseline run compares against the previous prompt every night.

    Failing it would report the same regression forever, and a red build nobody can clear is one
    people stop reading.
    """
    gate = next(s for s in grade_steps(with_history(basic_root)) if s.get("name", "").startswith("Refuse"))
    assert "github.event_name == 'pull_request'" in gate["if"]
    assert "steps.compare.outputs.regressed != ''" in gate["if"]
    assert "exit 1" in gate["run"]


def test_recording_a_baseline_needs_the_write_it_makes(basic_root):
    assert suite(with_history(basic_root))["jobs"][f"grade-{AGENT}"]["permissions"]["contents"] == "write"


def test_eval_jobs_that_write_nothing_ask_for_nothing(basic_spec_dir):
    for name, job in suite(basic_spec_dir)["jobs"].items():
        if "permissions" in job:
            assert job["permissions"]["contents"] == "read", name


def test_a_baseline_schedule_re_runs_an_unchanged_prompt(basic_root):
    """Which is the only way a noise floor is ever measured: one run of a prompt has no spread."""
    root = with_history(basic_root, extra="\nevals:\n  baseline: '0 3 * * *'\n")
    assert triggers(root)["schedule"] == [{"cron": "0 3 * * *"}]


def test_no_schedule_unless_somebody_asks_for_one(basic_root):
    """Repeats cost credits. Measuring the noise floor is a decision, not a default."""
    assert "schedule" not in triggers(with_history(basic_root))


# --- verifying a customized inherited agent ----------------------------------
#
# Upstream evaluated the prompt upstream wrote. Add a guardrail, a skill, a context or a tuned dial
# and what runs here is a different prompt that no suite anywhere describes. These cover the path
# that lets a consumer verify their own customization.


@pytest.fixture
def consumer_with_cases(consumer_root):
    """A consumer that customized an inherited agent and wrote a case for it."""
    cases = consumer_root / "evals" / "review" / "reviewer" / "cases"
    cases.mkdir(parents=True)
    (cases / "knows-our-layer.json").write_text(
        json.dumps({"input": {"diff": "x"}, "expect": {"schema": ["verdict"]}}), encoding="utf-8"
    )
    manifest = consumer_root / "pipeline.yaml"
    manifest.write_text(manifest.read_text() + "\nevals:\n  inherited: [review/reviewer]\n", encoding="utf-8")
    return consumer_root


def test_an_inherited_agent_is_not_evaluated_here_by_default(consumer_root):
    """Upstream already evaluates it, and re-running their cases would pay to re-test their lens."""
    from lockstep.emit.evals import agents_with_cases
    from lockstep.spec.load import load_spec

    assert "review/reviewer" not in agents_with_cases(load_spec(consumer_root))


def test_listing_it_runs_upstreams_cases_and_your_own(consumer_with_cases):
    """Upstream's are the regression contract; yours test what the customization was for."""
    job = suite(consumer_with_cases)["jobs"]["cases-review-reviewer"]
    staged = next(s for s in job["steps"] if s.get("name") == "Stage the cases")["run"]
    assert ".pipeline/inherited/review/evals/reviewer/cases" in staged
    assert "evals/review/reviewer/cases" in staged


def test_the_cases_are_staged_because_inherited_ones_do_not_survive_the_job(consumer_with_cases):
    """`save`/`restore` carry the output directory. `.pipeline/` is resolved state, not source."""
    jobs = suite(consumer_with_cases)["jobs"]
    for name in ("cases-review-reviewer", "grade-review-reviewer"):
        commands = " ".join(s.get("run", "") for s in jobs[name]["steps"])
        assert "--cases=outputs/evals/review-reviewer/cases" in commands


def test_a_suite_over_inherited_cases_materializes_them_first(consumer_with_cases):
    """They are gitignored, so without a fetch the directory is simply not there at run time."""
    job = suite(consumer_with_cases)["jobs"]["cases-review-reviewer"]
    names = [s.get("name", "") for s in job["steps"]]
    assert "Fetch inherited pipelines" in names
    # Which needs the compiler, and the executor image deliberately does not carry one.
    assert "container" not in job


def test_a_suite_over_this_repositorys_own_agents_needs_no_fetch(basic_spec_dir):
    job = suite(basic_spec_dir)["jobs"][f"cases-{AGENT}"]
    assert "Fetch inherited pipelines" not in [s.get("name", "") for s in job["steps"]]
    assert "container" in job


def test_doctor_names_a_customization_nothing_verifies(consumer_root):
    """The failure is silent: everything compiles, everything passes, the lens does something else."""
    from lockstep.checks import doctor
    from lockstep.spec.load import load_spec

    finding = next(f for f in doctor(load_spec(consumer_root), consumer_root).findings if f.code == "DOC025")
    assert "customized here and nothing evaluates them" in finding.message
    assert "evals.inherited" in (finding.hint or "")


def test_doctor_is_quiet_once_the_customization_is_verified(consumer_with_cases):
    from lockstep.checks import doctor
    from lockstep.spec.load import load_spec

    codes = {f.code for f in doctor(load_spec(consumer_with_cases), consumer_with_cases).findings}
    assert "DOC025" not in codes


def test_one_warning_per_cause_not_one_per_agent(adopter_like):
    """A profile context reaches every agent. Thirteen identical warnings is a report people mute."""
    from lockstep.checks import doctor
    from lockstep.spec.load import load_spec

    findings = [f for f in doctor(load_spec(adopter_like), adopter_like).findings if f.code == "DOC025"]
    assert len(findings) == 1


# --- the eval suite runs the agent the way the pipeline does ------------------


def test_the_suite_hands_the_agent_the_secrets_it_declares(basic_spec_dir):
    """A reusable workflow whose required secret is not passed fails at the call.

    Without this a suite for any agent with an MCP credential could not run at all — and it would
    look like the agent failing rather than the suite never reaching it.
    """
    agent = yaml.safe_load(
        compile_spec(basic_spec_dir).files[f".github/workflows/aw-{AGENT}.md"].split("---")[1]
    )
    required = list(((agent.get("on") or agent.get(True))["workflow_call"] or {}).get("secrets") or [])
    assert required, "this fixture's agent needs no secrets; the test asserts nothing"
    assert sorted(suite(basic_spec_dir)["jobs"][f"run-{AGENT}"]["secrets"]) == sorted(required)
