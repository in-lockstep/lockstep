"""What a run did and what it cost, as metrics somebody else's dashboard can hold.

Two properties matter more than the rest, and both are about refusing a comfortable number.

A cost report that says $0.00 because it did not recognise a model name is the one number worse than
no report at all — so an unpriced model is carried through and named, and a run whose usage artifact
could not be found reports *nothing found* rather than zero.

And the reader infers which objects in gh-aw's artifact are measurements and which are totals of
them. That inference is what breaks quietly after an upstream change, so the totals gh-aw wrote are
reconciled against the totals we computed and a disagreement is reported rather than resolved.
"""

from __future__ import annotations

import json

from pipeline_exec.otel import (
    Job,
    gen_ai_system,
    metrics_document,
    price,
    read_jobs,
    read_usage,
    render_summary,
    run_shape,
)

RATES = {"claude-sonnet-4-6": 0.0021}


def usage(tmp_path, name, payload):
    directory = tmp_path / "usage"
    directory.mkdir(exist_ok=True)
    path = directory / name
    if name.endswith(".jsonl"):
        path.write_text("\n".join(json.dumps(item) for item in payload) + "\n")
    else:
        path.write_text(json.dumps(payload))
    return directory


NESTED = {
    "workflow": "aw-security-reviewer",
    "ai_credits": 63.5,
    "total_tokens": 184320,
    "cost": 0.71,
    "by_model": {
        "claude-sonnet-4-6-20260101": {"ai_credits": 40.0, "total_tokens": 120000},
        "gpt-5-mini": {"ai_credits": 23.5, "total_tokens": 64320},
    },
}


# --- reading ----------------------------------------------------------------


def test_a_total_over_its_own_parts_is_not_counted_twice(tmp_path):
    """The whole reason roll-ups are separated: summing both would double a bill."""
    records, rollups = read_usage(usage(tmp_path, "agent_usage.json", NESTED))
    assert sorted(r.credits for r in records) == [23.5, 40.0]
    assert [r.credits for r in rollups] == [63.5]


def test_a_model_named_by_its_key_rather_than_a_field_is_still_named(tmp_path):
    records, _ = read_usage(usage(tmp_path, "agent_usage.json", NESTED))
    assert sorted(r.model for r in records) == ["claude-sonnet-4-6-20260101", "gpt-5-mini"]


def test_jsonl_is_one_record_per_line(tmp_path):
    directory = usage(tmp_path, "agent_usage.jsonl", [{"aic": 12, "model": "claude-sonnet-4-6"}, {"aic": 3}])
    records, _ = read_usage(directory)
    assert sorted(r.credits for r in records) == [3.0, 12.0]


def test_a_flag_is_not_read_as_a_credit(tmp_path):
    """`bool` is an `int` in Python; `credits: true` would otherwise be one credit nobody spent."""
    records, _ = read_usage(usage(tmp_path, "a.json", {"credits": True, "model": "x"}))
    assert records == []


def test_an_unparseable_file_is_skipped_not_fatal(tmp_path):
    directory = tmp_path / "usage"
    directory.mkdir()
    (directory / "broken.json").write_text("{not json")
    (directory / "good.json").write_text(json.dumps({"aic": 5, "model": "claude-sonnet-4-6"}))
    records, _ = read_usage(directory)
    assert [r.credits for r in records] == [5.0]


# --- pricing ----------------------------------------------------------------


def test_a_dated_snapshot_prices_at_its_family_rate(tmp_path):
    """A table naming every dated snapshot would stop pricing the day a provider published one."""
    records, rollups = read_usage(usage(tmp_path, "a.json", NESTED))
    assert price(records, RATES, rollups).rate_for("claude-sonnet-4-6-20260101") == 0.0021


