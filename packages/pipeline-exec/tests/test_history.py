"""The run ledger, and what it can honestly say changed.

A dashboard shows today. The question this exists for is whether things are getting better or worse,
which needs two windows and needs both to still exist — artifacts expire and job logs rotate, so a
repository that retains nothing cannot answer it at all.

Three properties matter more than the arithmetic, and each is about refusing to state a direction the
data does not support: a window too small reports no trend, a metric nothing measured is absent
rather than zero, and a comparison with no baseline says it is a snapshot.
"""

from __future__ import annotations

import json

from pipeline_exec.history import (
    Report,
    Stat,
    by_agent,
    by_workflow,
    compare,
    outliers,
    read_ledger,
    split_windows,
)


def record(
    day,
    *,
    workflow="review",
    credits=60.0,
    seconds=200.0,
    agent="security-reviewer",
    outcome="success",
    failed=None,
    attempt=1,
    run="1",
):
    return {
        "run_id": run,
        "run_url": f"https://x/{run}",
        "workflow": workflow,
        "finished": f"2026-08-{day:02d}T10:00:00+00:00",
        "attempt": attempt,
        "credits": credits,
        "cost_usd": round(credits * 0.002, 4),
        "wall_seconds": seconds,
        "failed": failed or [],
        "agents": {agent: {"outcome": outcome, "seconds": seconds}},
    }


def ledger(tmp_path, records, name="2026-08.jsonl"):
    directory = tmp_path / "history"
    directory.mkdir(exist_ok=True)
    (directory / name).write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))
    return directory


# --- reading ----------------------------------------------------------------


def test_records_come_back_oldest_first(tmp_path):
    directory = ledger(tmp_path, [record(9, run="b"), record(2, run="a")])
    assert [r["run_id"] for r in read_ledger(directory)] == ["a", "b"]


def test_several_months_are_one_ledger(tmp_path):
    directory = ledger(tmp_path, [record(2, run="aug")])
    (directory / "2026-07.jsonl").write_text(
        json.dumps({**record(2, run="jul"), "finished": "2026-07-02T10:00:00+00:00"}) + "\n"
    )
    assert [r["run_id"] for r in read_ledger(directory)] == ["jul", "aug"]


def test_one_bad_line_does_not_make_the_history_unreadable(tmp_path):
    """The ledger is append-only and shared. One bad write must not cost every other run."""
    directory = ledger(tmp_path, [record(1, run="a")])
    with (directory / "2026-08.jsonl").open("a") as handle:
        handle.write("{not json\n")
        handle.write(json.dumps(record(2, run="b")) + "\n")
    assert [r["run_id"] for r in read_ledger(directory)] == ["a", "b"]


def test_a_record_with_no_timestamp_is_skipped(tmp_path):
    """Nothing can be windowed by it, so counting it would put it in both windows or neither."""
    directory = ledger(tmp_path, [record(1, run="a")])
    with (directory / "2026-08.jsonl").open("a") as handle:
        handle.write(json.dumps({"run_id": "b", "credits": 5}) + "\n")
    assert [r["run_id"] for r in read_ledger(directory)] == ["a"]


# --- windows ----------------------------------------------------------------


def test_the_window_splits_where_it_is_told():
    records = [record(d, run=str(d)) for d in (1, 5, 20, 25)]
    before, after = split_windows(records, at="2026-08-15")
    assert [r["run_id"] for r in before] == ["1", "5"]
    assert [r["run_id"] for r in after] == ["20", "25"]


def test_too_few_runs_is_reported_as_that_not_as_no_change():
    """Noise presented as a direction is worse than silence. Somebody acts on it."""
    before = {"a": Stat(runs=3, failures=0, seconds=300)}
    after = {"a": Stat(runs=4, failures=1, seconds=400)}
    entry = compare(before, after)[0]
    assert entry["change"] == "too few runs"
    assert "deltas" not in entry


