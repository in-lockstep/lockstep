"""Chat ops: running a pipeline from a comment, safely.

A comment trigger runs with the repository's token, and anyone who can comment can fire one. Most of
these tests are therefore about what does *not* happen.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lockstep.checks import doctor, lint
from lockstep.conformance import simulate
from lockstep.emit import compile_spec
from lockstep.spec.load import load_spec

EXAMPLE = Path(__file__).parent.parent / "examples" / "implement-issue"
GATE = "command-gate"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(compile_spec(EXAMPLE).files[".github/workflows/implement.yml"])


def triggers(workflow):
    return workflow.get("on") or workflow.get(True)


# --- the example holds up ---------------------------------------------------


def test_the_example_lints_clean():
    assert lint(load_spec(EXAMPLE)).findings == []


def test_the_example_is_target_ready():
    report = doctor(load_spec(EXAMPLE), EXAMPLE)
    assert report.ok
    assert {finding.code for finding in report.findings} == {"DOC014"}


# --- the triggers -----------------------------------------------------------


def test_the_declared_comment_events_become_triggers(workflow):
    assert "issue_comment" in triggers(workflow)
    assert "pull_request_review_comment" in triggers(workflow)


def test_only_created_comments_fire_the_pipeline(workflow):
    """An edited comment re-firing would let somebody rewrite what an authorized run was asked to do."""
    assert triggers(workflow)["issue_comment"] == {"types": ["created"]}
    assert triggers(workflow)["pull_request_review_comment"] == {"types": ["created"]}


def test_the_pipeline_is_still_reachable_by_dispatch(workflow):
    assert "workflow_dispatch" in triggers(workflow)


# --- the gate ---------------------------------------------------------------


def test_the_gate_runs_first_and_reads_only(workflow):
    gate = workflow["jobs"][GATE]
    assert gate.get("needs") is None
    assert gate["permissions"] == {"contents": "read"}


def test_the_gate_publishes_what_the_rest_of_the_pipeline_needs(workflow):
    outputs = workflow["jobs"][GATE]["outputs"]
    assert set(outputs) >= {"authorized", "issue", "branch", "instruction", "pull_request"}


def test_the_gate_is_told_which_roles_may_invoke(workflow):
    step = next(s for s in workflow["jobs"][GATE]["steps"] if s.get("id") == "gate")
    assert step["with"]["command"] == "/implement"
    assert step["with"]["roles"] == "admin,maintain,write"


def test_every_job_checks_authorization_explicitly(workflow):
    """Skip propagation is not enough: the tolerant condition that lets conditional steps work would
    otherwise let an unauthorized comment run everything downstream of a skipped gate."""
    for name, job in workflow["jobs"].items():
        if name == GATE:
            continue
        assert f"needs.{GATE}.outputs.authorized == 'true'" in job.get("if", ""), name
        needs = job["needs"]
        assert GATE in ([needs] if isinstance(needs, str) else needs), name


def test_an_unauthorized_comment_runs_nothing_but_the_gate(workflow):
    ran = simulate(workflow, {}, {GATE: {"authorized": "false"}}).order
    assert ran == [GATE]


def test_the_job_holding_write_permissions_is_gated_too(workflow):
    """It is emitted after the pipeline's own steps, so it is the one most easily left out."""
    propose = workflow["jobs"]["propose-generated-artifacts"]
    assert "write" in str(propose["permissions"])
    assert f"needs.{GATE}.outputs.authorized == 'true'" in propose["if"]

    ran = simulate(workflow, {}, {GATE: {"authorized": "false"}}).ran
    assert "propose-generated-artifacts" not in ran


def test_an_authorized_comment_runs_the_whole_pipeline(workflow):
    ran = simulate(workflow, {}, {GATE: {"authorized": "true"}}).ran
    assert {"fetch-issue", "plan-the-change", "write-the-change", "propose-generated-artifacts"} <= ran


# --- arguments reach the steps ----------------------------------------------


def test_an_argument_resolves_from_either_trigger(workflow):
    """Dispatched, the value is an input; invoked from a comment, it comes from the gate."""
    runs = " ".join(
        step.get("run", "") for job in workflow["jobs"].values() for step in job.get("steps", []) or []
    )
    assert "inputs.issue || needs.command-gate.outputs.issue" in runs