def test_an_unpriced_model_is_named_not_treated_as_free(tmp_path):
    records, rollups = read_usage(usage(tmp_path, "a.json", NESTED))
    summary = price(records, RATES, rollups).summary()
    assert summary["dollars"] == round(40.0 * 0.0021, 6)
    assert summary["unpriced_models"] == ["gpt-5-mini"]
    assert summary["priced_fraction"] < 1


def test_the_summary_says_the_cost_is_a_floor_when_something_is_unpriced(tmp_path):
    records, rollups = read_usage(usage(tmp_path, "a.json", NESTED))
    text = render_summary(price(records, RATES, rollups), title="Cost")
    assert "is a floor, not a total" in text
    assert "`gpt-5-mini`" in text


def test_nothing_found_is_reported_as_nothing_found(tmp_path):
    text = render_summary(price([], RATES), title="Cost")
    assert "reported as *nothing found* rather than as a cost of zero" in text


# --- reconciliation ---------------------------------------------------------


def test_a_total_that_agrees_with_its_parts_reconciles(tmp_path):
    records, rollups = read_usage(usage(tmp_path, "a.json", NESTED))
    check = price(records, RATES, rollups).crosscheck()
    assert check["agrees"] is True


def test_a_total_that_does_not_add_up_is_reported(tmp_path):
    """The reader's guess at gh-aw's shape failing quietly is the risk this exists to remove."""
    wrong = {**NESTED, "ai_credits": 999}
    records, rollups = read_usage(usage(tmp_path, "a.json", wrong))
    priced = price(records, RATES, rollups)
    assert priced.crosscheck()["agrees"] is False
    assert "do not reconcile" in render_summary(priced, title="Cost")


def test_a_file_with_no_total_does_not_drag_another_files_into_disagreement(tmp_path):
    """A perfectly healthy multi-agent run must not report that it does not reconcile."""
    directory = usage(tmp_path, "one.json", NESTED)
    (directory / "two.jsonl").write_text(json.dumps({"aic": 12, "model": "claude-sonnet-4-6"}) + "\n")
    records, rollups = read_usage(directory)
    assert price(records, RATES, rollups).crosscheck()["agrees"] is True


def test_with_no_totals_at_all_there_is_nothing_to_reconcile(tmp_path):
    directory = usage(tmp_path, "flat.jsonl", [{"aic": 12, "model": "claude-sonnet-4-6"}])
    records, rollups = read_usage(directory)
    assert price(records, RATES, rollups).crosscheck() == {"available": False}


# --- what the run did -------------------------------------------------------

JOBS = {
    "jobs": [
        {
            "name": "aw-security-reviewer",
            "conclusion": "success",
            "status": "completed",
            "created_at": "2026-08-24T10:00:00Z",
            "started_at": "2026-08-24T10:00:30Z",
            "completed_at": "2026-08-24T10:04:30Z",
        },
        {
            "name": "post-reviews",
            "conclusion": "failure",
            "status": "completed",
            "created_at": "2026-08-24T10:00:00Z",
            "started_at": "2026-08-24T10:04:30Z",
            "completed_at": "2026-08-24T10:05:00Z",
        },
        {"name": "meter", "status": "in_progress"},
    ]
}


def jobs_file(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(JOBS))
    return path


def test_the_job_reading_this_does_not_report_its_own_duration(tmp_path):
    """A job cannot honestly time itself while it is still running."""
    assert [job.name for job in read_jobs(jobs_file(tmp_path))] == [
        "aw-security-reviewer",
        "post-reviews",
    ]


def test_waiting_is_separated_from_working(tmp_path):
    jobs = read_jobs(jobs_file(tmp_path))
    assert jobs[0].start_delay_seconds == 30.0
    assert jobs[0].duration_seconds == 240.0


def test_a_dependent_jobs_wait_is_not_reported_as_a_queue(tmp_path):
    """`post` started four minutes in because it waited for the reviewer, not for a runner.

    Every job's `created_at` is stamped when the *run* is created, so treating that gap as queue
    time would report a perfectly healthy pipeline as starved of capacity. Only the first job to
    start says anything about runner availability.
    """
    shape = run_shape(read_jobs(jobs_file(tmp_path)))
    assert shape["pickup_seconds"] == 30.0
    assert "max_queue_seconds" not in shape


