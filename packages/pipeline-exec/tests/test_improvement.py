"""Did a change to a prompt actually make the agent better?

A retro agent can say what to try. It cannot say whether the attempt worked — that is the same kind
of thing judging its own kind. The eval suite can, but only if the comparison knows what a
meaningless difference looks like: agents are non-deterministic, so a naive before-and-after reports
improvements and regressions that are nothing but sampling.

The property every test here defends is that **a comparison which has not measured its own noise
floor does not report a direction.** The rest follows from it.
"""

from __future__ import annotations

import json

from pipeline_exec.improvement import (
    MIN_BASELINE_RUNS,
    Comparison,
    baseline_runs,
    case_verdicts,
    eval_record,
    fingerprint,
    render,
)


def run(prompt, score, cases, *, day=10, agent="reviewer"):
    return {
        "kind": "eval",
        "agent": agent,
        "prompt": prompt,
        "finished": f"2026-08-{day:02d}T10:00:00+00:00",
        "pass_rate": round(sum(cases.values()) / len(cases), 4),
        "mean_score": score,
        "cases": {name: {"passed": passed, "score": None} for name, passed in cases.items()},
    }


SOLID = {"traversal": True, "nothing-to-find": True}


def baseline(prompt="old", scores=(4.2, 4.6, 4.4), cases=None):
    return [run(prompt, score, cases or SOLID, day=10 + i) for i, score in enumerate(scores)]


def compare(candidate, runs):
    return Comparison(agent="reviewer", candidate=candidate, runs=runs).build()


# --- the noise floor --------------------------------------------------------


def test_a_change_smaller_than_the_wobble_is_not_an_improvement():
    """Three baseline runs of one prompt spanning 0.4 make a 0.2 delta meaningless."""
    result = compare(run("new", 4.6, SOLID), baseline())
    assert result["verdict"] == "within noise"
    score = next(m for m in result["metrics"] if m["metric"] == "mean_score")
    assert score["noise"] == 0.4 and score["verdict"] == "within noise"


def test_a_change_larger_than_the_wobble_is_an_improvement():
    result = compare(run("new", 5.0, SOLID), baseline())
    assert result["verdict"] == "improved"


def test_landing_exactly_on_the_noise_floor_does_not_clear_it():
    """The floor is the largest difference seen between runs that differed by nothing."""
    result = compare(run("new", 4.8, SOLID), baseline())
    assert result["verdict"] == "within noise"


def test_one_baseline_run_measures_no_noise_and_says_so():
    """A sample of one has no spread. Reporting a direction from it would be inventing certainty."""
    result = compare(run("new", 9.0, SOLID), baseline(scores=(4.2,)))
    assert result["verdict"] == "no noise floor"
    assert result["baseline"]["noise_measured"] is False
    # The number is still reported; it is the *direction* that is not established.
    assert next(m for m in result["metrics"] if m["metric"] == "mean_score")["delta"] == 4.8


def test_two_baseline_runs_are_still_a_sample_size_of_one():
    assert MIN_BASELINE_RUNS == 3
    assert compare(run("new", 9.0, SOLID), baseline(scores=(4.2, 4.6)))["verdict"] == "no noise floor"


def test_no_baseline_at_all_says_so():
    result = compare(run("new", 4.6, SOLID), [])
    assert result["verdict"] == "no baseline"
    assert "becomes the baseline for the next change" in render(result)


# --- the per-case half ------------------------------------------------------


def test_a_case_that_was_solid_and_broke_outranks_the_aggregate():
    """An average absorbs one case failing and can still tick upward.

    The case is the thing somebody has to go and fix, so it decides the verdict.
    """
    result = compare(run("new", 4.9, {"traversal": False, "nothing-to-find": True}), baseline())
    assert result["verdict"] == "regressed"
    assert result["regressed"] == ["traversal"]
    # The aggregate, left to itself, saw an improvement.
    assert next(m for m in result["metrics"] if m["metric"] == "mean_score")["delta"] > 0


def test_a_flaky_case_flipping_decides_nothing():
    """It passed some baseline runs and not others with the prompt unchanged.

    Its flip today is the case being unreliable, which is a defect in the case rather than a
    finding about the agent.
    """
    runs = [
        run("old", 4.4, {"traversal": True, "wobbly": True}, day=10),
        run("old", 4.4, {"traversal": True, "wobbly": False}, day=11),
        run("old", 4.4, {"traversal": True, "wobbly": True}, day=12),
    ]
    result = compare(run("new", 4.4, {"traversal": True, "wobbly": False}), runs)
    assert result["flaky"] == ["wobbly"]
    assert result["regressed"] == []
    assert "decide nothing" in render(result)


def test_a_case_that_always_failed_and_now_passes_is_fixed():
    runs = [run("old", 3.0, {"traversal": False}, day=10 + i) for i in range(3)]
    result = compare(run("new", 3.0, {"traversal": True}), runs)
    assert result["fixed"] == ["traversal"]
    assert result["verdict"] == "improved"


