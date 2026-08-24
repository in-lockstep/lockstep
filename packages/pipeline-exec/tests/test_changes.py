"""Turning an agent's proposal into a change, and finding out whether it holds up.

Promoted out of `examples/implement-issue` and `examples/bug-fix`. Two properties carry most of the
weight here, and both are about the boundary between what an agent proposes and what actually
happens to a repository.

`apply-patch` is the only thing in a pipeline that writes, so the rules about where a change may
reach live in it as code rather than in a prompt as a request.

And a reproducer that does not fail proves nothing — so a suite run says what it *expected*, and a
test that passes both before and after a fix is reported as not having done its job.
"""

from __future__ import annotations

import json
import subprocess

from click.testing import CliRunner
from pipeline_exec.changes import check_verdict, protected_paths, render_plan, suite_verdict
from pipeline_exec.cli import main


def patch_for(path: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new\n"


# --- what a patch may reach -------------------------------------------------


def test_a_patch_that_edits_the_workflows_is_refused(tmp_path):
    """The prompt asks it not to. This is the thing that holds when the prompt does not."""
    assert protected_paths(patch_for(".github/workflows/review.yml")) == [".github/workflows/review.yml"]


def test_every_protected_root_is_covered():
    for path in (
        ".pipeline/pins.lock",
        "profiles/repo.md",
        "guardrails/common.md",
        "agents/writer.md",
        "commands/fix.md",
    ):
        assert protected_paths(patch_for(path)) == [path], path


def test_ordinary_source_is_not_protected():
    assert protected_paths(patch_for("src/app.py")) == []


def test_a_patch_reaching_a_protected_path_never_runs_git(tmp_path):
    bad = tmp_path / "p.diff"
    bad.write_text(patch_for(".github/workflows/ci.yml"))
    result = CliRunner().invoke(main, ["apply-patch", f"--patch={bad}", f"--repo={tmp_path}"])
    assert result.exit_code != 0
    assert "protected paths" in result.output
    assert "applied=false" in result.output


def test_an_empty_patch_is_not_a_failure(tmp_path):
    empty = tmp_path / "p.diff"
    empty.write_text("\n")
    result = CliRunner().invoke(main, ["apply-patch", f"--patch={empty}", f"--repo={tmp_path}"])
    assert result.exit_code == 0
    assert "nothing to apply" in result.output


def test_a_patch_that_is_not_there_is_an_error(tmp_path):
    result = CliRunner().invoke(main, ["apply-patch", f"--patch={tmp_path / 'nope.diff'}"])
    assert result.exit_code != 0


def test_a_real_patch_applies(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("old\n")
    # `--3way` reads the index to fall back on a merge, so the file has to be tracked. In a real
    # run it is: the patch is applied to a checkout.
    subprocess.run(["git", "add", "src/app.py"], cwd=tmp_path, check=True)
    patch = tmp_path / "p.diff"
    patch.write_text(patch_for("src/app.py"))
    result = CliRunner().invoke(main, ["apply-patch", f"--patch={patch}", f"--repo={tmp_path}"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "src" / "app.py").read_text() == "new\n"


# --- what a suite run proves ------------------------------------------------


def completed(code: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout="out", stderr="")


def test_a_reproducer_that_fails_is_satisfied():
    """Before the fix, failing is the point."""
    verdict = suite_verdict(completed(1), suite="pytest", select="test_bug", expect="fail")
    assert verdict["passed"] is False
    assert verdict["satisfied"] is True


def test_a_reproducer_that_passes_has_proven_nothing():
    verdict = suite_verdict(completed(0), suite="pytest", select="test_bug", expect="fail")
    assert verdict["satisfied"] is False


def test_after_the_fix_passing_is_what_is_wanted():
    assert suite_verdict(completed(0), suite="pytest", select="", expect="pass")["satisfied"] is True


def test_the_tail_of_the_output_is_kept_not_the_head():
    """A failure's reason is at the end of a test run, not the beginning."""
    long = subprocess.CompletedProcess(args=[], returncode=1, stdout="x" * 9000, stderr="THE REASON")
    assert suite_verdict(long, suite="pytest", select="", expect="pass")["output"].endswith("THE REASON")


def test_an_unknown_suite_is_refused_rather_than_guessed(tmp_path):
    result = CliRunner().invoke(main, ["run-suite", f"--repo={tmp_path}", "--suite=nosuchthing"])
    assert result.exit_code != 0
    assert "known:" in result.output


# --- what the project's own CI concluded -------------------------------------


def test_a_check_that_had_nothing_to_do_is_not_a_rejection():
    """`neutral` and `skipped` would otherwise block every change that did not touch what they watch."""
    runs = [
        {"name": "build", "conclusion": "success"},
        {"name": "lint", "conclusion": "neutral"},
        {"name": "e2e", "conclusion": "skipped"},
    ]
    assert check_verdict(runs, ref="abc")["passed"] is True


def test_a_failing_check_is_named():
    runs = [{"name": "build", "conclusion": "success"}, {"name": "tests", "conclusion": "failure"}]
    verdict = check_verdict(runs, ref="abc")
    assert verdict["passed"] is False
    assert verdict["failed"] == ["tests"]


def test_a_check_still_running_is_not_a_pass():
    assert check_verdict([{"name": "build", "conclusion": None}], ref="abc")["passed"] is False


# --- the plan a human reads --------------------------------------------------


PLAN = {
    "summary": "Validate the export format against the shipped templates.",
    "approach": "Reject anything not in the template directory.",
    "rejected": [{"option": "Escape the value", "reason": "still allows any readable file"}],
    "changes": [{"path": "src/reports/export.py", "reason": "the join happens here"}],
    "verification": "A test asserting an unknown format returns 400.",
    "risks": ["A caller relying on an undocumented format breaks."],
    "open_questions": ["Should an unknown format 400 or fall back to pdf?"],
}


def test_every_section_the_plan_filled_in_is_rendered():
    text = render_plan(PLAN)
    for expected in (
        "### Approach",
        "### Considered and rejected",
        "### Files this changes",
        "### How this is proven",
        "### What this could break",
        "### Open questions",
    ):
        assert expected in text


def test_a_section_the_plan_left_empty_is_left_out():
    """The comment is updated in place across iterations; empty headings would be churn."""
    text = render_plan({"summary": "Just this."})
    assert "###" not in text
    assert text.strip() == "Just this."


def test_rendering_is_stable_across_runs():
    assert render_plan(PLAN) == render_plan(json.loads(json.dumps(PLAN)))


def test_no_plan_to_render_is_not_a_failure(tmp_path):
    result = CliRunner().invoke(
        main, ["render-plan", f"--plan={tmp_path / 'nope.json'}", f"--output={tmp_path / 'o.md'}"]
    )
    assert result.exit_code == 0
    assert "no plan to render" in result.output


def test_the_rendered_plan_lands_where_it_was_asked_to(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(PLAN))
    out = tmp_path / "nested" / "plan.md"
    result = CliRunner().invoke(main, ["render-plan", f"--plan={plan}", f"--output={out}"])
    assert result.exit_code == 0, result.output
    assert "Validate the export format" in out.read_text()