def test_wall_clock_is_not_the_sum_of_a_fan_out(tmp_path):
    """Twelve reviewers finishing in four minutes took four minutes, not forty-eight."""
    shape = run_shape(read_jobs(jobs_file(tmp_path)))
    assert shape["wall_seconds"] == 240.0
    assert shape["busy_seconds"] == 270.0


def test_failures_are_named_not_just_counted(tmp_path):
    shape = run_shape(read_jobs(jobs_file(tmp_path)))
    assert shape["failed"] == ["post-reviews"]
    assert shape["outcomes"] == {"success": 1, "failure": 1}


def test_a_missing_jobs_file_is_not_an_error(tmp_path):
    assert read_jobs(tmp_path / "nothing.json") == []


def test_a_clock_that_runs_backwards_is_reported_as_unknown(tmp_path):
    job = Job(name="x", started_at="2026-08-24T10:05:00Z", completed_at="2026-08-24T10:00:00Z")
    assert job.duration_seconds is None


# --- the document -----------------------------------------------------------


def names(document):
    return [m["name"] for m in document["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]]


def attributes(point):
    return {a["key"]: list(a["value"].values())[0] for a in point["attributes"]}


def test_the_document_carries_more_than_cost(tmp_path):
    records, rollups = read_usage(usage(tmp_path, "a.json", NESTED))
    document = metrics_document(
        price(records, RATES, rollups),
        resource={"service.name": "pr-review"},
        nanos=1,
        jobs=read_jobs(jobs_file(tmp_path)),
    )
    emitted = names(document)
    for expected in (
        "lockstep.run.credits",
        "lockstep.run.cost.usd",
        "lockstep.run.duration",
        "lockstep.run.busy",
        "lockstep.run.jobs",
        "lockstep.job.duration",
        "lockstep.job.start_delay",
    ):
        assert expected in emitted


def test_a_run_with_no_job_data_still_reports_its_cost(tmp_path):
    records, rollups = read_usage(usage(tmp_path, "a.json", NESTED))
    document = metrics_document(price(records, RATES, rollups), resource={}, nanos=1)
    assert "lockstep.run.credits" in names(document)
    assert "lockstep.job.duration" not in names(document)


def test_per_model_points_follow_the_genai_conventions(tmp_path):
    """So a backend that already understands agent workloads groups these without being taught to."""
    records, rollups = read_usage(usage(tmp_path, "a.json", NESTED))
    document = metrics_document(price(records, RATES, rollups), resource={}, nanos=1)
    metric = next(
        m
        for m in document["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        if m["name"] == "lockstep.run.credits.by_model"
    )
    tagged = [attributes(p) for p in metric["gauge"]["dataPoints"]]
    assert {"claude-sonnet-4-6-20260101", "gpt-5-mini"} == {t["gen_ai.request.model"] for t in tagged}
    assert "anthropic" in {t.get("gen_ai.system") for t in tagged}


def test_an_agent_job_is_attributed_to_its_agent(tmp_path):
    """A per-agent error rate answers "where should improvement effort go"."""
    document = metrics_document(price([], RATES), resource={}, nanos=1, jobs=read_jobs(jobs_file(tmp_path)))
    metric = next(
        m
        for m in document["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        if m["name"] == "lockstep.job.duration"
    )
    tagged = {attributes(p).get("gen_ai.agent.name") for p in metric["gauge"]["dataPoints"]}
    assert "security-reviewer" in tagged


def test_an_unrecognised_model_family_gets_no_invented_provider():
    assert gen_ai_system("claude-sonnet-4-6") == "anthropic"
    assert gen_ai_system("gpt-5-mini") == "openai"
    assert gen_ai_system("llama-9") == ""
