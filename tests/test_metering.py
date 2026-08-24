"""The job that says what a run did and what it cost.

Cost is the last of the five questions an operator asks and the only one credits answer. These hold
the other four to the same standard: did it work, how long did it take, how long did it wait, and
which agent was responsible.

The property worth protecting above the rest is that the meter runs when the pipeline *failed*. A
run that fell over still spent what it spent, and the runs whose cost is most worth knowing are
disproportionately the ones that went wrong.
"""

from __future__ import annotations

import json

import yaml

from lockstep.emit import compile_spec
from lockstep.spec.load import load_spec


def enable(root, block="otel:\n  export: artifact\n  pricing:\n    claude-sonnet-4-6: 0.0021\n"):
    manifest = root / "pipeline.yaml"
    manifest.write_text(manifest.read_text() + "\n" + block, encoding="utf-8")
    # The artifact actions are pinned only where they are used, so a fixture that turns metering on
    # has to pin them too — which is the behaviour under test in the pin cases below.
    lock = root / ".pipeline" / "pins.lock"
    data = json.loads(lock.read_text())
    for action in ("actions/download-artifact", "actions/upload-artifact"):
        data["external"][action] = {"sha": "0" * 40, "tag": "v5"}
    lock.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return root


def meter_of(root):
    """The metering job, from whichever command workflow has agents to meter.

    Searched rather than named: a command with no agent steps gets no meter job, which is correct
    and would otherwise make this look like a feature that does not work.
    """
    for path, text in sorted(compile_spec(root).files.items()):
        if not path.endswith(".yml") or "/aw-" in path or "evals" in path:
            continue
        jobs = (yaml.safe_load(text) or {}).get("jobs") or {}
        if "meter" in jobs:
            return jobs["meter"]
    return None


# --- when it exists ---------------------------------------------------------


def test_no_metering_unless_a_pipeline_asks_for_it(basic_root):
    """A cost report with no rate table in it is a column of dollar signs in front of zeroes."""
    assert meter_of(basic_root) is None


def test_declaring_an_export_adds_the_job(basic_root):
    assert meter_of(enable(basic_root)) is not None


def test_it_waits_on_the_agents_whose_spending_it_collects(basic_root):
    job = meter_of(enable(basic_root))
    assert job["needs"], "a meter that waits on nothing collects nothing"


# --- the one that matters ---------------------------------------------------


def test_it_still_runs_when_the_pipeline_failed(basic_root):
    """`always()` would have been silently narrowed to `!failure()` by the skip guard.

    A job whose condition does not mention `cancelled()` has `!failure() && !cancelled()` folded
    into it, and `always() && !failure()` is `!failure()`. The meter would have skipped exactly the
    runs whose cost somebody wants to look up.
    """
    condition = meter_of(enable(basic_root))["if"]
    assert "!cancelled()" in condition
    assert "!failure()" not in condition


def test_it_cannot_turn_a_green_pipeline_red(basic_root):
    """Bookkeeping about finished work. A collector being down is not a build failure."""
    assert meter_of(enable(basic_root))["continue-on-error"] is True


# --- what it reads ----------------------------------------------------------


def _run(job):
    return " ".join(step.get("run", "") for step in job["steps"])


def test_it_asks_the_api_how_the_run_went(basic_root):
    """Outcomes, durations and queue times: four of the five operator questions."""
    job = meter_of(enable(basic_root))
    assert "actions/runs/$GITHUB_RUN_ID/jobs" in _run(job)
    assert job["permissions"]["actions"] == "read"


def test_it_collects_every_agents_usage_artifact(basic_root):
    job = meter_of(enable(basic_root))
    download = next(s for s in job["steps"] if "download-artifact" in str(s.get("uses", "")))
    assert download["with"]["pattern"] == "*usage*"
    # A pipeline whose conditional agents all sat this run out is not a broken pipeline.
    assert download["with"]["if-no-files-found"] == "ignore"


def test_the_rate_table_is_compiled_in_rather_than_read_from_a_file(basic_root):
    """So a change to what a credit costs shows up in a reviewable diff."""
    assert "claude-sonnet-4-6" in _run(meter_of(enable(basic_root)))


def test_an_endpoint_export_posts_and_an_artifact_export_uploads(basic_root):
    job = meter_of(enable(basic_root, "otel:\n  export: both\n  endpoint: https://otel.example/v1\n"))
    assert "--endpoint=" in _run(job)
    assert any("upload-artifact" in str(s.get("uses", "")) for s in job["steps"])


def test_an_artifact_export_does_not_post_anywhere(basic_root):
    assert "--endpoint=" not in _run(meter_of(enable(basic_root)))


# --- pins -------------------------------------------------------------------


def test_metering_declares_the_actions_it_needs_pinned(basic_root):
    spec = load_spec(enable(basic_root))
    assert "actions/download-artifact" in spec.external_actions_used()
    assert "actions/upload-artifact" in spec.external_actions_used()


def test_a_pipeline_that_does_not_meter_carries_no_pin_for_them(basic_root):
    assert "actions/download-artifact" not in load_spec(basic_root).external_actions_used()


def test_the_pin_set_is_readable_from_the_manifest_alone(basic_root):
    """`lockstep pin` runs before `fetch`, so it sees a manifest whose agents are not loaded yet.

    A condition that also asked about agents would answer differently in `pin` and in `doctor`, and
    the pin would come out missing in exactly the repository that needed it.
    """
    from lockstep.spec.load import load_manifest_only

    enable(basic_root)
    assert "actions/download-artifact" in load_manifest_only(basic_root).external_actions_used()