def test_the_reviewers_own_words_reach_the_pipeline(workflow):
    """The instruction is usually the point of the run; feedback is what the loop is built on."""
    runs = " ".join(
        step.get("run", "") for job in workflow["jobs"].values() for step in job.get("steps", []) or []
    )
    assert "needs.command-gate.outputs.pull_request" in runs


# --- the security posture ---------------------------------------------------


def test_no_agent_in_this_pipeline_can_write():
    files = compile_spec(EXAMPLE).files
    agents = [path for path in files if "/aw-" in path]
    assert len(agents) == 4
    for path in agents:
        front = yaml.safe_load(files[path].split("---")[1])
        assert front["permissions"] == "read-all"


def test_only_the_proposal_job_may_write(workflow):
    writers = [name for name, job in workflow["jobs"].items() if "write" in str(job.get("permissions", ""))]
    assert writers == ["propose-generated-artifacts"]


def test_the_overlay_adds_the_post_pull_request_work(workflow):
    steps = workflow["jobs"]["propose-generated-artifacts"]["steps"]
    ids = [step.get("id") for step in steps]
    assert ids.index("propose") < ids.index("ci") < ids.index("plan-comment")


# --- a gated pipeline that also runs on a schedule ---------------------------

BUG_FIX = Path(__file__).parent.parent / "examples" / "bug-fix"


@pytest.fixture(scope="module")
def bug_fix():
    return yaml.safe_load(compile_spec(BUG_FIX).files[".github/workflows/fix-bugs.yml"])


def test_a_gated_pipeline_can_still_run_on_a_schedule(bug_fix):
    """The gate must not be reachable only from a comment, or the schedule silently stops working."""
    events = triggers(bug_fix)
    assert {"schedule", "issue_comment", "workflow_dispatch"} <= set(events)


def test_the_gate_keys_on_the_payload_not_a_list_of_event_names():
    """Adding a trigger must not be able to disable the pipeline by omission."""
    action = (Path(__file__).parent.parent / "actions" / "command-gate" / "action.yml").read_text()
    assert 'has("comment")' in action
    assert 'GITHUB_EVENT_NAME}" = "workflow_dispatch"' not in action


def test_an_unauthorized_comment_runs_nothing_on_the_bug_fix_pipeline(bug_fix):
    assert simulate(bug_fix, {}, {GATE: {"authorized": "false"}}).order == [GATE]


def test_review_feedback_reaches_the_agents_that_can_act_on_it(bug_fix):
    runs = " ".join(
        step.get("run", "") for job in bug_fix["jobs"].values() for step in job.get("steps", []) or []
    )
    assert "pipeline-exec pr-feedback" in runs
    assert "needs.command-gate.outputs.pull_request" in runs


def test_a_review_rerun_can_narrow_to_the_bugs_that_were_objected_to(bug_fix):
    """Re-doing every bug in the query because a reviewer disliked one of them is waste."""
    runs = " ".join(
        step.get("run", "") for job in bug_fix["jobs"].values() for step in job.get("steps", []) or []
    )
    assert "--only=" in runs


def test_the_agents_that_respond_to_review_carry_the_guardrail_for_it():
    files = compile_spec(BUG_FIX).files
    assert ".github/workflows/shared/guardrail-review-response.md" in files
    for agent in ("aw-bug-analyst.md", "aw-fix-writer.md", "aw-fix-reviewer.md"):
        body = files[f".github/workflows/{agent}"].split("---", 2)[2]
        assert "takes precedence over your own earlier reasoning" in body


def test_pr_feedback_is_a_framework_builtin_now_not_an_extension():
    """It began as an extension; a second pipeline needing it is what moved it into the framework."""
    from lockstep.emit.builtins import AVAILABLE
    from lockstep.spec.load import load_spec

    assert "pr-feedback" in AVAILABLE
    for example in (BUG_FIX, Path(__file__).parent.parent / "examples" / "implement-issue"):
        assert "pr-feedback" not in load_spec(example).manifest.extensions.builtins
