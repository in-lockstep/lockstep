"""lint and doctor: is the spec well built, and will the target accept it."""

from __future__ import annotations

import json

import pytest

from conftest import ready_but_unpublished, target_ready
from lockstep.checks import Severity, doctor, lint
from lockstep.errors import SpecError
from lockstep.spec.load import load_spec


def codes(report):
    return {finding.code for finding in report.findings}


def lint_of(root):
    return lint(load_spec(root))


def doctor_of(root):
    return doctor(load_spec(root), root)


def drop_evals(root, agent="story-extractor"):
    """Remove the fixture's cases, for tests about an agent that has none."""
    for case in (root / "evals" / agent / "cases").glob("*.json"):
        case.unlink()


def give_evals(root, *agents):
    for name in agents:
        cases = root / "evals" / name / "cases"
        cases.mkdir(parents=True, exist_ok=True)
        (cases / "one.json").write_text(
            '{"input": {"key": "one"}, "expect": {"schema": ["summary"]}}', encoding="utf-8"
        )


# --- lint ------------------------------------------------------------------


def test_an_agent_without_evals_is_an_error(basic_root):
    # The fixture ships cases, because a canonical fixture that fails its own lint is a strange
    # thing to hold everything else to. This test creates the condition it is about.
    drop_evals(basic_root)
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


def test_the_fixture_is_target_ready_apart_from_being_unpublished(basic_spec_dir):
    ready_but_unpublished(doctor(load_spec(basic_spec_dir), basic_spec_dir))


def test_unpinned_capabilities_are_errors(basic_root):
    (basic_root / ".pipeline" / "pins.lock").unlink()
    assert {"DOC001", "DOC002"} <= codes(doctor_of(basic_root))


def test_a_capability_the_output_never_names_is_not_demanded(basic_root):
    """A pin is a promise about an artifact a workflow references.

    Asked for one the output does not name, doctor is a red gate with nothing behind it — which is
    the state a pipeline is in when its work is all compiler steps, or when it has no steps at all.
    """
    for command in (basic_root / "commands").glob("*.md"):
        command.unlink()
    for agent in (basic_root / "agents").glob("*.md"):
        agent.unlink()
    (basic_root / ".pipeline" / "pins.lock").unlink()
    assert not {"DOC001", "DOC002", "DOC003", "DOC016"} & codes(doctor_of(basic_root))


def test_a_capability_the_output_does_name_still_is(basic_root):
    """The same fixture with its commands intact: the demand is about use, not about being lenient."""
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


def test_a_turn_cap_below_the_credit_budget_is_a_warning(basic_root):
    """The backstop must not quietly become the budget.

    `max-ai-credits` is bandable and `max_tool_turns` deliberately is not, so an agent that runs out
    of turns first is being bounded by the one number a consumer cannot move. Run 32792379720 was
    that: the planner stopped at 6 turns with 34 of its 70 credits unspent, and no plan written.
    """
    agent = basic_root / "agents" / "story-extractor.md"
    # 8 turns buys about 40 credits, which is exactly what the fixture allows. One more and the
    # turns run out first.
    agent.write_text(agent.read_text().replace("  max-ai-credits: 40\n", "  max-ai-credits: 60\n"))
    assert "DOC026" in codes(doctor_of(basic_root))


def test_a_turn_cap_is_measured_against_the_top_of_the_credit_band(basic_root):
    """A band whose upper half the turn cap swallows is decoration, so the ceiling is what counts."""
    agent = basic_root / "agents" / "story-extractor.md"
    agent.write_text(
        agent.read_text().replace(
            "  max-ai-credits: 40\n",
            "  max-ai-credits: { default: 40, min: 20, max: 300 }\n",
        )
    )
    assert "DOC026" in codes(doctor_of(basic_root))


def test_a_turn_cap_above_the_whole_band_is_fine(basic_root):
    agent = basic_root / "agents" / "story-extractor.md"
    agent.write_text(
        agent.read_text()
        .replace("max_tool_turns: 8", "max_tool_turns: 60")
        .replace("  max-ai-credits: 40\n", "  max-ai-credits: { default: 40, min: 20, max: 300 }\n")
    )
    assert "DOC026" not in codes(doctor_of(basic_root))


