"""CLI behaviour, especially the exit codes CI depends on."""

from __future__ import annotations

from click.testing import CliRunner

from lockstep.cli import EXIT_DRIFT, EXIT_OK, EXIT_SPEC, main


def run(*args):
    return CliRunner().invoke(main, list(args))


def test_compile_writes_the_workflow_tree(basic_root):
    result = run("compile", "--root", str(basic_root))
    assert result.exit_code == EXIT_OK
    assert (basic_root / ".github/workflows/generate-tests.yml").is_file()
    assert (basic_root / ".github/workflows/aw-story-extractor.md").is_file()
    # Compiling produces the lock files too — an orchestrator naming a file this compile did not
    # write is a workflow GitHub rejects, so the stage is part of compiling rather than after it.
    assert (basic_root / ".github/workflows/aw-story-extractor.lock.yml").is_file()
    assert "gh-aw: 1 lock file(s)" in result.output


def test_compile_reports_the_deterministic_ratio(basic_root):
    result = run("compile", "--root", str(basic_root))
    assert "generate-tests: 4 steps -> 6 jobs · 1 agentic, 2 deterministic" in result.output
    assert "2 steps -> 1 job (fusion saved 1)" in result.output


def test_check_passes_on_freshly_compiled_output(basic_root):
    run("compile", "--root", str(basic_root))
    result = run("compile", "--root", str(basic_root), "--check")
    assert result.exit_code == EXIT_OK
    assert "drift gate: clean" in result.output


def test_check_fails_when_generated_output_was_hand_edited(basic_root):
    run("compile", "--root", str(basic_root))
    target = basic_root / ".github/workflows/discover.yml"
    target.write_text(target.read_text() + "\n# hand edit\n")

    result = run("compile", "--root", str(basic_root), "--check")
    assert result.exit_code == EXIT_DRIFT
    assert "modified" in result.output


def test_check_fails_when_the_spec_moved_without_recompiling(basic_root):
    run("compile", "--root", str(basic_root))
    agent = basic_root / "agents" / "story-extractor.md"
    agent.write_text(agent.read_text().replace("max_tool_turns: 8", "max_tool_turns: 12"))

    assert run("compile", "--root", str(basic_root), "--check").exit_code == EXIT_DRIFT


def test_spec_errors_exit_distinctly_from_drift(basic_root):
    (basic_root / "agents" / "story-extractor.md").unlink()
    result = run("compile", "--root", str(basic_root))
    assert result.exit_code == EXIT_SPEC
    assert "LS101" in result.output


def test_semantic_diff_flags_blocking_deltas(basic_root):
    run("compile", "--root", str(basic_root))
    guardrail = basic_root / "guardrails" / "common.md"
    guardrail.write_text(guardrail.read_text().replace("  permissions: read-all\n", ""))

    result = run("compile", "--root", str(basic_root), "--check", "--semantic-diff")
    assert "[BLOCK] mcp-tools" in result.output
    assert "require explicit acknowledgment" in result.output


def test_show_surface_renders_the_whole_github_surface(basic_root):
    result = run("show-surface", "--root", str(basic_root))
    assert result.exit_code == EXIT_OK
    assert "# GitHub target surface — user-story-validation" in result.output
    assert "in-lockstep/lockstep/actions@actions-v1.6.2" in result.output
    assert "overlays/github/generate-tests.yml" in result.output
    assert "local-only" in result.output


def test_unknown_target_is_rejected(basic_root):
    result = run("compile", "--root", str(basic_root), "--target", "jenkins")
    assert result.exit_code != EXIT_OK
    assert "unknown target" in result.output


def test_notes_surface_deferred_capabilities(basic_root):
    result = run("compile", "--root", str(basic_root))
    assert "note:" in result.output
    assert "Deploy the app locally" in result.output


# --- Phase 5 lifecycle commands --------------------------------------------


