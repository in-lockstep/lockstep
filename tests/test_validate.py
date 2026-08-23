"""Structural invariants of emitted workflows."""

from __future__ import annotations

import pytest

from lockstep.emit import compile_spec
from lockstep.emit.validate import validate_workflow
from lockstep.errors import EmitError


def test_the_fixture_compiles_to_structurally_valid_workflows(basic_spec_dir):
    compile_spec(basic_spec_dir)  # validation runs inside compile


def test_reusable_workflow_job_may_not_carry_runner_keys():
    workflow = {"jobs": {"a": {"uses": "./x.yml", "runs-on": "ubuntu-latest"}}}
    with pytest.raises(EmitError) as excinfo:
        validate_workflow("w.yml", workflow)
    assert "runs-on" in excinfo.value.render()


def test_dangling_needs_is_rejected():
    with pytest.raises(EmitError):
        validate_workflow("w.yml", {"jobs": {"a": {"needs": "ghost", "steps": []}}})


def test_matrix_reading_an_undeclared_output_is_rejected():
    """An undeclared output resolves to empty, which would silently produce zero matrix legs."""
    workflow = {
        "jobs": {
            "producer": {"steps": []},
            "consumer": {
                "needs": "producer",
                "strategy": {"matrix": {"item": "${{ fromJSON(needs.producer.outputs.items) }}"}},
                "uses": "./x.yml",
            },
        }
    }
    with pytest.raises(EmitError) as excinfo:
        validate_workflow("w.yml", workflow)
    assert "does not declare" in excinfo.value.render()


def test_declared_output_passes():
    workflow = {
        "jobs": {
            "producer": {"outputs": {"items": "x"}, "steps": []},
            "consumer": {
                "needs": "producer",
                "uses": "./x.yml",
                "strategy": {"matrix": {"item": "${{ fromJSON(needs.producer.outputs.items) }}"}},
            },
        }
    }
    validate_workflow("w.yml", workflow)


def test_job_timeout_above_the_platform_limit_is_rejected(basic_root):
    overlay = basic_root / "overlays" / "github" / "generate-tests.yml"
    overlay.write_text(overlay.read_text().replace("timeout-minutes: 30", "timeout-minutes: 600"))
    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    assert "may not exceed 360" in excinfo.value.render()


def test_too_many_dispatch_inputs_is_rejected(basic_root):
    command = basic_root / "commands" / "discover.md"
    extra = "".join(f"  - name: p{i}\n    default: ''\n" for i in range(12))
    command.write_text(
        command.read_text().replace(
            "    description: App profile to target\n", "    description: App profile to target\n" + extra
        )
    )
    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    assert "GitHub allows 10" in excinfo.value.render()