def test_a_case_with_too_few_baseline_runs_cannot_regress():
    """So the gate cannot fire on a case nobody has established a baseline for."""
    runs = [run("old", 4.4, {"traversal": True}, day=10 + i) for i in range(2)]
    verdicts = {v.case: v.verdict for v in case_verdicts(runs, run("new", 4.4, {"traversal": False}))}
    assert verdicts["traversal"] == "too few baseline runs"


def test_a_case_added_by_this_change_is_new_not_a_regression():
    result = compare(run("new", 4.4, {**SOLID, "brand-new": False}), baseline())
    assert "brand-new" not in result["regressed"]


# --- which runs are the baseline --------------------------------------------


def test_the_baseline_is_the_prompt_this_one_replaces():
    """Not "everything before now", which would mix prompts and call their differences noise."""
    records = [
        run("ancient", 3.0, SOLID, day=1),
        run("previous", 4.4, SOLID, day=10),
        run("previous", 4.6, SOLID, day=11),
    ]
    chosen = baseline_runs(records, candidate_prompt="new")
    assert {r["prompt"] for r in chosen} == {"previous"}


def test_a_rerun_of_the_same_prompt_has_no_baseline_of_its_own():
    """A scheduled re-run measures the noise; it is not a candidate to be judged."""
    records = [run("current", 4.4, SOLID, day=10)]
    assert baseline_runs(records, candidate_prompt="current") == []


# --- the fingerprint --------------------------------------------------------


def test_the_fingerprint_changes_when_anything_in_the_prompt_does(tmp_path):
    agent = tmp_path / "aw-reviewer.md"
    agent.write_text("---\nmodel: claude-sonnet-4-6\n---\nReview it.")
    before = fingerprint(agent)
    agent.write_text("---\nmodel: claude-opus-5\n---\nReview it.")
    assert fingerprint(agent) != before


def test_a_missing_agent_has_no_fingerprint(tmp_path):
    assert fingerprint(tmp_path / "nope.md") == ""


def test_a_record_carries_the_prompt_it_scored():
    report = {
        "agent": "reviewer",
        "summary": {"total": 2, "pending_rubric": [], "pass_rate": 1.0, "mean_score": 4.5},
        "cases": [{"case": "traversal", "passed": True, "score": 5}],
    }
    record = eval_record(report, prompt="abc123", identity={"run_id": "9"})
    assert record["kind"] == "eval" and record["prompt"] == "abc123"
    assert record["cases"]["traversal"] == {"passed": True, "score": 5, "answered": True, "judged": True}
    assert record["decided"] == 2


def test_a_pending_rubric_is_not_counted_as_decided():
    report = {
        "agent": "reviewer",
        "summary": {"total": 3, "pending_rubric": ["a"], "pass_rate": 1.0, "mean_score": None},
        "cases": [],
    }
    assert eval_record(report, prompt="p", identity={})["decided"] == 2


# --- the report somebody reads ----------------------------------------------


def test_an_unmeasured_noise_floor_is_stated_before_the_numbers():
    text = render(compare(run("new", 9.0, SOLID), baseline(scores=(4.2,))))
    assert "noise floor was not measured" in text
    assert "the direction is not established" in text


def test_the_comparison_is_json_a_later_step_can_gate_on():
    result = compare(run("new", 4.9, {"traversal": False, "nothing-to-find": True}), baseline())
    assert json.loads(json.dumps(result))["regressed"] == ["traversal"]


# --- day one ----------------------------------------------------------------


def test_a_ledger_branch_that_does_not_exist_yet_is_not_a_failure(tmp_path):
    """The first eval run on every repository that turns the loop on takes this path.

    Treating a missing branch as a usage error would fail that run before there was anything it
    could have compared against.
    """
    import subprocess

    from click.testing import CliRunner
    from pipeline_exec.cli import main

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "agent": "reviewer",
                "summary": {"total": 1, "pending_rubric": [], "pass_rate": 1.0, "mean_score": 5},
                "cases": [{"case": "traversal", "passed": True}],
            }
        )
    )
    (tmp_path / "agent.md").write_text("prompt")

    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = CliRunner().invoke(
            main,
            [
                "eval-compare",
                "--agent=reviewer",
                f"--report={report}",
                "--prompt-file=agent.md",
                "--branch=pipeline-history",
                f"--output={tmp_path / 'out.json'}",
            ],
        )
    finally:
        os.chdir(cwd)

    assert result.exit_code == 0, result.output
    assert json.loads((tmp_path / "out.json").read_text())["verdict"] == "no baseline"


