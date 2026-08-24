"""The seam between this compiler and the one that produces what actually runs.

`lockstep compile` emits an agent as markdown; `gh aw compile` turns it into the `.lock.yml` a
runner executes. Everything the drift gate proved stopped one layer above that file — a reviewer
approved a turn limit in a document GitHub never reads.

These tests need the real `gh aw`, deliberately. Mocking it would assert what we assumed about a
foreign tool, which is the thing that went unverified in the first place.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest
import yaml
from click.testing import CliRunner

from lockstep import ghaw
from lockstep.cli import main
from lockstep.emit import compile_spec

pytestmark = pytest.mark.skipif(
    shutil.which("gh") is None
    or subprocess.run(["gh", "aw", "version"], capture_output=True).returncode != 0,
    reason="gh-aw is not installed; the seam cannot be verified without it",
)

AGENT = "aw-story-extractor"


def run(*args):
    return CliRunner().invoke(main, list(args))


def workflows(root):
    return root / ".github/workflows"


def lock(root, name=AGENT):
    return workflows(root) / f"{name}{ghaw.LOCK_SUFFIX}"


@pytest.fixture
def compiled(basic_root):
    result = run("compile", "--root", str(basic_root))
    assert result.exit_code == 0, result.output
    return basic_root


# --- the lock file is produced, not left to a later step --------------------


def test_compiling_produces_the_file_the_orchestrator_names(compiled):
    """An orchestrator naming a file the compile did not write is a workflow GitHub rejects."""
    referenced = [
        job["uses"]
        for job in yaml.safe_load(
            (workflows(compiled) / "generate-tests.yml").read_text()
        )["jobs"].values()
        if "uses" in job
    ]
    assert any(ref.endswith(f"{AGENT}{ghaw.LOCK_SUFFIX}") for ref in referenced), referenced
    assert lock(compiled).is_file()


def test_the_check_passes_on_what_compile_just_wrote(compiled):
    result = run("compile", "--check", "--root", str(compiled))
    assert result.exit_code == 0, result.output
    assert "lock file(s) match" in result.output


# --- the gate now covers it -------------------------------------------------


def test_an_edited_lock_file_fails_the_gate(compiled):
    """The whole point: hand-editing what runs is now a build failure, not a silent divergence."""
    target = lock(compiled)
    target.write_text(target.read_text() + "\n# hand-edited\n", encoding="utf-8")
    result = run("compile", "--check", "--root", str(compiled))
    assert result.exit_code != 0
    assert "modified:" in result.output


def test_a_deleted_lock_file_fails_the_gate(compiled):
    lock(compiled).unlink()
    result = run("compile", "--check", "--root", str(compiled))
    assert result.exit_code != 0
    assert "missing:" in result.output


def test_a_lock_file_for_an_agent_that_no_longer_exists_is_pruned(compiled):
    orphan = workflows(compiled) / f"aw-departed{ghaw.LOCK_SUFFIX}"
    orphan.write_text("# left behind\n", encoding="utf-8")
    result = run("compile", "--root", str(compiled))
    assert result.exit_code == 0, result.output
    assert not orphan.exists()


# --- what the constraints actually become -----------------------------------


def test_the_turn_limit_reaches_the_command_that_runs_the_model(compiled):
    """`max-turns` is substrate, not prose: it becomes an argument to the agent CLI."""
    text = lock(compiled).read_text()
    front = yaml.safe_load(compile_spec(compiled).files[f".github/workflows/{AGENT}.md"].split("---")[1])
    assert f"GH_AW_MAX_TURNS: {front['max-turns']}" in text
    assert f"--max-turns {front['max-turns']}" in text


def test_the_credit_budget_reaches_the_proxy_that_enforces_it(compiled):
    front = yaml.safe_load(compile_spec(compiled).files[f".github/workflows/{AGENT}.md"].split("---")[1])
    assert f'GH_AW_MAX_AI_CREDITS: "{front["max-ai-credits"]}"' in lock(compiled).read_text()


def test_the_engine_credential_is_the_one_this_compiler_documents(compiled):
    """SECRETS.md names it from a mapping; the lock file's own manifest is the check on that."""
    from lockstep.emit.agentic import ENGINE_SECRET

    assert ENGINE_SECRET["claude"] in lock(compiled).read_text()


def test_the_deprecated_engine_model_form_is_not_emitted(compiled):
    """gh-aw warns on `engine.model`; a deprecation nobody sees becomes a breakage at an upgrade."""
    front = yaml.safe_load(compile_spec(compiled).files[f".github/workflows/{AGENT}.md"].split("---")[1])
    assert "model" not in front["engine"]
    assert front["model"]


# --- the tool itself --------------------------------------------------------


def test_compiling_the_same_markdown_twice_gives_the_same_bytes(basic_root):
    """Byte-comparison in CI is only a gate if the generator is deterministic."""
    run("compile", "--root", str(basic_root))
    first = ghaw.compile_locks(workflows(basic_root))
    second = ghaw.compile_locks(workflows(basic_root))
    assert first == second
    assert first, "expected at least one lock file"


def test_a_version_other_than_the_pinned_one_is_refused(basic_root):
    """Comparing against output from a different compiler answers a question nobody asked."""
    with pytest.raises(ghaw.GhAwError) as error:
        ghaw.require("v0.0.1", cwd=basic_root)
    assert "v0.0.1" in error.value.message
    assert "different version is a different file" in error.value.hint


def test_the_installed_version_is_accepted(basic_root):
    assert ghaw.require(ghaw.version(), cwd=basic_root).startswith("v")


def test_a_directory_with_no_agents_produces_nothing(tmp_path):
    empty = tmp_path / "workflows"
    empty.mkdir()
    (empty / "ci.yml").write_text("name: CI\n", encoding="utf-8")
    assert ghaw.compile_locks(empty) == {}