def test_a_text_only_agent_has_no_turns_to_cap(basic_root):
    """`max_tool_turns: 0` means no tools at all, which is a choice rather than a tight backstop."""
    agent = basic_root / "agents" / "story-extractor.md"
    agent.write_text(
        agent.read_text()
        .replace("max_tool_turns: 8", "max_tool_turns: 0")
        .replace("  max-ai-credits: 40\n", "  max-ai-credits: 300\n")
    )
    assert "DOC026" not in codes(doctor_of(basic_root))


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
    drop_evals(basic_root)
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
    target_ready(doctor(load_spec(example), example))


# --- extensions ------------------------------------------------------------


def declare_extension(root, *, package=True):
    manifest = root / "pipeline.yaml"
    block = "\nextensions:\n  builtins: [jira-fetch]\n"
    if package:
        block += "  packages: [my-pipeline-extensions==1.0.0]\n"
    manifest.write_text(manifest.read_text() + block)


def test_an_extension_builtin_without_a_package_is_an_error(basic_root):
    """Nothing would install it, and the workflow would fail with `No such command`."""
    declare_extension(basic_root, package=False)
    assert "DOC013" in codes(doctor_of(basic_root))


def test_a_declared_extension_is_flagged_as_unverifiable(basic_root):
    declare_extension(basic_root)
    report = doctor_of(basic_root)
    assert "DOC013" not in codes(report)
    finding = next(f for f in report.findings if f.code == "DOC014")
    assert finding.severity is Severity.WARNING
    assert "list-commands" in finding.hint


# --- ceilings on `enforce:` -------------------------------------------------


def test_a_ceiling_that_is_not_a_number_is_refused(basic_root):
    """Silently coercing it would produce a limit nobody can predict from the file."""
    guardrail = basic_root / "guardrails" / "common.md"
    guardrail.write_text(
        guardrail.read_text().replace("---\n\n", "enforce:\n  max-turns: lots\n---\n\n", 1),
        encoding="utf-8",
    )
    with pytest.raises(SpecError) as error:
        load_spec(basic_root)
    assert "not a number" in error.value.message


def test_a_ceiling_of_zero_is_refused(basic_root):
    """Zero forbids rather than limits, and reads like an omission."""
    guardrail = basic_root / "guardrails" / "common.md"
    guardrail.write_text(
        guardrail.read_text().replace("---\n\n", "enforce:\n  max-ai-credits: 0\n---\n\n", 1),
        encoding="utf-8",
    )
    with pytest.raises(SpecError) as error:
        load_spec(basic_root)
    assert "forbids rather than limits" in error.value.message


def test_no_ceiling_anywhere_constrains_nothing(basic_spec_dir):
    """The fixture sets none, so every agent compiles on its own declared numbers."""
    spec = load_spec(basic_spec_dir)
    assert all(fragment.enforce.max_turns is None for fragment in spec.guardrails.values())
    assert all(fragment.enforce.max_ai_credits is None for fragment in spec.guardrails.values())


# --- the shape of an eval case ----------------------------------------------
#
# `lockstep lint` used to check that a file existed. These are about the thing inside it: a case
# that asserts nothing, a rubric a judge cannot apply consistently, and a fixture that is not there.


CASES = "evals/story-extractor/cases"


def write_case(root, payload, name="one.json"):
    cases = root / CASES
    cases.mkdir(parents=True, exist_ok=True)
    (cases / name).write_text(json.dumps(payload), encoding="utf-8")
    return cases


def messages(report, code):
    return " ".join(f.message + " " + (f.hint or "") for f in report.findings if f.code == code)


def test_a_case_that_is_not_json_is_an_error(basic_root):
    (basic_root / CASES / "one.json").write_text("{not json", encoding="utf-8")
    assert "LNT007" in codes(lint_of(basic_root))


