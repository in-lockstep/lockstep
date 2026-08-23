"""The parts of the extracted test runner that do not need a live application.

Script loading, tag filtering and change detection are pure logic and carry real behaviour — tag
filtering in particular is how a pipeline excludes tests for features that are not built yet.
"""

from __future__ import annotations

import json

from pipeline_exec.builtins.test_runner import (
    _filter_by_tags,
    _filter_changed_only,
    _parse_test_script,
    load_test_scripts,
)
from pipeline_exec.config import ExecConfig
from pipeline_exec.executors.types import TestScript

SCRIPT = {
    "storyId": "LOGIN-1",
    "summary": "User can log in",
    "testType": "ui",
    "tags": ["smoke", "auth"],
    "setupSteps": [{"step": 1, "tool": "browser", "action": "navigate", "params": {"url": "/"}}],
    "testSteps": [{"step": 2, "tool": "browser", "action": "click", "expected": "dashboard"}],
    "teardownSteps": [],
    "executionTier": 2,
}


def write_scripts(tmp_path, *scripts):
    scripts_dir = tmp_path / "test-scripts"
    scripts_dir.mkdir(exist_ok=True)
    for script in scripts:
        (scripts_dir / f"{script['storyId']}.json").write_text(json.dumps(script), encoding="utf-8")
    return scripts_dir


def config_for(tmp_path, **overrides):
    return ExecConfig(
        output_dir=str(tmp_path),
        scripts_dir=str(tmp_path / "test-scripts"),
        tags_file=str(tmp_path / ".env-tests"),
        **overrides,
    )


def test_a_script_parses_into_its_phases():
    script = _parse_test_script(SCRIPT)
    assert script.story_id == "LOGIN-1"
    assert script.test_type == "ui"
    assert script.execution_tier == 2
    assert [step.action for step in script.setup_steps] == ["navigate"]
    assert script.test_steps[0].expected == "dashboard"


def test_scripts_load_from_the_committed_directory(tmp_path):
    write_scripts(tmp_path, SCRIPT)
    assert [s.story_id for s in load_test_scripts(config_for(tmp_path))] == ["LOGIN-1"]


def test_an_absent_scripts_directory_yields_nothing(tmp_path):
    assert load_test_scripts(config_for(tmp_path)) == []


def test_an_invalid_script_is_skipped_rather_than_fatal(tmp_path):
    scripts_dir = write_scripts(tmp_path, SCRIPT)
    (scripts_dir / "broken.json").write_text("{not json", encoding="utf-8")
    assert [s.story_id for s in load_test_scripts(config_for(tmp_path))] == ["LOGIN-1"]


def test_tags_marked_skip_are_excluded(tmp_path):
    (tmp_path / ".env-tests").write_text("TAG_metrics=skip\n", encoding="utf-8")
    scripts = [TestScript(story_id="a", tags=["metrics"]), TestScript(story_id="b", tags=["smoke"])]
    assert [s.story_id for s in _filter_by_tags(scripts, config_for(tmp_path))] == ["b"]


def test_conditional_skip_applies_when_its_variable_is_unset(tmp_path, monkeypatch):
    """Replaces the framework's hardcoded OCP rule with one the pipeline declares for itself."""
    (tmp_path / ".env-tests").write_text("TAG_ocp=skip-unless-env:OCP_API_URL\n", encoding="utf-8")
    monkeypatch.delenv("OCP_API_URL", raising=False)
    scripts = [TestScript(story_id="a", tags=["ocp"])]
    assert _filter_by_tags(scripts, config_for(tmp_path)) == []


def test_conditional_skip_lifts_when_its_variable_is_set(tmp_path, monkeypatch):
    (tmp_path / ".env-tests").write_text("TAG_ocp=skip-unless-env:OCP_API_URL\n", encoding="utf-8")
    monkeypatch.setenv("OCP_API_URL", "https://ocp.example")
    scripts = [TestScript(story_id="a", tags=["ocp"])]
    assert [s.story_id for s in _filter_by_tags(scripts, config_for(tmp_path))] == ["a"]


def test_no_tag_file_means_no_filtering(tmp_path):
    scripts = [TestScript(story_id="a", tags=["anything"])]
    assert len(_filter_by_tags(scripts, config_for(tmp_path))) == 1


def test_tag_matching_ignores_case(tmp_path):
    (tmp_path / ".env-tests").write_text("TAG_METRICS=skip\n", encoding="utf-8")
    scripts = [TestScript(story_id="a", tags=["Metrics"])]
    assert _filter_by_tags(scripts, config_for(tmp_path)) == []


def test_changed_only_keeps_scripts_newer_than_their_last_execution(tmp_path):
    import os
    import time

    write_scripts(tmp_path, SCRIPT)
    executions = tmp_path / "runs" / "latest" / "executions"
    executions.mkdir(parents=True)
    report = executions / "LOGIN-1.md"
    report.write_text("ran", encoding="utf-8")
    old = time.time() - 3600
    os.utime(report, (old, old))

    scripts = [TestScript(story_id="LOGIN-1")]
    assert len(_filter_changed_only(scripts, config_for(tmp_path))) == 1


def test_changed_only_drops_scripts_older_than_their_last_execution(tmp_path):
    import os
    import time

    write_scripts(tmp_path, SCRIPT)
    old = time.time() - 3600
    os.utime(tmp_path / "test-scripts" / "LOGIN-1.json", (old, old))
    executions = tmp_path / "runs" / "latest" / "executions"
    executions.mkdir(parents=True)
    (executions / "LOGIN-1.md").write_text("ran", encoding="utf-8")

    scripts = [TestScript(story_id="LOGIN-1")]
    assert _filter_changed_only(scripts, config_for(tmp_path)) == []
