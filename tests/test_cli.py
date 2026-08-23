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
    assert "gh aw compile" in result.output


def test_compile_reports_the_deterministic_ratio(basic_root):
    result = run("compile", "--root", str(basic_root))
    assert "generate-tests: 4 steps -> 5 jobs · 1 agentic, 2 deterministic" in result.output
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
    assert "pipeline-fw/pipeline-actions@v1.6.2" in result.output
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
