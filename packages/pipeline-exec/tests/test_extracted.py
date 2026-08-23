"""The commands extracted from pipeline-framework."""

from __future__ import annotations

import json

from click.testing import CliRunner
from pipeline_exec.cli import main
from pipeline_exec.config import ExecConfig


def run(*args):
    return CliRunner().invoke(main, list(args))


def make_run_dir(tmp_path, reports):
    run_dir = tmp_path / "runs" / "current"
    (run_dir / "executions").mkdir(parents=True)
    for name, body in reports.items():
        (run_dir / "executions" / f"{name}.md").write_text(body, encoding="utf-8")
    return run_dir


# --- configuration ---------------------------------------------------------


def test_config_reads_the_profile_environment_the_compiler_exports(monkeypatch):
    monkeypatch.setenv("PROFILE_URL", "https://app.example")
    monkeypatch.setenv("PROFILE_API_URL", "https://api.example")
    monkeypatch.setenv("PROFILE_PASSWORD", "secret")
    monkeypatch.setenv("PROFILE_AUTH_METHOD", "basic")
    config = ExecConfig.from_env()
    assert config.profile_url == "https://app.example"
    assert config.profile_api_url == "https://api.example"
    assert config.profile_password == "secret"
    assert config.profile_auth_method == "basic"


def test_cli_overrides_beat_the_environment(monkeypatch):
    monkeypatch.setenv("PROFILE_URL", "https://from-env")
    assert ExecConfig.from_env(profile_url="https://from-cli").profile_url == "https://from-cli"


def test_empty_overrides_do_not_clobber_the_environment(monkeypatch):
    """Click passes "" for unset options; that must not erase a value the job exported."""
    monkeypatch.setenv("PROFILE_URL", "https://from-env")
    assert ExecConfig.from_env(profile_url="").profile_url == "https://from-env"


def test_test_scripts_default_to_the_committed_location():
    """Compiled pipelines run reviewed scripts committed to the repo, not artefacts under outputs/."""
    config = ExecConfig()
    assert config.scripts_dir == "test-scripts"
    assert config.tags_file == ".env-tests"


def test_report_templates_ship_inside_the_package():
    assert (ExecConfig().framework_dir / "reports" / "templates" / "dashboard.html.j2").is_file()


# --- step outputs ----------------------------------------------------------


def test_outputs_go_to_github_output_when_present(tmp_path, monkeypatch):
    target = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(target))
    source = tmp_path / "items.json"
    source.write_text(json.dumps([{"key": "A"}]), encoding="utf-8")

    run("fanout", f"--input={source}")
    assert 'items=[{"key":"A"}]' in target.read_text()


# --- collect-failures ------------------------------------------------------


def test_collect_failures_selects_only_failed_reports(tmp_path):
    run_dir = make_run_dir(
        tmp_path,
        {"login": "Status: **FAILED**\nselector missing", "signup": "Status: **PASSED**"},
    )
    scripts = tmp_path / "test-scripts"
    scripts.mkdir()
    (scripts / "login.json").write_text('{"storyId": "login"}', encoding="utf-8")
    output = tmp_path / "failures.json"

    result = run("collect-failures", f"--run-dir={run_dir}", f"--output={output}", f"--scripts-dir={scripts}")
    assert result.exit_code == 0
    failures = json.loads(output.read_text())
    assert [entry["story_id"] for entry in failures] == ["login"]
    assert failures[0]["test_script"] == '{"storyId": "login"}'


def test_collected_failures_carry_a_key_for_fan_out(tmp_path):
    """The repair loop fans out over these, and fanout keys on `key`."""
    run_dir = make_run_dir(tmp_path, {"login": "**FAILED**"})
    output = tmp_path / "failures.json"
    run("collect-failures", f"--run-dir={run_dir}", f"--output={output}")
    assert json.loads(output.read_text())[0]["key"] == "login"


def test_collect_failures_on_a_run_with_no_executions(tmp_path):
    output = tmp_path / "failures.json"
    result = run("collect-failures", f"--run-dir={tmp_path / 'missing'}", f"--output={output}")
    assert result.exit_code == 0
    assert json.loads(output.read_text()) == []


def test_collect_failures_truncates_long_reports(tmp_path):
    run_dir = make_run_dir(tmp_path, {"login": "**FAILED**\n" + "x" * 9000})
    output = tmp_path / "failures.json"
    run("collect-failures", f"--run-dir={run_dir}", f"--output={output}", "--report-chars=100")
    assert len(json.loads(output.read_text())[0]["report"]) == 100


# --- check-convergence -----------------------------------------------------


def write_classifications(tmp_path, categories):
    run_dir = tmp_path / "runs" / "current"
    run_dir.mkdir(parents=True)
    (run_dir / "heal-classifications.json").write_text(
        json.dumps({f"s{i}": {"category": c} for i, c in enumerate(categories)}), encoding="utf-8"
    )
    return run_dir


def test_convergence_is_reported_not_signalled_by_exit_code(tmp_path):
    """A non-zero exit would fail the very job that exists to read the answer."""
    run_dir = write_classifications(tmp_path, ["script_bug", "app_bug"])
    result = run("check-convergence", f"--run-dir={run_dir}")
    assert result.exit_code == 0
    assert "converged=false" in result.output


def test_convergence_when_no_script_bugs_remain(tmp_path):
    run_dir = write_classifications(tmp_path, ["app_bug", "infra_issue"])
    result = run("check-convergence", f"--run-dir={run_dir}")
    assert result.exit_code == 0
    assert "converged=true" in result.output


def test_an_unclassified_run_counts_as_converged(tmp_path):
    result = run("check-convergence", f"--run-dir={tmp_path}")
    assert result.exit_code == 0
    assert "converged=true" in result.output


def test_convergence_detail_goes_to_stderr(tmp_path, monkeypatch):
    target = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(target))
    run_dir = write_classifications(tmp_path, ["script_bug", "script_bug", "app_bug"])
    result = run("check-convergence", f"--run-dir={run_dir}")
    assert "converged=false" in target.read_text()
    assert json.loads(result.stderr)["script_bugs"] == 2


# --- report ----------------------------------------------------------------


def test_report_renders_a_dashboard(tmp_path):
    run_dir = make_run_dir(tmp_path, {"login": "# login\n\nStatus: **PASSED**\n"})
    result = run("report", f"--run-dir={run_dir}", f"--output-dir={tmp_path}", "--no-index")
    assert result.exit_code == 0
    assert (run_dir / "dashboard.html").is_file()


def test_report_on_a_missing_run_is_not_a_failure(tmp_path):
    result = run("report", f"--run-dir={tmp_path / 'nope'}")
    assert result.exit_code == 0
    assert "nothing to report" in result.output


# --- test-runner -----------------------------------------------------------


def test_test_runner_with_no_scripts_reports_an_empty_run(tmp_path):
    result = run(
        "test-runner",
        f"--scripts-dir={tmp_path / 'none'}",
        f"--run-dir={tmp_path / 'run'}",
        f"--output-dir={tmp_path}",
    )
    assert result.exit_code == 0
    assert json.loads(result.output.strip().splitlines()[-1])["total"] == 0
