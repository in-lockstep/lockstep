"""The command surface the compiler emits."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner
from pipeline_exec.cli import main


def run(*args):
    return CliRunner().invoke(main, list(args))


def outputs(result):
    return dict(line.split("=", 1) for line in result.output.strip().splitlines() if "=" in line)


@pytest.fixture
def items_file(tmp_path):
    path = tmp_path / "items.json"
    path.write_text(json.dumps([{"key": "A"}, {"key": "B"}, {"key": "C"}]), encoding="utf-8")
    return path


# --- fanout ----------------------------------------------------------------


def test_fanout_emits_a_matrix_of_items(items_file):
    result = run("fanout", f"--input={items_file}")
    assert result.exit_code == 0
    values = outputs(result)
    assert json.loads(values["items"]) == [{"key": "A"}, {"key": "B"}, {"key": "C"}]
    assert values["mode"] == "items"
    assert values["count"] == "3"


def test_fanout_output_is_stable_across_runs(items_file):
    """The matrix expression lands in a committed lock file; unstable ordering would churn diffs."""
    assert run("fanout", f"--input={items_file}").output == run("fanout", f"--input={items_file}").output


def test_only_missing_drops_covered_items(items_file, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "A.json").write_text("{}", encoding="utf-8")
    result = run("fanout", f"--input={items_file}", "--only-missing", f"--output-dir={out}")
    assert [item["key"] for item in json.loads(outputs(result)["items"])] == ["B", "C"]


def test_complete_coverage_emits_an_empty_matrix(items_file, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    for key in "ABC":
        (out / f"{key}.json").write_text("{}", encoding="utf-8")
    result = run("fanout", f"--input={items_file}", "--only-missing", f"--output-dir={out}")
    assert outputs(result)["items"] == "[]"
    assert outputs(result)["count"] == "0"


def test_fanout_refuses_more_items_than_the_matrix_cap(tmp_path):
    path = tmp_path / "many.json"
    path.write_text(json.dumps([{"key": f"K{n}"} for n in range(300)]), encoding="utf-8")
    result = run("fanout", f"--input={path}")
    assert result.exit_code == 1
    assert "matrix cap" in result.output


def test_above_the_threshold_fanout_emits_shards(tmp_path):
    """The compiler cannot know the item count, so this branch lives here, not in the emitter."""
    path = tmp_path / "many.json"
    path.write_text(json.dumps([{"key": f"K{n}"} for n in range(50)]), encoding="utf-8")
    result = run("fanout", f"--input={path}", "--shard-threshold=20", "--shards=4")
    values = outputs(result)
    assert values["mode"] == "shards"
    assert json.loads(values["items"]) == [
        {"key": "shard-0", "shard": 0, "shards": 4},
        {"key": "shard-1", "shard": 1, "shards": 4},
        {"key": "shard-2", "shard": 2, "shards": 4},
        {"key": "shard-3", "shard": 3, "shards": 4},
    ]


def test_below_the_threshold_fanout_still_emits_items(items_file):
    result = run("fanout", f"--input={items_file}", "--shard-threshold=20")
    assert outputs(result)["mode"] == "items"


def test_no_shard_forces_one_leg_per_item(tmp_path):
    """An agent leg is a whole gh-aw run; it cannot host more than one item."""
    path = tmp_path / "many.json"
    path.write_text(json.dumps([{"key": f"K{n}"} for n in range(50)]), encoding="utf-8")
    result = run("fanout", f"--input={path}", "--shard-threshold=20", "--no-shard")
    assert outputs(result)["mode"] == "items"
    assert len(json.loads(outputs(result)["items"])) == 50


def test_shard_count_never_exceeds_item_count(tmp_path):
    path = tmp_path / "few.json"
    path.write_text(json.dumps([{"key": f"K{n}"} for n in range(3)]), encoding="utf-8")
    result = run("fanout", f"--input={path}", "--shard-threshold=2", "--shards=8")
    assert len(json.loads(outputs(result)["items"])) == 3


def test_fanout_reports_a_missing_input(tmp_path):
    result = run("fanout", f"--input={tmp_path / 'nope.json'}")
    assert result.exit_code == 1


# --- shard-run -------------------------------------------------------------


def test_shard_run_executes_once_per_item_in_the_slice(items_file, tmp_path):
    marker = tmp_path / "seen.txt"
    result = run(
        "shard-run",
        '--slice={"shard":0,"shards":1}',
        f"--input={items_file}",
        "--",
        "sh",
        "-c",
        f"echo {{item.key}} >> {marker}",
    )
    assert result.exit_code == 0
    assert marker.read_text().split() == ["A", "B", "C"]


def test_shard_run_accepts_a_single_item_slice(items_file, tmp_path):
    marker = tmp_path / "seen.txt"
    result = run(
        "shard-run",
        '--slice={"key":"A"}',
        f"--input={items_file}",
        "--",
        "sh",
        "-c",
        f"echo {{item.key}} >> {marker}",
    )
    assert result.exit_code == 0
    assert marker.read_text().split() == ["A"]


def test_shard_run_substitutes_the_whole_item_as_json(items_file, tmp_path):
    marker = tmp_path / "seen.txt"
    run(
        "shard-run",
        '--slice={"key":"A"}',
        f"--input={items_file}",
        "--",
        "sh",
        "-c",
        f"echo '{{item}}' >> {marker}",
    )
    assert json.loads(marker.read_text().strip()) == {"key": "A"}


def test_shard_run_completes_every_item_then_fails(items_file, tmp_path):
    """A bad item costs its own output, not the rest of the slice — the leg still fails."""
    marker = tmp_path / "seen.txt"
    result = run(
        "shard-run",
        '--slice={"shard":0,"shards":1}',
        f"--input={items_file}",
        "--",
        "sh",
        "-c",
        f"echo {{item.key}} >> {marker}; test {{item.key}} != B",
    )
    assert result.exit_code == 1
    assert marker.read_text().split() == ["A", "B", "C"]
    assert "failed items: B" in result.output


def test_shard_run_rejects_a_malformed_slice(items_file):
    result = run("shard-run", "--slice=not-json", f"--input={items_file}", "--", "true")
    assert result.exit_code == 1
    assert "not valid JSON" in result.output


def test_an_empty_shard_is_not_a_failure(items_file):
    result = run("shard-run", '--slice={"shard":5,"shards":6}', f"--input={items_file}", "--", "false")
    assert result.exit_code == 0


# --- fanout-verify ---------------------------------------------------------


def test_verify_passes_when_coverage_is_complete(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    for key in "AB":
        (out / f"{key}.json").write_text("{}", encoding="utf-8")
    result = run("fanout-verify", f"--dir={out}", '--expected=[{"key":"A"},{"key":"B"}]')
    assert result.exit_code == 0
    assert "coverage 2/2 (100%)" in result.output


def test_verify_tolerates_a_shortfall_within_the_declared_rate(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "A.json").write_text("{}", encoding="utf-8")
    result = run(
        "fanout-verify",
        f"--dir={out}",
        '--expected=[{"key":"A"},{"key":"B"}]',
        "--min-success-rate=0.5",
    )
    assert result.exit_code == 0


def test_verify_fails_below_the_declared_rate(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "A.json").write_text("{}", encoding="utf-8")
    result = run(
        "fanout-verify",
        f"--dir={out}",
        '--expected=[{"key":"A"},{"key":"B"}]',
        "--min-success-rate=0.9",
    )
    assert result.exit_code == 1
    assert "below the required" in result.output
    assert "missing: B" in result.output


def test_verify_of_an_empty_fanout_is_vacuously_complete(tmp_path):
    result = run("fanout-verify", f"--dir={tmp_path}", "--expected=[]")
    assert result.exit_code == 0
    assert "vacuously" in result.output


def test_verify_writes_to_the_step_summary(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    out = tmp_path / "out"
    out.mkdir()
    run("fanout-verify", f"--dir={out}", '--expected=[{"key":"A"}]', "--min-success-rate=0.0")
    assert "Fan-out coverage" in summary.read_text()


def test_verify_requires_an_expectation(tmp_path):
    result = run("fanout-verify", f"--dir={tmp_path}")
    assert result.exit_code == 1
    assert "--expected" in result.output


# --- validate-schema -------------------------------------------------------


def test_validation_rejects_malformed_json(tmp_path):
    (tmp_path / "a.json").write_text("{nope", encoding="utf-8")
    result = run("validate-schema", f"--dir={tmp_path}")
    assert result.exit_code == 1
    assert "not valid JSON" in result.output


def test_validation_enforces_required_keys(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"title": "x"}), encoding="utf-8")
    result = run("validate-schema", f"--dir={tmp_path}", "--require=title,criteria")
    assert result.exit_code == 1
    assert "criteria" in result.output


def test_validation_strips_markup_and_control_characters(tmp_path):
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"title": "<script>alert(1)</script>ok\x00"}), encoding="utf-8")
    assert run("validate-schema", f"--dir={tmp_path}").exit_code == 0
    assert json.loads(path.read_text())["title"] == "alert(1)ok"


def test_validation_caps_field_length(tmp_path):
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"body": "x" * 100}), encoding="utf-8")
    run("validate-schema", f"--dir={tmp_path}", "--max-field-length=10")
    assert json.loads(path.read_text())["body"].endswith("[truncated]")


def test_check_mode_reports_without_rewriting(tmp_path):
    path = tmp_path / "a.json"
    original = json.dumps({"title": "<b>x</b>"})
    path.write_text(original, encoding="utf-8")
    result = run("validate-schema", f"--dir={tmp_path}", "--check")
    assert result.exit_code == 0
    assert "1 sanitized" in result.output
    assert path.read_text() == original


def test_an_absent_output_directory_is_not_a_failure(tmp_path):
    """The producing step may have been skipped; failing here would mask the real cause."""
    result = run("validate-schema", f"--dir={tmp_path / 'missing'}")
    assert result.exit_code == 0
    assert "nothing to validate" in result.output


def test_validation_requires_a_target():
    result = run("validate-schema")
    assert result.exit_code == 1


# --- wait-for --------------------------------------------------------------


def test_wait_for_times_out_with_a_clear_message():
    result = run("wait-for", "--url=http://127.0.0.1:1/never", "--timeout=1", "--interval=1")
    assert result.exit_code == 1
    assert "timed out" in result.output
