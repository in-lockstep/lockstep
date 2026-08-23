"""Gating a step on something an earlier step decided.

`(if --flag)` asks about a workflow input, which is known before anything runs. That is enough for
"skip the slow part", and not enough for anything the pipeline works out for itself — which review
aspects a comment asked for, which items are already covered, whether a check found anything. This
is the other form, and the failure it exists to prevent is a job silently not running because the
value it was gated on never arrived.
"""

from __future__ import annotations

import pytest
import yaml

from lockstep.conformance import simulate
from lockstep.emit import compile_spec
from lockstep.errors import SpecError
from lockstep.spec.parse import parse_steps

STEPS = """
1. **Decide** → script: scripts/fetch-issues.py
   - id: decide
   - emits: chosen
   - args: --output={output_dir}/chosen.json

2. **Do the first thing** → agent: story-extractor
   (if alpha in {decide.chosen})
   - output: {output_dir}/alpha.json

3. **Do the second thing** → agent: story-extractor
   (if beta in {decide.chosen})
   - output: {output_dir}/beta.json

4. **Finish** → script: scripts/save-manifest.py
   - id: finish
   - args: --output={output_dir}/done.json
"""


def write_command(root, body=STEPS):
    path = root / "commands" / "branching.md"
    path.write_text(
        "---\nname: branching\ndescription: Two conditional steps\nguardrails: [common]\n---\n"
        "\n## Steps\n" + body,
        encoding="utf-8",
    )
    manifest = root / "pipeline.yaml"
    manifest.write_text(manifest.read_text().replace("commands:", "commands:\n  branching: {}"), "utf-8")
    return path


@pytest.fixture
def branching(basic_root):
    write_command(basic_root)
    return yaml.safe_load(compile_spec(basic_root).files[".github/workflows/branching.yml"])


# --- parsing ----------------------------------------------------------------


def test_a_membership_condition_names_a_step_and_its_value():
    steps = parse_steps(STEPS, location="t")
    condition = steps[1].condition
    assert condition.is_membership
    assert (condition.value, condition.step_id, condition.output) == ("alpha", "decide", "chosen")


def test_the_flag_form_still_parses():
    steps = parse_steps("1. **X** → script: scripts/fetch-issues.py\n   (if not --skip-it)\n", location="t")
    assert steps[0].condition.flag == "--skip-it"
    assert steps[0].condition.negated
    assert not steps[0].condition.is_membership


# --- emission ---------------------------------------------------------------


def test_the_emitting_step_publishes_a_job_output(branching):
    assert branching["jobs"]["decide"]["outputs"]["chosen"] == "${{ steps.decide.outputs.chosen }}"


def test_a_gated_job_depends_on_the_job_it_reads(branching):
    """Actions exposes the outputs of direct needs only, so a transitive dependency is not enough."""
    assert "decide" in branching["jobs"]["do-the-first-thing"]["needs"]


def test_the_condition_survives_an_empty_output(branching):
    """`fromJSON('')` is an error, not false, and a skipped upstream job publishes exactly that."""
    expression = branching["jobs"]["do-the-first-thing"]["if"]
    assert "needs.decide.outputs.chosen != ''" in expression
    assert "contains(fromJSON(needs.decide.outputs.chosen), 'alpha')" in expression


def test_siblings_gated_on_one_value_run_beside_each_other(branching):
    first = branching["jobs"]["do-the-first-thing"]["needs"]
    second = branching["jobs"]["do-the-second-thing"]["needs"]
    assert second == first
    assert "do-the-first-thing" not in second


def test_whatever_follows_the_branch_waits_for_all_of_it(branching):
    assert branching["jobs"]["finish"]["needs"] == ["do-the-first-thing", "do-the-second-thing"]


# --- what actually runs -----------------------------------------------------


