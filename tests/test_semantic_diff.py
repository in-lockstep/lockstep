"""The semantic diff is what a reviewer reads instead of thousands of lines of YAML."""

from __future__ import annotations

from lockstep.emit import compile_spec
from lockstep.emit.semantic_diff import against_disk, diff_surfaces, surfaces
from lockstep.emit.writer import write_plan


def test_no_deltas_when_nothing_changed(basic_root):
    write_plan(basic_root, compile_spec(basic_root))
    assert against_disk(basic_root, compile_spec(basic_root)).deltas == []


def test_widened_mcp_allow_list_is_blocking(basic_root):
    write_plan(basic_root, compile_spec(basic_root))
    guardrail = basic_root / "guardrails" / "common.md"
    guardrail.write_text(guardrail.read_text().replace("  permissions: read-all\n", ""))

    diff = against_disk(basic_root, compile_spec(basic_root))
    categories = {d.category for d in diff.blocking}
    assert "mcp-tools" in categories
    assert "create_issue" in diff.render()


def test_new_egress_host_is_blocking(basic_root):
    write_plan(basic_root, compile_spec(basic_root))
    agent = basic_root / "agents" / "story-extractor.md"
    agent.write_text(agent.read_text().replace("'*.atlassian.net'", "'*.atlassian.net', '*.internal'"))

    diff = against_disk(basic_root, compile_spec(basic_root))
    assert any(d.category == "network" and d.blocking for d in diff.deltas)


def test_new_trigger_is_blocking(basic_root):
    write_plan(basic_root, compile_spec(basic_root))
    command = basic_root / "commands" / "discover.md"
    command.write_text(
        command.read_text().replace(
            "    workflow_dispatch: true", "    workflow_dispatch: true\n    push: {}"
        )
    )
    diff = against_disk(basic_root, compile_spec(basic_root))
    assert any(d.category == "triggers" and d.blocking for d in diff.deltas)


def test_pin_bumps_are_informational_not_blocking(basic_root):
    write_plan(basic_root, compile_spec(basic_root))
    pins = basic_root / ".pipeline" / "pins.lock"
    pins.write_text(pins.read_text().replace("8c44e0d2ab19f3c5d7e6b4a2091f83cc55d1e470", "a" * 40))

    diff = against_disk(basic_root, compile_spec(basic_root))
    assert any(d.category == "uses" for d in diff.deltas)
    assert not diff.blocking


def test_surface_covers_workflows_only(basic_spec_dir):
    """Prompt fragments and generated docs carry no security surface and would only add noise."""
    covered = set(surfaces(compile_spec(basic_spec_dir).files))
    assert not any("/shared/" in path for path in covered)
    assert "SECRETS.md" not in covered
    assert ".github/workflows/aw-story-extractor.md" in covered
    assert ".github/workflows/generate-tests.yml" in covered


def test_added_workflow_is_reported():
    diff = diff_surfaces({}, {"a.yml": {"permissions": None}})
    assert diff.deltas[0].category == "new-file"