def test_lint_fails_on_an_error(basic_root):
    for case in (basic_root / "evals" / "story-extractor" / "cases").glob("*.json"):
        case.unlink()
    result = run("lint", "--root", str(basic_root))
    assert result.exit_code == EXIT_DRIFT
    assert "LNT001" in result.output


def test_lint_strict_promotes_warnings(basic_root):
    cases = basic_root / "evals" / "story-extractor" / "cases"
    cases.mkdir(parents=True, exist_ok=True)
    (cases / "one.json").write_text(
        '{"input": {"key": "one"}, "expect": {"schema": ["summary"]}}', encoding="utf-8"
    )

    assert run("lint", "--root", str(basic_root)).exit_code == EXIT_OK
    assert run("lint", "--root", str(basic_root), "--strict").exit_code == EXIT_DRIFT


def test_doctor_reports_a_pipeline_whose_capabilities_were_never_published(basic_root):
    """A zero pin has the shape of a pin and none of the value; doctor is where that surfaces."""
    result = run("doctor", "--root", str(basic_root))
    assert result.exit_code == EXIT_DRIFT
    assert "DOC015" in result.output
    assert "placeholder" in result.output


def test_doctor_rejects_an_unknown_target(basic_root):
    assert run("doctor", "--root", str(basic_root), "--target=jenkins").exit_code != EXIT_OK


def test_pin_writes_the_lockfile(basic_root):
    result = run("pin", "--root", str(basic_root), "--sha", "c" * 40, "--exec-digest", "sha256:" + "d" * 64)
    assert result.exit_code == EXIT_OK
    assert "pins.lock" in result.output
    assert "c" * 40 in (basic_root / ".pipeline" / "pins.lock").read_text()


def test_eject_then_compile_leaves_the_file_alone(basic_root):
    run("compile", "--root", str(basic_root))
    target = ".github/workflows/discover.yml"
    assert run("eject", "--root", str(basic_root), target).exit_code == EXIT_OK

    (basic_root / target).write_text("# mine now\n", encoding="utf-8")
    run("compile", "--root", str(basic_root))
    assert (basic_root / target).read_text() == "# mine now\n"


def test_ejecting_an_ungenerated_file_is_refused(basic_root):
    result = run("eject", "--root", str(basic_root), "README.md")
    assert result.exit_code == EXIT_SPEC
    assert "not generated" in result.output


def test_uneject_restores_compiler_ownership(basic_root):
    run("compile", "--root", str(basic_root))
    target = ".github/workflows/discover.yml"
    run("eject", "--root", str(basic_root), target)
    assert run("uneject", "--root", str(basic_root), target).exit_code == EXIT_OK

    (basic_root / target).write_text("# no longer mine\n", encoding="utf-8")
    assert run("compile", "--root", str(basic_root), "--check").exit_code == EXIT_DRIFT


def test_the_policy_gate_fails_an_unacknowledged_surface_change(basic_root):
    """A widened tool allow-list must stop a merge, not merely appear in a check summary."""
    run("compile", "--root", str(basic_root))
    guardrail = basic_root / "guardrails" / "common.md"
    guardrail.write_text(guardrail.read_text().replace("  permissions: read-all\n", ""))

    result = run("compile", "--root", str(basic_root), "--check", "--fail-on-blocking")
    assert result.exit_code == EXIT_DRIFT
    assert "[BLOCK]" in result.output


def test_a_clean_recompile_passes_the_policy_gate(basic_root):
    run("compile", "--root", str(basic_root))
    assert run("compile", "--root", str(basic_root), "--check", "--fail-on-blocking").exit_code == EXIT_OK


def test_check_reports_a_stale_ejection(basic_root):
    run("compile", "--root", str(basic_root))
    run("eject", "--root", str(basic_root), ".github/workflows/discover.yml")
    command = basic_root / "commands" / "discover.md"
    command.write_text(command.read_text().replace("**Discover UI structure**", "**Map the UI**"))

    result = run("compile", "--root", str(basic_root), "--check")
    assert "stale eject" in result.output