def test_a_case_with_no_input_is_an_error(basic_root):
    write_case(basic_root, {"expect": {"schema": ["summary"]}})
    assert "LNT007" in codes(lint_of(basic_root))


def test_a_case_that_asserts_nothing_is_an_error(basic_root):
    write_case(basic_root, {"input": {}, "expect": {}})
    assert "LNT008" in codes(lint_of(basic_root))


def test_an_unknown_expectation_is_an_error_rather_than_a_stricter_case(basic_root):
    write_case(basic_root, {"input": {}, "expect": {"contians": ["x"]}})
    assert "contians" in messages(lint_of(basic_root), "LNT008")


def test_a_scored_rubric_needs_levels_that_say_what_earns_them(basic_root):
    """A judge told to score out of 5 with nothing else invents the scale on every call."""
    write_case(
        basic_root,
        {"input": {}, "expect": {"rubric": {"criteria": "Finds it", "levels": {"5": "good"}, "min": 5}}},
    )
    assert "at least two scores" in messages(lint_of(basic_root), "LNT008")


def test_a_scored_rubric_needs_a_threshold(basic_root):
    write_case(
        basic_root,
        {"input": {}, "expect": {"rubric": {"criteria": "F", "levels": {"5": "a", "1": "b"}}}},
    )
    assert "needs `min`" in messages(lint_of(basic_root), "LNT008")


def test_a_threshold_outside_the_scale_is_an_error(basic_root):
    write_case(
        basic_root,
        {"input": {}, "expect": {"rubric": {"criteria": "F", "levels": {"5": "a", "1": "b"}, "min": 7}}},
    )
    assert "outside the scale" in messages(lint_of(basic_root), "LNT008")


def test_an_empty_rubric_is_an_error(basic_root):
    write_case(basic_root, {"input": {}, "expect": {"rubric": "   "}})
    assert "empty" in messages(lint_of(basic_root), "LNT008")


def test_a_well_formed_scored_rubric_passes(basic_root):
    write_case(
        basic_root,
        {
            "input": {},
            "expect": {
                "rubric": {
                    "criteria": "Says what an attacker does with it.",
                    "levels": {"5": "Names the exploit", "3": "Notices the input", "1": "Misses it"},
                    "min": 4,
                }
            },
        },
    )
    assert "LNT008" not in codes(lint_of(basic_root))


def test_a_case_naming_a_fixture_that_is_not_there_is_an_error(basic_root):
    write_case(basic_root, {"input": {}, "fixture": "traversal", "expect": {"contains": ["x"]}})
    assert "LNT009" in codes(lint_of(basic_root))


def test_a_fixture_cannot_be_a_path_out_of_the_suite(basic_root):
    write_case(basic_root, {"input": {}, "fixture": "../../..", "expect": {"contains": ["x"]}})
    assert "directory name" in messages(lint_of(basic_root), "LNT009")


def test_an_empty_fixture_directory_is_an_error(basic_root):
    write_case(basic_root, {"input": {}, "fixture": "traversal", "expect": {"contains": ["x"]}})
    (basic_root / "evals/story-extractor/fixtures/traversal").mkdir(parents=True)
    assert "no files" in messages(lint_of(basic_root), "LNT009")


def test_a_case_cannot_set_the_key_the_fixture_path_goes_in(basic_root):
    write_case(
        basic_root,
        {"input": {"repo": "/elsewhere"}, "fixture": "traversal", "expect": {"contains": ["x"]}},
    )
    fixture = basic_root / "evals/story-extractor/fixtures/traversal"
    fixture.mkdir(parents=True)
    (fixture / "main.py").write_text("x = 1\n", encoding="utf-8")
    assert "input.repo" in messages(lint_of(basic_root), "LNT009")


def test_a_fixture_that_is_there_passes(basic_root):
    write_case(basic_root, {"input": {}, "fixture": "traversal", "expect": {"contains": ["x"]}})
    fixture = basic_root / "evals/story-extractor/fixtures/traversal"
    (fixture / "src").mkdir(parents=True)
    (fixture / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    assert "LNT009" not in codes(lint_of(basic_root))
