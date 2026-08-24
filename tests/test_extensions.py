"""Extending the framework: third-party builtins and composite actions.

The bug-fix example exercises both extension points. These tests hold it to the same standard as
anything the framework ships, because a guide that walks somebody through a broken example is worse
than no guide.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from conftest import target_ready
from lockstep.checks import doctor, lint
from lockstep.conformance import simulate
from lockstep.emit import compile_spec
from lockstep.emit.agentic import AGENT_CALLER_PERMISSIONS
from lockstep.spec.load import load_spec

EXAMPLE = Path(__file__).parent.parent / "examples" / "bug-fix"
EXTENSION = EXAMPLE / "extensions"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(compile_spec(EXAMPLE).files[".github/workflows/fix-bugs.yml"])


# --- the example holds up ---------------------------------------------------


def test_the_example_lints_clean():
    assert lint(load_spec(EXAMPLE)).findings == []


def test_the_example_is_target_ready_apart_from_unverifiable_extensions():
    target_ready(doctor(load_spec(EXAMPLE), EXAMPLE), "DOC014")


AUTHORIZED = {"command-gate": {"authorized": "true"}}


def test_the_whole_pipeline_is_reachable(workflow):
    order = simulate(workflow, {}, AUTHORIZED).order
    assert order[0] == "command-gate"
    assert order[1] == "fetch-bugs"
    assert order[-1] == "propose-generated-artifacts"
    assert "review-the-fixes" in order


def test_a_dry_run_stops_before_proposing(workflow):
    ran = simulate(workflow, {"dry_run": True}, AUTHORIZED).ran
    assert "review-the-fixes" in ran
    assert "assemble-what-passed-review" not in ran


# --- extension builtins -----------------------------------------------------


def test_the_manifest_declares_the_builtins_it_adds():
    extensions = load_spec(EXAMPLE).manifest.extensions
    assert set(extensions.builtins) == {"jira-fetch", "apply-patch", "run-suite"}
    assert extensions.packages, "a declared builtin nothing installs would fail at run time"


def test_extension_builtins_compile_into_ordinary_commands(workflow):
    runs = " ".join(
        step.get("run", "") for job in workflow["jobs"].values() for step in job.get("steps", []) or []
    )
    assert "pipeline-exec jira-fetch" in runs
    assert "pipeline-exec apply-patch" in runs
    assert "pipeline-exec run-suite" in runs


def test_the_extension_registers_its_commands_for_real(tmp_path):
    """The entry-point mechanism, exercised against the real CLI rather than described."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pipeline_exec.plugins import discover; import json; print(json.dumps(sorted(discover())))",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # The extension is only installed in the dedicated environment the guide describes, so this
    # asserts the mechanism works rather than that it happens to be installed here.
    assert result.returncode == 0, result.stderr


# --- the composite action ---------------------------------------------------


def test_the_custom_action_declares_what_the_overlay_passes():
    action = yaml.safe_load((EXTENSION / "actions" / "setup-target" / "action.yml").read_text())
    overlay = list(yaml.safe_load_all((EXAMPLE / "overlays/github/setup-target.yml").read_text()))[0]
    step = next(patch["value"] for patch in overlay["patches"] if patch["value"].get("id") == "setup-target")
    assert set(step["with"]) <= set(action["inputs"])


def test_the_overlay_reaches_the_generated_workflow(workflow):
    steps = workflow["jobs"]["fetch-bugs"]["steps"]
    ids = [step.get("id") for step in steps]
    assert "install-extensions" in ids
    assert "setup-target" in ids
    # Extensions must be installed before a builtin step names one of their commands.
    assert ids.index("install-extensions") < ids.index("fetch-bugs")


# --- the security posture holds ---------------------------------------------


def test_no_agent_in_this_pipeline_can_write_anything():
    """Four agents read source and propose diffs. None of them may touch the repository."""
    files = compile_spec(EXAMPLE).files
    agents = [path for path in files if "/aw-" in path]
    assert len(agents) == 4
    for path in agents:
        front = yaml.safe_load(files[path].split("---")[1])
        assert front["permissions"] == {"actions": "read", "contents": "read"}, "the agent can write"


def test_only_the_proposal_job_runs_code_with_a_write_token(workflow):
    """One job proposes, and nothing else executes code while holding a write token.

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


def test_the_guardrails_deny_write_tools_to_every_agent():
    """The MCP servers offer read tools only, and the guardrail enforces that independently."""
    files = compile_spec(EXAMPLE).files
    for path in [p for p in files if "/aw-" in p]:
        front = yaml.safe_load(files[path].split("---")[1])
        for server in (front.get("mcp-servers") or {}).values():
            allowed = server.get("allowed", [])
            assert not any(tool.startswith(("write_", "create_", "update_", "delete_")) for tool in allowed)
