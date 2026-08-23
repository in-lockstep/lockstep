"""lint and doctor: is the spec well built, and will the target accept it."""

from __future__ import annotations

import json

import pytest

from lockstep.checks import Severity, doctor, lint
from lockstep.spec.load import load_spec


def codes(report):
    return {finding.code for finding in report.findings}


def lint_of(root):
    return lint(load_spec(root))


def doctor_of(root):
    return doctor(load_spec(root), root)


def give_evals(root, *agents):
    for name in agents:
        cases = root / "evals" / name / "cases"
        cases.mkdir(parents=True, exist_ok=True)
        (cases / "one.json").write_text("{}", encoding="utf-8")


# --- lint ------------------------------------------------------------------


def test_an_agent_without_evals_is_an_error(basic_root):
    report = lint_of(basic_root)
    assert "LNT001" in codes(report)
    assert any(f.severity is Severity.ERROR for f in report.findings if f.code == "LNT001")


def test_evals_satisfy_the_agent_check(basic_root):
    give_evals(basic_root, "story-extractor")
    assert "LNT001" not in codes(lint_of(basic_root))


def test_a_script_without_tests_is_a_warning(basic_root):
    report = lint_of(basic_root)
    assert "LNT002" in codes(report)
    assert all(f.severity is Severity.WARNING for f in report.findings if f.code == "LNT002")


def test_a_tested_script_is_not_reported(basic_root):
    (basic_root / "tests").mkdir(exist_ok=True)
    for stem in (
        "discover_api",
        "discover_ui",
        "fetch_issues",
        "save_manifest",
        "deploy_local",
        "repair_script",
    ):
        (basic_root / "tests" / f"test_{stem}.py").write_text("", encoding="utf-8")
    assert "LNT002" not in codes(lint_of(basic_root))


def test_an_agent_doing_deterministic_work_is_flagged(basic_root):
    """AI decides what to do; scripts do it."""
    agent = basic_root / "agents" / "story-extractor.md"
    agent.write_text(
        agent.read_text().replace("max_tool_turns: 8", "max_tool_turns: 0")
        + "\n\nSort the stories alphabetically and deduplicate them.\n"
    )
    assert "LNT003" in codes(lint_of(basic_root))


def test_a_serial_foreach_is_flagged(basic_root):
    command = basic_root / "commands" / "generate-tests.md"
    command.write_text(command.read_text().replace("   - parallel: 3\n", ""))
    assert "LNT004" in codes(lint_of(basic_root))


def test_a_clean_spec_lints_clean(basic_root):
    give_evals(basic_root, "story-extractor")
    (basic_root / "tests").mkdir(exist_ok=True)
    for stem in (
        "discover_api",
        "discover_ui",
        "fetch_issues",
        "save_manifest",
        "deploy_local",
        "repair_script",
    ):
        (basic_root / "tests" / f"test_{stem}.py").write_text("", encoding="utf-8")
    assert lint_of(basic_root).findings == []


# --- doctor ----------------------------------------------------------------


def test_the_fixture_is_target_ready(basic_spec_dir):
    assert doctor(load_spec(basic_spec_dir), basic_spec_dir).ok


def test_unpinned_capabilities_are_errors(basic_root):
    (basic_root / ".pipeline" / "pins.lock").unlink()
    assert {"DOC001", "DOC002"} <= codes(doctor_of(basic_root))


def test_a_moved_tag_would_not_be_caught_by_pins_alone(basic_root):
    """Pinning only helps if the pin is a commit; a tag is a mutable pointer."""
    pins = json.loads((basic_root / ".pipeline" / "pins.lock").read_text())
    del pins["capabilities"]["actions"]["sha"]
    (basic_root / ".pipeline" / "pins.lock").write_text(json.dumps(pins))
    assert "DOC001" in codes(doctor_of(basic_root))


def test_an_unmappable_provider_is_an_error(basic_root):
    agent = basic_root / "agents" / "story-extractor.md"
    agent.write_text(agent.read_text().replace("provider: vertex-claude", "provider: ollama"))
    assert "DOC004" in codes(doctor_of(basic_root))


def test_an_unknown_provider_is_an_error(basic_root):
    agent = basic_root / "agents" / "story-extractor.md"
    agent.write_text(agent.read_text().replace("provider: vertex-claude", "provider: telepathy"))
    assert "DOC005" in codes(doctor_of(basic_root))


def test_an_agent_without_a_budget_is_an_error(basic_root):
    agent = basic_root / "agents" / "story-extractor.md"
    agent.write_text(agent.read_text().replace("  max-ai-credits: 40\n", ""))
    assert "DOC006" in codes(doctor_of(basic_root))


def test_a_missing_per_run_budget_is_a_warning(basic_root):
    manifest = basic_root / "pipeline.yaml"
    manifest.write_text(manifest.read_text().replace("budgets:\n  per_run_ai_credits: 400\n", ""))
    assert "DOC007" in codes(doctor_of(basic_root))


def test_an_undeclared_credential_reference_is_an_error(basic_root):
    profile = basic_root / "profiles" / "my-app.md"
    profile.write_text(profile.read_text().replace("auth_method=jwt", "token=${MYSTERY_TOKEN}"))
    assert "DOC008" in codes(doctor_of(basic_root))


def test_secrets_without_an_environment_are_a_warning(basic_root):
    profile = basic_root / "profiles" / "my-app.md"
    profile.write_text(profile.read_text().replace("  environment: my-app-staging\n", ""))
    assert "DOC009" in codes(doctor_of(basic_root))


def test_an_mcp_server_without_a_tool_list_is_an_error(basic_root):
    servers = basic_root / "mcp" / "servers.json"
    data = json.loads(servers.read_text())
    data["servers"]["jira"]["tools"] = []
    servers.write_text(json.dumps(data))
    assert "DOC010" in codes(doctor_of(basic_root))


def test_an_over_limit_timeout_is_an_error(basic_root):
    command = basic_root / "commands" / "discover.md"
    command.write_text(command.read_text().replace("github:", "github:\n  timeout-minutes: 600"))
    assert "DOC011" in codes(doctor_of(basic_root))


@pytest.mark.parametrize("code", ["LNT001", "DOC006"])
def test_findings_render_with_a_hint(basic_root, code):
    agent = basic_root / "agents" / "story-extractor.md"
    agent.write_text(agent.read_text().replace("  max-ai-credits: 40\n", ""))
    report = lint_of(basic_root) if code.startswith("LNT") else doctor_of(basic_root)
    finding = next(f for f in report.findings if f.code == code)
    assert finding.hint
    assert code in finding.render()


# --- the worked example ----------------------------------------------------


def test_the_example_pipeline_lints_and_doctors_clean():
    """The guide walks a reader through this; it has to hold up to the same checks as anything else."""
    from pathlib import Path

    example = Path(__file__).parent.parent / "examples" / "httpbin"
    assert lint(load_spec(example)).findings == []
    assert doctor(load_spec(example), example).ok
