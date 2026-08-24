"""Grading an agent's answer against a case.

The contract these tests hold is the one that turns "no agent ships without evals" from a check on
the file system into a check on behaviour. Two properties matter most and both are about refusing to
report a comfortable number: a case that asserts nothing is an error rather than a pass, and a case
whose rubric has not been judged is counted as undecided rather than as passing.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner
from pipeline_exec.cli import main
from pipeline_exec.evals import Case, CaseError, grade, parse_rubric, summarize

FINDINGS = {
    "findings": [{"path": "src/files.py", "line": 2, "note": "traversal via name"}],
    "summary": "One finding.",
}


def case(**expect):
    return Case(name="c", input={}, expect=expect)


def checks(result):
    return {(c["check"], c["target"]): c["passed"] for c in result["checks"]}


# --- parsing ----------------------------------------------------------------


def test_a_case_must_say_what_the_agent_was_given():
    with pytest.raises(CaseError, match="no `input`"):
        Case.parse({"expect": {"rubric": "x"}}, name="c")


def test_a_case_that_asserts_nothing_is_refused():
    """It passed before anybody wrote it, which is the whole failure this contract removes."""
    with pytest.raises(CaseError, match="asserts nothing"):
        Case.parse({"input": {}}, name="c")


def test_an_unknown_expectation_is_refused_rather_than_ignored():
    """A typo is not a stricter case; it is one that never runs."""
    with pytest.raises(CaseError, match="notes"):
        Case.parse({"input": {}, "expect": {"notes": "prose"}}, name="c")


def test_a_case_names_itself_or_takes_the_filename(tmp_path):
    path = tmp_path / "path-traversal.json"
    path.write_text(json.dumps({"input": {}, "expect": {"rubric": "x"}}), encoding="utf-8")
    assert Case.load(path).name == "path-traversal"
    path.write_text(json.dumps({"case": "named", "input": {}, "expect": {"rubric": "x"}}), encoding="utf-8")
    assert Case.load(path).name == "named"


def test_invalid_json_names_the_line(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CaseError, match="line 1"):
        Case.load(path)


# --- the deterministic half --------------------------------------------------


def test_schema_checks_a_top_level_field():
    result = grade(case(schema=["findings", "verdict"]), FINDINGS)
    assert checks(result)[("schema", "findings")] is True
    assert checks(result)[("schema", "verdict")] is False


def test_equals_compares_a_field_exactly():
    """The expectation the migration surfaced: two cases were already asserting exact field values."""
    result = grade(case(equals={"summary": "One finding."}), FINDINGS)
    assert result["deterministic_passed"]
    failed = grade(case(equals={"summary": "Nothing."}), FINDINGS)
    assert not failed["deterministic_passed"]
    assert "!=" in failed["checks"][0]["detail"]


def test_equals_is_not_a_substring_match():
    assert not grade(case(equals={"summary": "One"}), FINDINGS)["deterministic_passed"]


def test_contains_searches_the_whole_output_not_one_field():
    """An agent may put the file path in a nested finding; the question is whether it said it."""
    assert grade(case(contains=["src/files.py"]), FINDINGS)["deterministic_passed"]


def test_contains_is_case_insensitive():
    assert grade(case(contains=["SRC/FILES.PY"]), FINDINGS)["deterministic_passed"]


def test_absent_fails_when_the_text_is_there():
    result = grade(case(absent=["traversal"]), FINDINGS)
    assert not result["deterministic_passed"]
    assert "present in the output" in result["checks"][0]["detail"]


def test_absent_passes_when_it_is_not():
    assert grade(case(absent=["password"]), FINDINGS)["deterministic_passed"]


def test_a_bare_count_is_an_exact_count():
    assert grade(case(count={"findings": 1}), FINDINGS)["deterministic_passed"]
    assert not grade(case(count={"findings": 0}), FINDINGS)["deterministic_passed"]


def test_a_count_range_bounds_it_at_either_end():
    assert grade(case(count={"findings": {"min": 1}}), FINDINGS)["deterministic_passed"]
    assert not grade(case(count={"findings": {"min": 2}}), FINDINGS)["deterministic_passed"]
    assert not grade(case(count={"findings": {"max": 0}}), FINDINGS)["deterministic_passed"]


def test_counting_something_with_no_length_fails_rather_than_crashing():
    result = grade(case(count={"missing": 1}), FINDINGS)
    assert not result["deterministic_passed"]
    assert "not something with a length" in result["checks"][0]["detail"]


def test_a_single_expectation_may_be_written_without_a_list():
    assert grade(case(contains="src/files.py"), FINDINGS)["deterministic_passed"]


def test_a_string_output_is_still_searchable():
    assert grade(case(contains=["hello"]), "Hello there")["deterministic_passed"]


# --- what a pass means -------------------------------------------------------


def test_a_case_with_a_rubric_is_never_reported_as_passed_here():
    """This module has no model. Scoring prose without one would invent the number it reports."""
    result = grade(case(contains=["src/files.py"], rubric="Cites the file"), FINDINGS)
    assert result["deterministic_passed"] is True
    assert result["rubric_pending"] is True
    assert result["passed"] is False


def test_a_case_with_no_rubric_passes_outright():
    assert grade(case(contains=["src/files.py"]), FINDINGS)["passed"] is True


# --- the roll-up --------------------------------------------------------------


def test_pending_rubrics_are_not_counted_as_passes():
    """A suite reporting 100% while half of it was never judged is the reassuring lie."""
    results = [
        grade(case(contains=["src/files.py"]), FINDINGS),
        grade(case(contains=["src/files.py"], rubric="judge me"), FINDINGS),
    ]
    summary = summarize(results)
    assert summary["total"] == 2
    assert summary["passed"] == 1
    assert summary["pending_rubric"] == ["c"]
    assert summary["pass_rate"] == 1.0


def test_a_failure_fails_the_suite():
    summary = summarize([grade(case(contains=["nope"]), FINDINGS)])
    assert summary["failed"] == ["c"]
    assert summary["ok"] is False


def test_a_minimum_pass_rate_tolerates_some_failures():
    results = [grade(case(contains=["src/files.py"]), FINDINGS), grade(case(contains=["nope"]), FINDINGS)]
    assert summarize(results, min_pass_rate=0.5)["ok"] is True
    assert summarize(results, min_pass_rate=0.9)["ok"] is False


def test_a_suite_of_only_rubrics_decides_nothing_and_says_so():
    summary = summarize([grade(case(rubric="judge me"), FINDINGS)])
    assert summary["pending_rubric"] == ["c"]
    assert summary["passed"] == 0


# --- the command --------------------------------------------------------------


@pytest.fixture
def suite(tmp_path):
    cases, outputs = tmp_path / "cases", tmp_path / "outputs"
    cases.mkdir()
    outputs.mkdir()
    (cases / "traversal.json").write_text(
        json.dumps({"input": {}, "expect": {"schema": ["findings"], "count": {"findings": {"min": 1}}}}),
        encoding="utf-8",
    )
    (outputs / "traversal.json").write_text(json.dumps(FINDINGS), encoding="utf-8")
    return cases, outputs, tmp_path / "report.json"


def run(cases, outputs, report, *extra):
    return CliRunner().invoke(
        main,
        ["eval-grade", f"--cases={cases}", f"--outputs={outputs}", f"--output={report}", *extra],
    )


def test_the_command_grades_and_writes_a_report(suite):
    cases, outputs, report = suite
    result = run(cases, outputs, report, "--agent=security-reviewer")
    assert result.exit_code == 0, result.output
    written = json.loads(report.read_text())
    assert written["agent"] == "security-reviewer"
    assert written["summary"]["passed"] == 1


def test_an_unanswered_case_fails_rather_than_being_skipped(suite):
    """The agent was asked and did not answer, which is exactly what a suite is for."""
    cases, outputs, report = suite
    (outputs / "traversal.json").unlink()
    result = run(cases, outputs, report)
    assert result.exit_code == 1
    assert "no output" in json.dumps(json.loads(report.read_text()))


def test_a_failing_suite_says_which_check_and_why(suite):
    cases, outputs, report = suite
    (outputs / "traversal.json").write_text(json.dumps({"findings": []}), encoding="utf-8")
    result = run(cases, outputs, report)
    assert result.exit_code == 1
    assert "count" in result.output and "min 1" in result.output


def test_the_verdict_is_published_for_a_later_step(suite):
    cases, outputs, report = suite
    assert "passed=true" in run(cases, outputs, report).output


def test_an_empty_case_directory_is_an_error(tmp_path):
    empty = tmp_path / "cases"
    empty.mkdir()
    result = run(empty, tmp_path, tmp_path / "r.json")
    assert result.exit_code == 1
    assert "no cases" in result.output


def test_a_malformed_case_stops_the_run(suite):
    cases, outputs, report = suite
    (cases / "broken.json").write_text(json.dumps({"expect": {"rubric": "x"}}), encoding="utf-8")
    result = run(cases, outputs, report)
    assert result.exit_code == 1
    assert "no `input`" in result.output


# --- the loop: cases in, answers out, rubrics judged -------------------------


def test_expanding_writes_one_agent_input_per_case(tmp_path):
    """The agent contract is input_path/output_path; a case is just where the input came from."""
    from pipeline_exec.evals import expand

    items = expand([Case("a", {"k": 1}, {"rubric": "x"}), Case("b", {"k": 2}, {"schema": ["s"]})], tmp_path)
    assert json.loads((tmp_path / "a.json").read_text()) == {"k": 1}
    assert [i["case"] for i in items] == ["a", "b"]
    assert [i["rubric"] for i in items] == [True, False]


def test_only_rubric_cases_with_an_answer_reach_the_judge(tmp_path):
    """A case with no answer already failed for that reason; judging it spends a call to repeat it."""
    from pipeline_exec.evals import judge_inputs

    outputs = tmp_path / "out"
    outputs.mkdir()
    (outputs / "answered.json").write_text(json.dumps(FINDINGS), encoding="utf-8")
    cases = [
        Case("answered", {}, {"rubric": "judge me"}),
        Case("unanswered", {}, {"rubric": "judge me"}),
        Case("no-rubric", {}, {"schema": ["findings"]}),
    ]
    pending = judge_inputs(cases, outputs, tmp_path / "judge")
    assert pending == ["answered"]
    paired = json.loads((tmp_path / "judge" / "answered.json").read_text())
    assert paired["rubric"] == "judge me"
    assert paired["output"] == FINDINGS


def test_a_judge_verdict_decides_a_pending_case():
    from pipeline_exec.evals import apply_judgement

    graded = grade(case(contains=["src/files.py"], rubric="Cites the file"), FINDINGS)
    decided = apply_judgement(graded, {"passed": True, "reason": "cites src/files.py:2"})
    assert decided["rubric_pending"] is False
    assert decided["passed"] is True
    assert decided["rubric_verdict"]["reason"] == "cites src/files.py:2"


def test_a_rejected_rubric_fails_even_with_the_checks_green():
    from pipeline_exec.evals import apply_judgement

    graded = grade(case(contains=["src/files.py"], rubric="Says what an attacker does"), FINDINGS)
    decided = apply_judgement(graded, {"passed": False, "reason": "never says what an attacker does"})
    assert decided["deterministic_passed"] is True
    assert decided["passed"] is False
    assert summarize([decided])["failed"] == ["c"]


def test_an_unreadable_verdict_is_not_a_pass():
    """A judge that answered in an unexpected shape has not judged anything."""
    from pipeline_exec.evals import apply_judgement

    graded = grade(case(rubric="x"), FINDINGS)
    for nonsense in (None, {}, {"passed": "yes"}, ["passed"], {"verdict": True}):
        decided = apply_judgement(graded, nonsense)
        assert decided["passed"] is False
        assert decided["rubric_pending"] is False
        assert "did not answer" in decided["rubric_verdict"]["reason"]


# --- the commands -------------------------------------------------------------


def test_the_cases_command_publishes_the_fan_out_list(tmp_path):
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "one.json").write_text(
        json.dumps({"input": {"k": 1}, "expect": {"schema": ["s"]}}), encoding="utf-8"
    )
    result = CliRunner().invoke(
        main, ["eval-cases", f"--cases={cases}", f"--output-dir={tmp_path / 'inputs'}"]
    )
    assert result.exit_code == 0, result.output
    assert 'cases=["one"]' in result.output
    assert json.loads((tmp_path / "inputs" / "one.json").read_text()) == {"k": 1}


def test_grading_folds_in_a_judgement_when_one_is_supplied(suite):
    cases, outputs, report = suite
    (cases / "judged.json").write_text(
        json.dumps({"input": {}, "expect": {"rubric": "Cites the file"}}), encoding="utf-8"
    )
    (outputs / "judged.json").write_text(json.dumps(FINDINGS), encoding="utf-8")
    verdicts = cases.parent / "verdicts"
    verdicts.mkdir()
    (verdicts / "judged.json").write_text(json.dumps({"passed": True, "reason": "ok"}), encoding="utf-8")

    result = run(cases, outputs, report, f"--judgements={verdicts}")
    assert result.exit_code == 0, result.output
    summary = json.loads(report.read_text())["summary"]
    assert summary["pending_rubric"] == []
    assert summary["passed"] == 2


def test_without_judgements_a_rubric_case_stays_undecided(suite):
    cases, outputs, report = suite
    (cases / "judged.json").write_text(
        json.dumps({"input": {}, "expect": {"rubric": "Cites the file"}}), encoding="utf-8"
    )
    (outputs / "judged.json").write_text(json.dumps(FINDINGS), encoding="utf-8")
    result = run(cases, outputs, report)
    assert result.exit_code == 0, result.output
    assert json.loads(report.read_text())["summary"]["pending_rubric"] == ["judged"]


# --- scored rubrics ---------------------------------------------------------
#
# A binary rubric cannot show an agent getting worse while still passing, and prompt work degrades
# in degrees rather than all at once. These hold the scored form to the same standard as the rest of
# the contract: a number nobody can compare across runs is not better than a boolean.


SCALE = {
    "criteria": "Says what an attacker does with it.",
    "levels": {"5": "Names the exploit and the line", "3": "Notices the input", "1": "Misses it"},
    "min": 4,
}


def scored(**overrides):
    return Case(name="c", input={}, expect={"rubric": {**SCALE, **overrides}})


def test_a_rubric_may_be_prose_or_a_scale():
    assert case(rubric="prose").rubric.scored is False
    assert scored().rubric.scored is True
    assert scored().rubric.scale == (1, 5)
    assert scored().rubric.threshold == 4


def test_one_level_is_not_a_scale():
    with pytest.raises(CaseError, match="at least two scores"):
        parse_rubric({**SCALE, "levels": {"5": "good"}}, name="c")


def test_a_scored_rubric_without_a_threshold_is_refused():
    with pytest.raises(CaseError, match="needs `min`"):
        parse_rubric({k: v for k, v in SCALE.items() if k != "min"}, name="c")


def test_a_threshold_outside_the_scale_is_refused():
    with pytest.raises(CaseError, match="outside the scale"):
        parse_rubric({**SCALE, "min": 9}, name="c")


def test_a_level_that_is_not_a_score_is_refused():
    with pytest.raises(CaseError, match="not a score"):
        parse_rubric({**SCALE, "levels": {"good": "…", "bad": "…"}}, name="c")


def test_a_level_that_says_nothing_about_what_earns_it_is_refused():
    with pytest.raises(CaseError, match="says nothing"):
        parse_rubric({**SCALE, "levels": {"5": "Names the exploit", "1": "  "}}, name="c")


def test_a_rubric_needs_criteria():
    with pytest.raises(CaseError, match="needs `criteria`"):
        parse_rubric({**SCALE, "criteria": ""}, name="c")


def test_an_unknown_rubric_key_is_refused_rather_than_ignored():
    with pytest.raises(CaseError, match="unknown rubric key"):
        parse_rubric({**SCALE, "treshold": 4}, name="c")


def test_an_empty_rubric_asks_the_judge_nothing():
    with pytest.raises(CaseError, match="empty"):
        parse_rubric("   ", name="c")


def test_a_malformed_rubric_is_caught_when_the_case_is_read_not_after_the_agent_ran():
    """A model call to be told the case is broken is a model call wasted."""
    with pytest.raises(CaseError, match="at least two scores"):
        Case.parse(
            {"input": {}, "expect": {"rubric": {"criteria": "x", "levels": {"5": "y"}, "min": 5}}}, name="c"
        )


def test_the_scale_travels_with_the_graded_case():
    result = grade(scored(), FINDINGS)
    assert result["rubric_scored"] is True
    assert result["rubric_scale"] == {"min": 1, "max": 5}
    assert result["rubric_min_score"] == 4


def test_the_judge_is_given_the_levels_not_just_the_scale(tmp_path):
    """A judge told only 'score this out of 5' invents the scale on every call."""
    from pipeline_exec.evals import judge_inputs

    (tmp_path / "answers").mkdir()
    (tmp_path / "answers" / "c.json").write_text(json.dumps(FINDINGS))
    judge_inputs([scored()], tmp_path / "answers", tmp_path / "judge")

    payload = json.loads((tmp_path / "judge" / "c.json").read_text())
    assert payload["scored"] is True
    assert payload["levels"]["3"] == "Notices the input"
    assert payload["scale"] == {"min": 1, "max": 5}
    assert payload["min_score"] == 4


def test_a_score_at_the_threshold_passes():
    from pipeline_exec.evals import apply_judgement

    decided = apply_judgement(grade(scored(), FINDINGS), {"score": 4, "reason": "close enough"})
    assert decided["passed"] is True
    assert decided["score"] == 4


def test_a_score_below_the_threshold_fails_a_case_whose_checks_all_passed():
    from pipeline_exec.evals import apply_judgement

    result = grade(scored(), FINDINGS)
    decided = apply_judgement(result, {"score": 3, "reason": "did not say what an attacker does"})
    assert decided["deterministic_passed"] is True
    assert decided["passed"] is False
    assert decided["rubric_verdict"]["score"] == 3


def test_a_boolean_answer_to_a_scored_rubric_is_not_a_pass():
    """`True` is an `int` in Python; taken as a score it would silently become a 1."""
    from pipeline_exec.evals import apply_judgement

    decided = apply_judgement(grade(scored(), FINDINGS), {"passed": True, "reason": "looks good"})
    assert decided["passed"] is False
    assert "whole number" in decided["rubric_verdict"]["reason"]


def test_a_score_outside_the_scale_is_not_a_pass():
    from pipeline_exec.evals import apply_judgement

    decided = apply_judgement(grade(scored(), FINDINGS), {"score": 9})
    assert decided["passed"] is False
    assert "outside the scale" in decided["rubric_verdict"]["reason"]


def test_a_perfect_score_does_not_rescue_a_failed_check():
    from pipeline_exec.evals import apply_judgement

    result = grade(Case(name="c", input={}, expect={"schema": ["missing"], "rubric": SCALE}), FINDINGS)
    decided = apply_judgement(result, {"score": 5})
    assert decided["passed"] is False


def test_the_summary_reports_the_mean_and_the_distribution():
    results = [
        {"case": "a", "passed": True, "score": 5},
        {"case": "b", "passed": True, "score": 5},
        {"case": "c", "passed": True, "score": 2},
    ]
    summary = summarize(results)
    assert summary["mean_score"] == 4.0
    assert summary["score_counts"] == {"2": 1, "5": 2}
    assert summary["scores"] == {"a": 5, "b": 5, "c": 2}


def test_a_slide_in_the_mean_fails_a_suite_in_which_every_case_passed():
    """The regression a pass rate cannot see, which is the reason scores exist."""
    results = [{"case": "a", "passed": True, "score": 4}, {"case": "b", "passed": True, "score": 4}]
    assert summarize(results, min_score=4)["ok"] is True
    assert summarize(results, min_score=4.5)["ok"] is False
    assert summarize(results, min_score=4.5)["pass_rate"] == 1.0


def test_an_undecided_score_is_left_out_of_the_mean():
    results = [
        {"case": "a", "passed": True, "score": 5},
        {"case": "b", "passed": False, "rubric_pending": True, "score": 1},
    ]
    summary = summarize(results)
    assert summary["mean_score"] == 5.0
    assert summary["pending_rubric"] == ["b"]


def test_an_unanswered_scored_case_reports_like_every_other_case(tmp_path):
    """One shape for a report, built in one place.

    The unanswered branch used to assemble the result itself, which is how it came to be writing a
    rubric object where every other case writes prose — a crash in the grader, at the end of a run
    that had already spent the credits.
    """
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "one.json").write_text(json.dumps({"input": {}, "expect": {"rubric": SCALE}}))

    result = CliRunner().invoke(
        main,
        [
            "eval-grade",
            f"--cases={cases}",
            f"--outputs={tmp_path / 'answers'}",
            f"--output={tmp_path / 'report.json'}",
        ],
    )
    assert result.exit_code == 1, result.output
    entry = json.loads((tmp_path / "report.json").read_text())["cases"][0]
    assert entry["rubric"] == SCALE["criteria"]
    assert entry["rubric_scored"] is True
    assert entry["passed"] is False
    assert entry["rubric_pending"] is False


def test_a_floor_with_nothing_scored_behind_it_does_not_invent_a_verdict():
    results = [{"case": "a", "passed": True}]
    summary = summarize(results, min_score=5)
    assert summary["mean_score"] is None
    assert summary["ok"] is True


# --- fixture repositories ---------------------------------------------------
#
# A case carries `input`, and an agent asked to review code was being handed a JSON object and asked
# to reason about a patch fragment. These cover the tree: where it comes from, where it lands, and
# what happens when a case says it has one and does not.


def suite_with_fixture(tmp_path, *, files=None, case_extra=None):
    cases = tmp_path / "evals" / "reviewer" / "cases"
    cases.mkdir(parents=True)
    fixture = tmp_path / "evals" / "reviewer" / "fixtures" / "traversal"
    (fixture / "src").mkdir(parents=True)
    for name, body in (files or {"src/files.py": "def serve(name):\n    return open(name).read()\n"}).items():
        target = fixture / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    payload = {
        "input": {"pull_request": {"title": "t"}},
        "fixture": "traversal",
        "expect": {"contains": ["src/files.py"]},
        **(case_extra or {}),
    }
    (cases / "one.json").write_text(json.dumps(payload))
    return cases


def test_a_fixture_is_laid_down_per_case_and_the_input_says_where(tmp_path):
    from pipeline_exec.evals import expand

    cases = suite_with_fixture(tmp_path)
    expand([Case.load(cases / "one.json")], tmp_path / "inputs", repos=tmp_path / "repos")

    written = json.loads((tmp_path / "inputs" / "one.json").read_text())
    assert written["repo"] == str(tmp_path / "repos" / "one")
    assert (tmp_path / "repos" / "one" / "src" / "files.py").is_file()
    # The case file itself is untouched by the path that only exists at run time.
    assert "repo" not in json.loads((cases / "one.json").read_text())["input"]


def test_a_stale_file_from_an_earlier_run_does_not_survive(tmp_path):
    """An agent answering because of a file nobody wrote is reporting on the wrong repository."""
    from pipeline_exec.evals import expand

    cases = suite_with_fixture(tmp_path)
    stale = tmp_path / "repos" / "one" / "leftover.py"
    stale.parent.mkdir(parents=True)
    stale.write_text("# from the last run\n")

    expand([Case.load(cases / "one.json")], tmp_path / "inputs", repos=tmp_path / "repos")
    assert not stale.exists()
    assert (tmp_path / "repos" / "one" / "src" / "files.py").is_file()


def test_a_case_cannot_set_the_key_the_fixture_path_goes_in(tmp_path):
    cases = suite_with_fixture(tmp_path)
    payload = json.loads((cases / "one.json").read_text())
    payload["input"]["repo"] = "/somewhere/else"
    (cases / "one.json").write_text(json.dumps(payload))

    with pytest.raises(CaseError, match="input.repo"):
        Case.load(cases / "one.json")


def test_a_fixture_name_cannot_be_a_path(tmp_path):
    """A case that could write `../../..` would hand the agent the repository running the eval."""
    cases = suite_with_fixture(tmp_path)
    payload = json.loads((cases / "one.json").read_text())
    payload["fixture"] = "../../../.."
    (cases / "one.json").write_text(json.dumps(payload))

    with pytest.raises(CaseError, match="directory name"):
        Case.load(cases / "one.json")


def test_a_fixture_that_is_not_there_is_an_error_not_an_empty_checkout(tmp_path):
    cases = suite_with_fixture(tmp_path)
    payload = json.loads((cases / "one.json").read_text())
    payload["fixture"] = "no-such-tree"
    (cases / "one.json").write_text(json.dumps(payload))

    with pytest.raises(CaseError, match="no fixture at"):
        Case.load(cases / "one.json")


def test_an_empty_fixture_directory_is_refused(tmp_path):
    cases = suite_with_fixture(tmp_path)
    for path in sorted((tmp_path / "evals" / "reviewer" / "fixtures" / "traversal").rglob("*")):
        if path.is_file():
            path.unlink()

    with pytest.raises(CaseError, match="no files"):
        Case.load(cases / "one.json")


def test_a_fixture_needs_somewhere_to_record_the_path(tmp_path):
    cases = suite_with_fixture(tmp_path)
    payload = json.loads((cases / "one.json").read_text())
    payload["input"] = "a string"
    (cases / "one.json").write_text(json.dumps(payload))

    with pytest.raises(CaseError, match="object for `input`"):
        Case.load(cases / "one.json")


def test_expanding_a_fixture_case_with_nowhere_to_put_it_says_so(tmp_path):
    from pipeline_exec.evals import expand

    cases = suite_with_fixture(tmp_path)
    with pytest.raises(CaseError, match="no directory was given"):
        expand([Case.load(cases / "one.json")], tmp_path / "inputs")


def test_the_cases_command_materializes_the_tree(tmp_path):
    cases = suite_with_fixture(tmp_path)
    result = CliRunner().invoke(
        main,
        [
            "eval-cases",
            f"--cases={cases}",
            f"--output-dir={tmp_path / 'inputs'}",
            f"--repo-dir={tmp_path / 'repos'}",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "1 with a fixture" in result.output
    assert (tmp_path / "repos" / "one" / "src" / "files.py").is_file()


def test_the_cases_command_refuses_a_broken_fixture_rather_than_running_the_agent(tmp_path):
    cases = suite_with_fixture(tmp_path)
    payload = json.loads((cases / "one.json").read_text())
    payload["fixture"] = "no-such-tree"
    (cases / "one.json").write_text(json.dumps(payload))

    result = CliRunner().invoke(
        main, ["eval-cases", f"--cases={cases}", f"--output-dir={tmp_path / 'inputs'}"]
    )
    assert result.exit_code != 0
    assert "no fixture at" in result.output
