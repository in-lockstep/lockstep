"""Reviewing a pull request, one review per requested aspect.

Each aspect is an agent. That is what makes a lens testable — `evals/security-reviewer/` holds diffs
with planted vulnerabilities — and it is what lets a lens carry its own budget and its own knowledge
of the codebase. Three behaviours decide whether the bot is useful or muted, and all three are here:
it reviews what the comment asked for, only that, and never a pull request that has not moved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from conftest import ready_but_unpublished

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


def test_the_example_is_target_ready_apart_from_being_unpublished():
    ready_but_unpublished(doctor(load_spec(EXAMPLE), EXAMPLE), "DOC014")


ASPECTS = ("security", "intent", "performance", "tests")
REVIEW_JOBS = {
    "security": "review-for-security",
    "intent": "review-for-intent",
    "performance": "review-for-performance",
    "tests": "review-for-test-coverage",
}


def test_the_pipeline_is_reachable_end_to_end(workflow):
    everything = {"diff": {"pending": '["security","intent","performance","tests"]'}}
    order = simulate(workflow, {}, {**AUTHORIZED, **everything}).order
    assert order == [
        GATE,
        "diff",
        *(f"scan-{job}" for job in REVIEW_JOBS.values()),
        *REVIEW_JOBS.values(),
        "post",
    ]


# --- reviewing what the comment asked for, and only that --------------------


def test_the_words_after_the_command_drive_the_work_list(workflow):
    """`/review security intent` asks for two reviews; how many is not known until it is typed."""
    assert "needs.command-gate.outputs.positional" in runs(workflow)


@pytest.mark.parametrize("aspect", ASPECTS)
def test_an_aspect_that_was_not_asked_for_does_not_run(workflow, aspect):
    asked = [a for a in ASPECTS if a != aspect]
    outcome = simulate(workflow, {}, {**AUTHORIZED, "diff": {"pending": json.dumps(asked)}})
    assert REVIEW_JOBS[aspect] not in outcome.order
    for other in asked:
        assert REVIEW_JOBS[other] in outcome.order


def test_the_reviews_run_beside_each_other_not_in_a_queue(workflow):
    """Two aspects asked for is two reviews at once; queueing them doubles the wait for nothing."""
    for job in REVIEW_JOBS.values():
        # Each review is preceded by its own scan of its own input, so they still fan out from the
        # diff rather than queueing behind one another.
        assert workflow["jobs"][job]["needs"] == [GATE, "diff", f"scan-{job}"]
        assert workflow["jobs"][f"scan-{job}"]["needs"] == [GATE, "diff"]
    assert workflow["jobs"]["post"]["needs"] == [GATE, *REVIEW_JOBS.values()]


def test_each_review_writes_its_own_file(workflow):
    """One review per aspect, and the file name is what the posting step names it by."""
    for aspect, job in REVIEW_JOBS.items():
        assert workflow["jobs"][job]["with"]["output_path"] == f"outputs/reviews/{aspect}.json"


def test_an_aspect_is_an_agent(workflow):
    """Which is what gives it evals, its own budget, and its own knowledge of this codebase."""
    spec = load_spec(EXAMPLE)
    assert {f"{a}-reviewer" for a in ASPECTS} <= set(spec.agents)
    assert not (EXAMPLE / "aspects").exists()


@pytest.mark.parametrize("aspect", ASPECTS)
def test_every_lens_has_evals(aspect):
    """LNT001 requires them, and a lens nobody tested reports plausible nonsense confidently."""
    cases = list((EXAMPLE / "evals" / f"{aspect}-reviewer" / "cases").glob("*.json"))
    assert cases
    for case in cases:
        # The lens is the agent under test, so a case that restated it would be testing a copy.
        assert "brief" not in case.read_text()


def test_a_lens_may_cost_what_it_is_worth():
    """One shared agent forced one budget on four jobs of different value."""
    spec = load_spec(EXAMPLE)
    credits = {name: agent.github.max_ai_credits for name, agent in spec.agents.items()}
    assert credits["security-reviewer"] > credits["tests-reviewer"]


# --- not re-reviewing what has not changed ----------------------------------


def test_the_pipeline_asks_what_still_needs_reviewing(workflow):
    """A second review saying the same thing buries the human conversation."""
    assert "pipeline-exec review-state" in runs(workflow)


def test_nothing_reviews_a_pull_request_that_has_not_moved(workflow):
    """An empty pending list gates every reviewer off; only the posting step runs, and says so."""
    outcome = simulate(workflow, {}, {**AUTHORIZED, "diff": {"pending": "[]"}})
    assert not [job for job in outcome.order if job.startswith("review-for-")]


def test_the_state_check_decides_before_any_agent_starts(workflow):
    ids = [step.get("id") for step in workflow["jobs"]["diff"]["steps"]]
    assert "state" in ids
    assert workflow["jobs"]["diff"]["outputs"]["pending"] == "${{ steps.state.outputs.pending }}"


@pytest.mark.parametrize("aspect", ASPECTS)
def test_every_reviewer_is_told_how_to_revise(aspect):
    """Shared method, so it is one skill rather than four copies drifting apart."""
    files = compile_spec(EXAMPLE).files
    front = yaml.safe_load(files[f".github/workflows/aw-{aspect}-reviewer.md"].split("---")[1])
    assert "shared/skill-review-revision.md" in front["imports"]
    shared = files[".github/workflows/shared/skill-review-revision.md"]
    assert "You are revising that review, not writing a new one" in " ".join(shared.split())


@pytest.mark.parametrize("aspect", ASPECTS)
def test_the_guardrail_forbids_repeating_an_addressed_finding(aspect):
    body = " ".join(
        compile_spec(EXAMPLE).files[f".github/workflows/aw-{aspect}-reviewer.md"].split("---", 2)[2].split()
    )
    assert "MUST NOT repeat a finding the author has already addressed" in body
    assert "MUST NOT drop a previous finding without saying so" in body


# --- the security posture ---------------------------------------------------


def test_an_unauthorized_comment_runs_nothing(workflow):
    assert simulate(workflow, {}, {GATE: {"authorized": "false"}}).order == [GATE]


@pytest.mark.parametrize("aspect", ASPECTS)
def test_no_reviewer_can_write(aspect):
    front = yaml.safe_load(
        compile_spec(EXAMPLE).files[f".github/workflows/aw-{aspect}-reviewer.md"].split("---")[1]
    )
    assert front["permissions"] == "read-all"


def test_only_the_posting_job_may_write(workflow):
    writers = {
        name: job["permissions"]
        for name, job in workflow["jobs"].items()
        if "write" in str(job.get("permissions", ""))
    }
    assert set(writers) == {"post"}
    assert writers["post"] == {"contents": "read", "pull-requests": "write"}


def test_an_outside_contributor_cannot_spend_the_projects_budget_by_default():
    """`read` permission on a public repository means "anyone", so it is not the trust signal."""
    command = load_spec(EXAMPLE).commands["review"].github.command
    assert command.name == "/review"
    assert "read" not in command.roles
    assert command.associations == ["OWNER", "MEMBER", "COLLABORATOR"]