def test_enough_runs_produces_a_delta():
    before = {"a": Stat(runs=10, failures=0, seconds=2000)}
    after = {"a": Stat(runs=10, failures=3, seconds=3000)}
    entry = compare(before, after)[0]
    assert entry["change"] == "compared"
    assert entry["deltas"]["failure_rate"] == 0.3
    assert entry["deltas"]["mean_seconds"] == 100.0


def test_a_subject_with_no_baseline_is_new_not_a_regression():
    """There is nothing for it to have moved from, and inventing one makes a first run a regression."""
    entry = compare({}, {"a": Stat(runs=10, failures=5, seconds=100)})[0]
    assert entry["change"] == "new"
    assert entry["before"] is None


def test_a_subject_that_stopped_running_is_reported_as_gone():
    assert compare({"a": Stat(runs=10)}, {})[0]["change"] == "gone"


# --- unmeasured is not zero -------------------------------------------------


def test_per_agent_credits_are_absent_rather_than_zero(tmp_path):
    """The ledger records what a *run* spent and cannot attribute it to the agents inside it.

    A delta of 0.0 would say "unchanged" about a number nothing ever measured.
    """
    records = [record(d, run=str(d)) for d in range(1, 25)]
    before, after = split_windows(records, at="2026-08-13")
    entry = compare(by_agent(before), by_agent(after))[0]
    assert "mean_credits" not in entry["deltas"]
    assert "mean_credits" not in entry["after"]


def test_per_workflow_credits_are_measured_and_reported(tmp_path):
    records = [record(d, run=str(d), credits=60) for d in range(1, 13)]
    records += [record(d, run=str(d), credits=160) for d in range(13, 25)]
    before, after = split_windows(records, at="2026-08-13")
    entry = compare(by_workflow(before), by_workflow(after))[0]
    assert entry["deltas"]["mean_credits"] == 100.0


# --- outliers ---------------------------------------------------------------


def test_a_runaway_run_is_found_against_the_median():
    """Against the median, because one runaway drags a mean far enough to hide the next one."""
    records = [record(d, run=str(d), credits=60) for d in range(1, 12)]
    records.append(record(12, run="999", credits=900))
    found = outliers(records)
    assert [o.run_id for o in found] == ["999"]
    assert found[0].times_median == 15.0


def test_an_expensive_pipeline_is_not_an_outlier_against_a_cheap_one():
    """A review costing ten times a triage is two pipelines, not an anomaly."""
    records = [record(d, run=f"r{d}", workflow="review", credits=600) for d in range(1, 12)]
    records += [record(d, run=f"t{d}", workflow="triage", credits=20) for d in range(1, 12)]
    assert outliers(records) == []


def test_too_few_runs_to_have_a_median_reports_no_outlier():
    records = [record(1, run="a", credits=10), record(2, run="b", credits=900)]
    assert outliers(records) == []


# --- the report -------------------------------------------------------------


def test_a_report_with_no_baseline_says_it_is_a_snapshot():
    """Read as a trend, a first window makes everything in it look like a change."""
    report = Report(records=[record(1)], since="").build()
    assert report["window"]["compared"] is False


def test_reruns_are_counted_because_a_human_retrying_is_a_signal():
    records = [record(1, run="a"), record(2, run="b", attempt=2)]
    assert Report(records=records).build()["totals"]["reruns"] == 1


def test_a_failed_run_is_counted_by_what_failed_in_it():
    records = [record(1, run="a"), record(2, run="b", failed=["aw-tests-reviewer"])]
    assert Report(records=records).build()["totals"]["failed_runs"] == 1


# --- the commands -----------------------------------------------------------


def run_history(*args):
    from click.testing import CliRunner
    from pipeline_exec.cli import main

    return CliRunner().invoke(main, ["run-history", *args])


