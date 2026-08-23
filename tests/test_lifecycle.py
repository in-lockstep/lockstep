"""Pinning and ejection: keeping what runs equal to what was reviewed."""

from __future__ import annotations

import json

import pytest
import yaml

from lockstep.emit import compile_spec
from lockstep.emit.writer import check_plan, write_plan
from lockstep.errors import LockstepError
from lockstep.lifecycle import Ejection, eject, load_pins, pin, stale_ejections, uneject, write_pins
from lockstep.spec.load import load_spec

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


# --- pinning ---------------------------------------------------------------


def test_pinning_records_the_supplied_commit_and_digest(basic_root):
    data, notes = pin(load_spec(basic_root), basic_root, actions_sha=SHA, exec_digest=DIGEST)
    write_pins(basic_root, data)

    stored = load_pins(basic_root)["capabilities"]
    assert stored["actions"]["sha"] == SHA
    assert stored["actions"]["tag"] == "v1.6.2"
    assert stored["exec"]["digest"] == DIGEST
    assert any("supplied" in note for note in notes)


def test_offline_pinning_contacts_nothing(basic_root):
    """`--offline` must not reach the network, so it can run in a sandbox or an air-gapped build."""
    before = load_pins(basic_root)["capabilities"]["actions"]["sha"]
    data, _ = pin(load_spec(basic_root), basic_root, offline=True)
    assert data["capabilities"]["actions"]["sha"] == before


def test_pinning_reports_an_unpinned_image(basic_root):
    pins = load_pins(basic_root)
    del pins["capabilities"]["exec"]["digest"]
    write_pins(basic_root, pins)
    _, notes = pin(load_spec(basic_root), basic_root, offline=True)
    assert any("not pinned" in note for note in notes)


def test_pinned_output_makes_the_compiler_emit_that_commit(basic_root):
    data, _ = pin(load_spec(basic_root), basic_root, actions_sha=SHA, exec_digest=DIGEST)
    write_pins(basic_root, data)
    text = compile_spec(basic_root).files[".github/workflows/discover.yml"]
    assert f"@{SHA}" in text
    assert DIGEST in text


# --- ejection --------------------------------------------------------------


TARGET = ".github/workflows/discover.yml"


def eject_target(root):
    plan = compile_spec(root)
    write_plan(root, plan)
    return eject(root, TARGET, plan.files[TARGET])


def test_ejecting_records_the_file_and_snapshots_its_generation(basic_root):
    base = eject_target(basic_root)
    assert TARGET in Ejection.load(basic_root).files
    assert base.is_file()
    assert base.read_text() == (basic_root / TARGET).read_text()


def test_the_compiler_stops_maintaining_an_ejected_file(basic_root):
    eject_target(basic_root)
    (basic_root / TARGET).write_text("# hand maintained\n", encoding="utf-8")

    write_plan(basic_root, compile_spec(basic_root))
    assert (basic_root / TARGET).read_text() == "# hand maintained\n"
    assert check_plan(basic_root, compile_spec(basic_root)).clean


def test_a_fork_is_reported_once_its_source_moves_on(basic_root):
    """A silent fork becomes indistinguishable from an intentional one; this keeps the debt visible."""
    eject_target(basic_root)
    assert stale_ejections(basic_root, compile_spec(basic_root).files) == []

    command = basic_root / "commands" / "discover.md"
    command.write_text(command.read_text().replace("**Discover UI structure**", "**Map the UI**"))
    assert stale_ejections(basic_root, compile_spec(basic_root).files) == [TARGET]


def test_ejecting_twice_is_refused(basic_root):
    eject_target(basic_root)
    with pytest.raises(LockstepError):
        eject(basic_root, TARGET, "x")


def test_ejecting_something_ungenerated_is_refused(basic_root):
    with pytest.raises(LockstepError):
        eject(basic_root, "README.md", "x")


def test_unejecting_hands_the_file_back(basic_root):
    eject_target(basic_root)
    uneject(basic_root, TARGET)
    assert Ejection.load(basic_root).files == []
    assert not (basic_root / ".pipeline/eject-base" / TARGET).exists()

    (basic_root / TARGET).write_text("# stale\n", encoding="utf-8")
    assert not check_plan(basic_root, compile_spec(basic_root)).clean


def test_unejecting_something_not_ejected_is_refused(basic_root):
    with pytest.raises(LockstepError):
        uneject(basic_root, TARGET)


def test_the_registry_explains_itself(basic_root):
    eject_target(basic_root)
    text = (basic_root / ".pipeline/ejected.yaml").read_text()
    assert text.startswith("#")
    assert yaml.safe_load(text)["files"] == [TARGET]


# --- generated CI ----------------------------------------------------------


def test_the_generated_repo_gates_itself(basic_spec_dir):
    ci = yaml.safe_load(compile_spec(basic_spec_dir).files[".github/workflows/pipeline-ci.yml"])
    assert set(ci["jobs"]) == {"drift", "lint", "doctor", "scripts"}
    drift = " ".join(step.get("run", "") for step in ci["jobs"]["drift"]["steps"])
    assert "--check" in drift
    assert "--fail-on-blocking" in drift


def test_the_gate_installs_the_pinned_compiler_rather_than_the_project(basic_spec_dir):
    """A check must not execute project-defined build hooks in order to run."""
    ci = yaml.safe_load(compile_spec(basic_spec_dir).files[".github/workflows/pipeline-ci.yml"])
    install = " ".join(step.get("run", "") for step in ci["jobs"]["drift"]["steps"])
    assert "uv tool install" in install
    assert "uv sync" not in install


def test_ci_runs_on_spec_changes(basic_spec_dir):
    ci = yaml.safe_load(compile_spec(basic_spec_dir).files[".github/workflows/pipeline-ci.yml"])
    paths = (ci.get("on") or ci.get(True))["pull_request"]["paths"]
    assert "agents/**" in paths
    assert "overlays/**" in paths


def test_ci_is_read_only(basic_spec_dir):
    ci = yaml.safe_load(compile_spec(basic_spec_dir).files[".github/workflows/pipeline-ci.yml"])
    assert ci["permissions"] == {"contents": "read"}
    for job in ci["jobs"].values():
        assert job.get("permissions", {}).get("contents") == "read"
        assert set(job.get("permissions", {})) <= {"contents"}


def test_pins_are_recorded_as_json_for_review(basic_root):
    data, _ = pin(load_spec(basic_root), basic_root, actions_sha=SHA, exec_digest=DIGEST)
    path = write_pins(basic_root, data)
    assert json.loads(path.read_text())["capabilities"]["actions"]["sha"] == SHA