@pytest.mark.parametrize(
    ("chosen", "expected"),
    [
        ('["alpha"]', ["do-the-first-thing"]),
        ('["beta"]', ["do-the-second-thing"]),
        ('["alpha","beta"]', ["do-the-first-thing", "do-the-second-thing"]),
        ("[]", []),
        ("", []),
    ],
)
def test_only_what_was_chosen_runs(branching, chosen, expected):
    outcome = simulate(branching, {}, {"decide": {"chosen": chosen}})
    assert [job for job in outcome.order if job.startswith("do-the-")] == expected


def test_the_step_after_a_branch_runs_even_when_the_branch_did_not(branching):
    """Actions skips a job whose dependency was skipped, which would strand everything downstream."""
    assert "finish" in simulate(branching, {}, {"decide": {"chosen": "[]"}}).order


# --- what it refuses --------------------------------------------------------


def test_reading_a_value_no_step_emits_is_refused(basic_root):
    write_command(basic_root, STEPS.replace("   - emits: chosen\n", ""))
    with pytest.raises(SpecError) as excinfo:
        compile_spec(basic_root)
    assert "does not emit it" in excinfo.value.render()


def test_reading_the_wrong_name_is_refused(basic_root):
    write_command(basic_root, STEPS.replace("{decide.chosen}", "{decide.picked}", 1))
    with pytest.raises(SpecError) as excinfo:
        compile_spec(basic_root)
    assert "emits 'chosen', not 'picked'" in excinfo.value.render()


def test_reading_a_value_a_later_step_emits_is_refused(basic_root):
    """A job cannot read the outputs of one that runs after it."""
    reordered = STEPS.replace("   - emits: chosen\n", "").replace(
        "   - id: finish\n", "   - id: finish\n   - emits: chosen\n"
    )
    write_command(basic_root, reordered.replace("{decide.chosen}", "{finish.chosen}"))
    with pytest.raises(SpecError) as excinfo:
        compile_spec(basic_root)
    assert "does not emit it before this point" in excinfo.value.render()


def test_a_step_whose_value_gates_others_is_never_cached(basic_root):
    """Skipped, it publishes nothing, every dependent reads false, and the pipeline does nothing."""
    write_command(basic_root)
    workflow = yaml.safe_load(compile_spec(basic_root).files[".github/workflows/branching.yml"])
    steps = workflow["jobs"]["decide"]["steps"]
    assert not [step for step in steps if "step-cache" in str(step.get("uses", ""))]


# --- running a step with the compiler ---------------------------------------


COMPILER_STEPS = """
1. **Re-pin** → script: scripts/fetch-issues.py
   - id: repin
   - uses-compiler: true
   - args: --output={output_dir}/moved.json

2. **Run in the executor** → builtin: report
   - id: report
   - args: --run-dir={output_dir}/runs
"""


@pytest.fixture
def compiler_job(basic_root):
    write_command(basic_root, COMPILER_STEPS)
    return yaml.safe_load(compile_spec(basic_root).files[".github/workflows/branching.yml"])


def test_a_compiler_step_runs_outside_the_executor_container(compiler_job):
    """The image deliberately lacks the compiler: a runtime that could recompile could change what runs."""
    assert "container" not in compiler_job["jobs"]["repin"]
    assert "container" in compiler_job["jobs"]["report"]


def test_a_compiler_step_installs_the_pinned_compiler(compiler_job):
    steps = compiler_job["jobs"]["repin"]["steps"]
    runs = [step.get("run", "") for step in steps]
    assert any("uv tool install" in run for run in runs)


def test_the_body_lands_after_the_preamble_not_inside_it(compiler_job):
    """A longer preamble with a fixed insertion index would splice the body into the middle of it."""
    steps = compiler_job["jobs"]["repin"]["steps"]
    names = [step.get("name") or str(step.get("uses", "")) for step in steps]
    assert names.index("Install the pinned compiler") < names.index("Re-pin")


def test_a_compiler_step_never_shares_a_job_with_a_container_step(compiler_job):
    assert "repin" in compiler_job["jobs"] and "report" in compiler_job["jobs"]


def test_no_workflow_carries_the_compilers_scratch_key(basic_root):
    write_command(basic_root, COMPILER_STEPS)
    for text in compile_spec(basic_root).files.values():
        assert "__body_at__" not in text
