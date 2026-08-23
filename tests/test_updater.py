"""An inherited pipeline that notices its upstream moved, and proposes the recompile.

The failure modes worth preventing are the quiet ones: a bump that opens a fourth pull request beside
three stale ones, a recompile that commits itself instead of asking, and — the one that matters most —
a run that decides which commit to fetch from data somebody sent it.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest
import yaml

from lockstep.checks import doctor
from lockstep.emit import compile_spec
from lockstep.lifecycle import fetch
from lockstep.spec.load import load_manifest_only, load_spec

FIXTURES = Path(__file__).parent / "fixtures"
STANDARDS = FIXTURES / "upstream-standards"


@pytest.fixture
def consumer(tmp_path):
    for name in ("upstream-standards", "upstream-review", "consumer"):
        shutil.copytree(FIXTURES / name, tmp_path / name)
    root = tmp_path / "consumer"
    fetch(load_manifest_only(root), root)
    return root


@pytest.fixture
def workflow(consumer):
    return yaml.safe_load(compile_spec(consumer).files[".github/workflows/update.yml"])


def repin():
    spec = importlib.util.spec_from_file_location("repin", STANDARDS / "scripts" / "repin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- a consumer inherits the updater, it does not write one -----------------


def test_the_updater_is_inherited_like_any_other_pipeline(consumer):
    """Upstream writes it once; nobody downstream authors a self-updating pipeline."""
    assert "update" in load_spec(consumer).commands
    # The repository has commands of its own; none of them is this one.
    assert not (consumer / "commands" / "update.md").exists()
    assert not any("upstream" in path.read_text() for path in (consumer / "commands").glob("*.md"))


def test_it_polls_and_accepts_a_dispatch(workflow):
    triggers = workflow.get("on") or workflow.get(True)
    assert "schedule" in triggers
    assert triggers["repository_dispatch"]["types"] == ["upstream-moved"]


# --- the trust rule ---------------------------------------------------------


def test_no_step_reads_the_dispatch_payload(workflow):
    """A payload that could name a ref could point a consumer at arbitrary code."""
    emitted = yaml.dump(workflow)
    assert "client_payload" not in emitted
    assert "github.event.client_payload" not in emitted


def test_the_script_says_why_it_ignores_the_payload():
    source = (STANDARDS / "scripts" / "repin.py").read_text()
    assert "payload" in source
    assert "repositories it already\ntrusts" in source or "already trusts" in source


# --- deciding whether anything moved ----------------------------------------


def test_an_unresolved_upstream_is_not_mistaken_for_a_new_version():
    """An empty commit means `pin` could not reach it — a failure, not something to propose."""
    assert repin().moved({"standards": "aaa"}, {"standards": ""}) == []


def test_a_retagged_ref_is_noticed_because_commits_are_compared():
    assert repin().moved({"standards": "aaa"}, {"standards": "ccc"}) == ["standards"]


def test_nothing_moved_proposes_nothing():
    assert repin().moved({"a": "1"}, {"a": "1"}) == []


# --- what it emits ----------------------------------------------------------


def test_the_repin_job_runs_outside_the_executor_container(workflow):
    jobs = workflow["jobs"]
    compiling = [name for name, job in jobs.items() if "container" not in job]
    assert compiling, "the job that re-pins needs the compiler, which the image does not carry"
    for name in compiling:
        runs = [step.get("run", "") for step in jobs[name]["steps"]]
        assert any("uv tool install" in run for run in runs)


def test_the_recompile_reaches_a_pull_request_and_not_a_branch(workflow):
    propose = next(
        step
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "propose-pr" in str(step.get("uses", ""))
    )
    assert propose["with"]["reuse-branch"] == "true"
    assert propose["with"]["branch"] == "pipeline/upstream-bump"


def test_only_the_proposing_job_may_write(workflow):
    writers = {
        name: job["permissions"]
        for name, job in workflow["jobs"].items()
        if "write" in str(job.get("permissions", ""))
    }
    assert list(writers) == ["propose-generated-artifacts"]
    assert writers["propose-generated-artifacts"] == {"contents": "write", "pull-requests": "write"}


def test_three_bumps_leave_one_pull_request():
    """`reuse-branch` force-pushes one branch and edits the open pull request in place."""
    action = (Path(__file__).parent.parent / "actions" / "propose-pr" / "action.yml").read_text()
    assert "gh pr edit" in action
    assert "git push -qf origin" in action
    assert 'gh pr list --head "$branch" --state open' in action


def test_the_recompile_commits_nothing_itself():
    script = (STANDARDS / "scripts" / "recompile.sh").read_text()
    assert "git commit" not in script
    assert "git push" not in script


def test_the_pins_travel_with_the_proposal():
    """A reviewer who cannot see which commit moved cannot review the bump."""
    assert "pins.lock" in (STANDARDS / "scripts" / "recompile.sh").read_text()


# --- the checks -------------------------------------------------------------


def test_a_runtime_compiler_is_surfaced_for_a_human(consumer):
    codes = {f.code for f in doctor(load_spec(consumer), consumer).findings}
    assert "DOC020" in codes


def test_recompiling_without_proposing_is_refused(consumer):
    command = consumer / ".pipeline/inherited/standards/commands/update.md"
    text = command.read_text()
    start = text.index("  propose:")
    end = text.index("---", start)
    command.write_text(text[:start] + text[end:], encoding="utf-8")
    report = doctor(load_spec(consumer), consumer)
    assert "DOC021" in {f.code for f in report.findings}
    assert not report.ok


def test_the_manifest_records_what_it_is_pinned_to(consumer):
    manifest = json.loads(compile_spec(consumer).files[".pipeline/compile-manifest.json"])
    assert set(manifest["inherits"]) == {"standards", "review"}
