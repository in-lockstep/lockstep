"""Report writing, secret redaction, and dashboard rendering."""

from __future__ import annotations

import json

from click.testing import CliRunner
from pipeline_exec.builtins.test_runner import _write_error_reports, _write_execution_report
from pipeline_exec.cli import main
from pipeline_exec.config import ExecConfig
from pipeline_exec.executors.types import ExecutedStep, TestResult
from pipeline_exec.reports.collect import build_dashboard_data
from pipeline_exec.reports.dashboard import generate_dashboard, generate_index_page
from pipeline_exec.sanitize import init_sanitizer, sanitize


def result_for(passed: bool) -> TestResult:
    return TestResult(
        story_id="LOGIN-1",
        passed=passed,
        summary="User can log in",
        executed_steps=[
            ExecutedStep(phase="test", step_number=1, tool="browser", action="navigate", status="passed"),
            ExecutedStep(
                phase="test",
                step_number=2,
                tool="browser",
                action="click",
                status="failed" if not passed else "passed",
                result="selector not found" if not passed else "ok",
            ),
        ],
        errors=[] if passed else [{"step": "2", "message": "selector not found"}],
    )


# --- secret redaction ------------------------------------------------------


def test_sensitive_environment_values_are_redacted(monkeypatch):
    """Reports are published as artifacts and rendered into dashboards; secrets must not ride along."""
    monkeypatch.setenv("APP_PASSWORD", "hunter2-very-secret")
    init_sanitizer()
    assert "hunter2-very-secret" not in sanitize("login failed with hunter2-very-secret")


def test_ordinary_text_survives_redaction(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "hunter2-very-secret")
    init_sanitizer()
    assert sanitize("selector not found") == "selector not found"


def test_short_values_are_not_redacted(monkeypatch):
    """Redacting a two-character secret would blank out unrelated text."""
    monkeypatch.setenv("TOKEN", "ab")
    init_sanitizer()
    assert "grab" in sanitize("grab the thing")


# --- execution reports -----------------------------------------------------


def test_a_passing_run_writes_a_passing_report(tmp_path):
    _write_execution_report(result_for(True), str(tmp_path))
    report = (tmp_path / "executions" / "LOGIN-1.md").read_text()
    assert "**PASSED**" in report
    assert "LOGIN-1" in report


def test_a_failing_run_writes_a_failing_report(tmp_path):
    _write_execution_report(result_for(False), str(tmp_path))
    report = (tmp_path / "executions" / "LOGIN-1.md").read_text()
    assert "**FAILED**" in report
    assert "1 step(s) failed out of 2" in report


def test_failures_also_produce_error_files(tmp_path):
    _write_error_reports(result_for(False), str(tmp_path))
    assert list((tmp_path / "errors").glob("*"))


def test_collect_failures_finds_a_report_the_runner_wrote(tmp_path):
    """The two halves of the repair loop must agree on the report format."""
    run_dir = tmp_path / "runs" / "current"
    _write_execution_report(result_for(False), str(run_dir))
    output = tmp_path / "failures.json"
    CliRunner().invoke(main, ["collect-failures", f"--run-dir={run_dir}", f"--output={output}"])
    assert [entry["key"] for entry in json.loads(output.read_text())] == ["LOGIN-1"]


# --- dashboards ------------------------------------------------------------


def test_dashboard_data_summarizes_a_run(tmp_path):
    run_dir = tmp_path / "runs" / "current"
    _write_execution_report(result_for(True), str(run_dir))
    _write_execution_report(result_for(False), str(run_dir))
    data = build_dashboard_data(run_dir, str(tmp_path))
    assert data["runTimestamp"] == "current"
    assert data["stories"]


def test_a_dashboard_renders_from_that_data(tmp_path):
    run_dir = tmp_path / "runs" / "current"
    _write_execution_report(result_for(False), str(run_dir))
    config = ExecConfig(output_dir=str(tmp_path))
    path = generate_dashboard(str(run_dir), build_dashboard_data(run_dir, str(tmp_path)), config)
    assert path.is_file()
    assert "<html" in path.read_text().lower()


def test_the_index_lists_the_runs(tmp_path):
    run_dir = tmp_path / "runs" / "current"
    _write_execution_report(result_for(True), str(run_dir))
    config = ExecConfig(output_dir=str(tmp_path))
    generate_dashboard(str(run_dir), build_dashboard_data(run_dir, str(tmp_path)), config)
    index = generate_index_page(str(tmp_path), config)
    assert index.is_file()


def test_dashboard_data_for_an_empty_run(tmp_path):
    run_dir = tmp_path / "runs" / "current"
    (run_dir / "executions").mkdir(parents=True)
    data = build_dashboard_data(run_dir, str(tmp_path))
    assert data["stories"] == []