def test_the_command_reports_the_window(tmp_path):
    directory = ledger(tmp_path, [record(d, run=str(d)) for d in range(1, 25)])
    out = tmp_path / "report.json"
    result = run_history(f"--ledger={directory}", f"--output={out}", "--since=2026-08-13")
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["window"]["compared"] is True


def test_the_command_says_when_it_has_no_baseline(tmp_path):
    """A snapshot read as a trend makes everything in a first window look like a change."""
    directory = ledger(tmp_path, [record(1)])
    result = run_history(f"--ledger={directory}", f"--output={tmp_path / 'r.json'}")
    assert "snapshot, not a comparison" in result.output


def test_an_outlier_is_named_in_the_output(tmp_path):
    records = [record(d, run=str(d), credits=60) for d in range(1, 12)]
    records.append(record(12, run="999", credits=900))
    result = run_history(f"--ledger={ledger(tmp_path, records)}", f"--output={tmp_path / 'r.json'}")
    assert "9" in result.output and "median" in result.output


def test_a_ledger_that_does_not_exist_says_nothing_has_been_recorded(tmp_path):
    result = run_history(f"--ledger={tmp_path / 'nope'}", f"--output={tmp_path / 'r.json'}")
    assert result.exit_code != 0
    assert "nothing has been recorded" in result.output


def test_an_empty_ledger_is_an_error_rather_than_an_empty_report(tmp_path):
    result = run_history(f"--ledger={ledger(tmp_path, [])}", f"--output={tmp_path / 'r.json'}")
    assert result.exit_code != 0
    assert "no run records" in result.output


def test_neither_a_ledger_nor_a_branch_is_refused(tmp_path):
    result = run_history(f"--output={tmp_path / 'r.json'}")
    assert result.exit_code != 0
    assert "--ledger or --branch" in result.output


def test_a_branch_that_is_not_there_says_so(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    result = run_history("--branch=no-such-branch", f"--output={tmp_path / 'r.json'}")
    assert result.exit_code != 0
    assert "could not read the ledger branch" in result.output


def test_the_meter_appends_a_record_sharded_by_month(tmp_path, monkeypatch):
    """Appended rather than written: the publishing step retries by appending again."""
    from click.testing import CliRunner
    from pipeline_exec.cli import main

    usage = tmp_path / "usage"
    usage.mkdir()
    (usage / "agent_usage.json").write_text(json.dumps({"aic": 12, "model": "claude-sonnet-4-6"}))
    history = tmp_path / "hist"
    monkeypatch.setenv("GITHUB_RUN_ID", "4821")
    monkeypatch.setenv("GITHUB_WORKFLOW", "review")

    for _ in range(2):
        result = CliRunner().invoke(
            main,
            [
                "meter",
                f"--usage={usage}",
                f"--output={tmp_path / 'm.json'}",
                f"--history-dir={history}",
            ],
        )
        assert result.exit_code == 0, result.output

    written = sorted(history.glob("*.jsonl"))
    assert len(written) == 1 and written[0].name.endswith(".jsonl")
    lines = written[0].read_text().splitlines()
    assert len(lines) == 2, "the second run overwrote the first"
    assert json.loads(lines[0])["run_id"] == "4821"
    assert json.loads(lines[0])["workflow"] == "review"


def test_a_record_carries_no_content(tmp_path, monkeypatch):
    """This file is as readable as the repository, so a transcript in it is one in every clone."""
    from click.testing import CliRunner
    from pipeline_exec.cli import main

    usage = tmp_path / "usage"
    usage.mkdir()
    (usage / "agent_usage.json").write_text(
        json.dumps({"aic": 12, "model": "claude-sonnet-4-6", "prompt": "SECRET PROMPT TEXT"})
    )
    history = tmp_path / "hist"
    CliRunner().invoke(
        main, ["meter", f"--usage={usage}", f"--output={tmp_path / 'm.json'}", f"--history-dir={history}"]
    )
    assert "SECRET PROMPT TEXT" not in next(history.glob("*.jsonl")).read_text()
