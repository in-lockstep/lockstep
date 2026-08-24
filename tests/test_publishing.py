"""Publishing a report to the branch GitHub Pages serves."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from conftest import target_ready
from lockstep.checks import doctor, lint
from lockstep.conformance import simulate
from lockstep.emit import compile_spec
from lockstep.emit.agentic import AGENT_CALLER_PERMISSIONS
from lockstep.spec.load import load_spec

EXAMPLE = Path(__file__).parent.parent / "examples" / "triage-report"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(compile_spec(EXAMPLE).files[".github/workflows/triage.yml"])


def propose_step(workflow):
    return next(
        step
        for step in workflow["jobs"]["propose-generated-artifacts"]["steps"]
        if step.get("id") == "propose"
    )


# --- the example holds up ---------------------------------------------------


def test_the_example_lints_clean():
    assert lint(load_spec(EXAMPLE)).findings == []


def test_the_example_is_target_ready():
    target_ready(doctor(load_spec(EXAMPLE), EXAMPLE), "DOC014")


def test_the_pipeline_is_reachable_end_to_end(workflow):
    assert simulate(workflow).order == [
        "search",
        # Every agent is preceded by a scan of what it is about to read.
        "scan-write-the-triage-report",
        "write-the-triage-report",
        "render",
        "propose-generated-artifacts",
    ]


# --- publishing to a different branch ---------------------------------------


def test_the_report_targets_the_pages_branch(workflow):
    """A published site's contents have nothing to do with the branch that generated them."""
    assert propose_step(workflow)["with"]["base"] == "gh-pages"


def test_the_site_is_what_gets_proposed(workflow):
    with_block = propose_step(workflow)["with"]
    assert with_block["source"] == "outputs/site"
    assert with_block["destination"] == "."


def test_parameters_in_the_proposal_are_expanded(workflow):
    """A literal `{title}` is not noticed until somebody is looking at a pull request called that."""
    title = propose_step(workflow)["with"]["title"]
    assert title == "${{ inputs.title }}"
    assert "{title}" not in title.replace("${{ inputs.title }}", "")


def test_a_proposal_without_a_base_still_targets_the_current_branch():
    """The default has to keep working; not every pipeline publishes somewhere else."""
    other = Path(__file__).parent.parent / "examples" / "bug-fix"
    workflow = yaml.safe_load(compile_spec(other).files[".github/workflows/fix-bugs.yml"])
    assert "base" not in propose_step(workflow)["with"]


def test_publishing_holds_the_only_write_permission_that_runs_anything(workflow):
    """One job publishes, and nothing else executes code while holding a write token.

    Agent-calling jobs hold `issues: write` as well, and they are a different thing. A job with
    `uses:` and no `steps:` runs no code, so it cannot spend a permission — it can only hand it to
    the workflow it calls, whose own agent job is `read-all` and is asserted separately. gh-aw's
    generated `conclusion` and `safe_outputs` jobs require it, and without it GitHub refuses the
    whole workflow at startup.
    """
    executing = [
        name
        for name, job in workflow["jobs"].items()
        if "write" in str(job.get("permissions", "")) and "steps" in job
    ]
    assert executing == ["propose-generated-artifacts"]

    for name, job in workflow["jobs"].items():
        if "uses" in job and "write" in str(job.get("permissions", "")):
            assert job["permissions"] == AGENT_CALLER_PERMISSIONS, name


# --- the prompt layers ------------------------------------------------------


def test_all_four_prompt_layers_reach_the_agent():
    files = compile_spec(EXAMPLE).files
    agent = files[".github/workflows/aw-triage-reporter.md"]
    front = yaml.safe_load(agent.split("---")[1])
    body = agent.split("---", 2)[2]

    assert front["imports"] == [
        "shared/skill-report-writing.md",
        "shared/context-tracker.md",
    ]
    assert "You MUST NOT invent an issue key" in body
    assert "This report is published to a public page" in body
    assert "You are given counts of a triage backlog" in body


def test_guardrails_precede_the_agents_own_instructions():
    """A constraint that might land after what it constrains is not a constraint."""
    body = compile_spec(EXAMPLE).files[".github/workflows/aw-triage-reporter.md"].split("---", 2)[2]
    assert body.index("You MUST NOT invent an issue key") < body.index("You are given counts")


def test_every_layer_is_written_out_for_audit():
    files = compile_spec(EXAMPLE).files
    for shared in (
        "shared/guardrail-common.md",
        "shared/guardrail-reporting.md",
        "shared/skill-report-writing.md",
        "shared/context-tracker.md",
    ):
        assert f".github/workflows/{shared}" in files


def test_the_reporting_agent_needs_no_tools():
    """It is given the counts and the issues; a tool call would only add latency and risk."""
    front = yaml.safe_load(
        compile_spec(EXAMPLE).files[".github/workflows/aw-triage-reporter.md"].split("---")[1]
    )
    assert front["max-turns"] == 0
    assert "mcp-servers" not in front
    assert front["permissions"] == {"actions": "read", "contents": "read"}, "the agent can write"