def test_neither_a_ledger_nor_a_branch_is_still_a_usage_error(tmp_path):
    from click.testing import CliRunner
    from pipeline_exec.cli import main

    report = tmp_path / "report.json"
    report.write_text(json.dumps({"agent": "a", "summary": {}, "cases": []}))
    (tmp_path / "agent.md").write_text("prompt")
    result = CliRunner().invoke(
        main,
        [
            "eval-compare",
            "--agent=a",
            f"--report={report}",
            f"--prompt-file={tmp_path / 'agent.md'}",
            f"--output={tmp_path / 'out.json'}",
        ],
    )
    assert result.exit_code != 0
    assert "--ledger or --branch" in result.output


# --- an outage is not a regression ------------------------------------------


def test_a_case_the_agent_never_answered_does_not_block_a_merge():
    """A provider outage, a rate limit or a cancelled leg is a failure of the suite, not evidence.

    Without this the chain is airtight and wrong: a missing answer file becomes `passed: False`,
    counts as decided, reads as a case that always passed and now fails, and exits 1 on the pull
    request — blocking a merge for a reason nobody can act on.
    """
    candidate = run("new", 4.4, SOLID)
    candidate["cases"]["traversal"]["answered"] = False
    candidate["cases"]["traversal"]["passed"] = False
    result = compare(candidate, baseline())
    assert result["regressed"] == []
    assert result["not_run"] == ["traversal"]
    assert result["verdict"] != "regressed"
    assert "Never ran, so they say nothing either way" in render(result)


def test_a_baseline_run_that_never_answered_is_not_a_baseline_failure():
    """Counting it would make the next change look like a fix."""
    runs = baseline()
    for record in runs:
        record["cases"]["traversal"] = {"passed": False, "score": None, "answered": False}
    verdicts = {v.case: v.verdict for v in case_verdicts(runs, run("new", 4.4, SOLID))}
    # Every baseline observation was discarded, so there is nothing to compare against.
    assert verdicts["traversal"] == "new case"


def test_an_answered_failure_is_still_a_regression():
    """The distinction must not become a way for real regressions to escape."""
    candidate = run("new", 4.4, {"traversal": False, "nothing-to-find": True})
    assert compare(candidate, baseline())["regressed"] == ["traversal"]


def test_records_from_before_this_distinction_existed_are_read_as_answered():
    """`answered` defaults to true, so a ledger written by an older executor still compares."""
    runs = baseline()
    for record in runs:
        for entry in record["cases"].values():
            entry.pop("answered", None)
    assert compare(run("new", 4.4, {"traversal": False, "nothing-to-find": True}), runs)["regressed"] == [
        "traversal"
    ]


# --- a suite with no judge decides nothing -----------------------------------
#
# Every case worth writing carries a rubric — "says what an attacker does with it" is not a
# substring match — and a rubric nobody judges is reported as undecided. Before this, that produced
# a pass_rate of 1.0 in the ledger, every case reading as "unchanged" forever, and a merge gate that
# could never fire. A comfortable number from no evidence is the one thing this module exists to
# refuse, so it has to refuse it here too.


def pending(prompt, day=10):
    report = {
        "agent": "reviewer",
        "summary": {"total": 2, "pending_rubric": ["a", "b"], "pass_rate": None, "mean_score": None},
        "cases": [
            {"case": "a", "passed": False, "rubric_pending": True},
            {"case": "b", "passed": False, "rubric_pending": True},
        ],
    }
    return eval_record(report, prompt=prompt, identity={"finished": f"2026-08-{day:02d}"})


def test_an_unjudged_case_is_recorded_as_undecided():
    assert pending("old")["cases"]["a"] == {"passed": False, "score": None, "answered": True, "judged": False}


def test_a_suite_that_decided_nothing_says_so_instead_of_reporting_a_direction():
    runs = [pending("old", day=10 + i) for i in range(3)]
    result = compare(pending("new"), runs)
    assert result["verdict"] == "nothing decided"
    assert result["unjudged"] == ["a", "b"]
    assert result["regressed"] == []
    text = render(result)
    assert "Not one case in this suite decided anything" in text
    assert "evals.judge" in text


def test_an_unjudged_case_cannot_be_mistaken_for_a_baseline_failure():
    """`passes == 0` on every baseline run made every later change look like it fixed nothing."""
    runs = [pending("old", day=10 + i) for i in range(3)]
    verdicts = {v.case: v.verdict for v in case_verdicts(runs, pending("new"))}
    assert set(verdicts.values()) == {"unjudged"}


def test_a_judged_case_alongside_unjudged_ones_still_decides():
    """The suite is not written off because part of it awaits judgement."""
    runs = []
    for i in range(3):
        record = pending("old", day=10 + i)
        record["cases"]["c"] = {"passed": True, "score": 5, "answered": True, "judged": True}
        runs.append(record)
    candidate = pending("new")
    candidate["cases"]["c"] = {"passed": False, "score": 1, "answered": True, "judged": True}
    result = compare(candidate, runs)
    assert result["regressed"] == ["c"]
    assert result["verdict"] == "regressed"
