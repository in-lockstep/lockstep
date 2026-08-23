"""Reviewing a pull request, one review per requested aspect.

Two behaviours decide whether a review bot is useful or muted, and both are tested here: it fans out
over what the comment asked for, and it does not review a pull request that has not moved.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lockstep.checks import doctor, lint
from lockstep.conformance import simulate
from lockstep.emit import compile_spec
from lockstep.spec.load import load_spec

EXAMPLE = Path(__file__).parent.parent / "examples" / "pr-review"
GATE = "command-gate"
AUTHORIZED = {GATE: {"authorized": "true"}}


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(compile_spec(EXAMPLE).files[".github/workflows/review.yml"])


def runs(workflow):
    return " ".join(
        step.get("run", "") for job in workflow["jobs"].values() for step in job.get("steps", []) or []
    )


# --- the example holds up ---------------------------------------------------


def test_the_example_lints_clean():
    assert lint(load_spec(EXAMPLE)).findings == []


def test_the_example_is_target_ready():
    report = doctor(load_spec(EXAMPLE), EXAMPLE)
    assert report.ok
    assert {finding.code for finding in report.findings} == {"DOC014"}


def test_the_pipeline_is_reachable_end_to_end(workflow):
    assert simulate(workflow, {}, AUTHORIZED).order == [
        GATE,
        "select",
        "review-one-aspect",
        "verify-review-one-aspect",
        "post",
    ]


# --- fanning out over what the comment asked for ----------------------------


def test_the_words_after_the_command_drive_the_work_list(workflow):
    """`/review security intent` asks for two reviews; how many is not known until it is typed."""
    assert "needs.command-gate.outputs.positional" in runs(workflow)


def test_each_aspect_becomes_its_own_matrix_leg(workflow):
    review = workflow["jobs"]["review-one-aspect"]
    assert review["strategy"]["matrix"]["item"].startswith("${{ fromJSON(")
    assert review["strategy"]["max-parallel"] == 4
    assert review["strategy"]["fail-fast"] is False


def test_each_leg_writes_its_own_review_file(workflow):
    """One review per aspect, and the file name is what the posting step names it by."""
    review = workflow["jobs"]["review-one-aspect"]
    assert review["with"]["output_path"] == "outputs/reviews/${{ matrix.item.key }}.json"


def test_an_aspect_is_a_file_not_a_code_change():
    """Adding a review lens should be adding a markdown file."""
    aspects = {path.stem for path in (EXAMPLE / "aspects").glob("*.md")}
    assert aspects == {"security", "intent", "tests", "performance"}


# --- not re-reviewing what has not changed ----------------------------------


def test_the_pipeline_asks_what_still_needs_reviewing(workflow):
    """A second review saying the same thing buries the human conversation."""
    assert "pipeline-exec review-state" in runs(workflow)


def test_the_state_check_runs_before_any_agent(workflow):
    """An empty work list means an empty matrix, and the agent never starts."""
    select = workflow["jobs"]["select"]
    ids = [step.get("id") for step in select["steps"]]
    assert ids.index("state") < len(ids)
    assert workflow["jobs"]["review-one-aspect"]["needs"] == [GATE, "select"]


def test_the_reviewing_agent_is_told_how_to_revise():
    body = " ".join(
        compile_spec(EXAMPLE).files[".github/workflows/aw-pr-reviewer.md"].split("---", 2)[2].split()
    )
    assert "You are revising that review, not writing a new one" in body
    assert "fixed, still standing, or no longer relevant" in body


def test_the_guardrail_forbids_repeating_an_addressed_finding():
    body = " ".join(
        compile_spec(EXAMPLE).files[".github/workflows/aw-pr-reviewer.md"].split("---", 2)[2].split()
    )
    assert "MUST NOT repeat a finding the author has already addressed" in body
    assert "MUST NOT drop a previous finding without saying so" in body


# --- the security posture ---------------------------------------------------


def test_an_unauthorized_comment_runs_nothing(workflow):
    assert simulate(workflow, {}, {GATE: {"authorized": "false"}}).order == [GATE]


def test_the_reviewing_agent_cannot_write():
    front = yaml.safe_load(compile_spec(EXAMPLE).files[".github/workflows/aw-pr-reviewer.md"].split("---")[1])
    assert front["permissions"] == "read-all"
    assert front["max-turns"] == 0


def test_only_the_posting_job_may_write(workflow):
    writers = {
        name: job["permissions"]
        for name, job in workflow["jobs"].items()
        if "write" in str(job.get("permissions", ""))
    }
    assert set(writers) == {"post"}
    assert writers["post"] == {"contents": "read", "pull-requests": "write"}


def test_readers_may_ask_for_a_review():
    """Asking a bot to look at a pull request is not a privileged action."""
    command = load_spec(EXAMPLE).commands["review"].github.command
    assert "read" in command.roles
    assert command.name == "/review"
