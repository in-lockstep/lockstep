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
from pipeline_exec.evals import Case, CaseError, grade, summarize

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

