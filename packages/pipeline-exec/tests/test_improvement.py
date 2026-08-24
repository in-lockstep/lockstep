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
    assert record["cases"]["traversal"] == {"passed": True, "score": 5}
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
