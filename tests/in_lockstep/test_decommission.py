"""GATE-CI-1 and friends: the compiler is gone, and nothing still reaches for it."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_gate_ci_1_no_workflow_invokes_the_compiler() -> None:
    """A drift gate whose compiler was deleted is a job that fails forever."""
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = workflow.read_text()
        for command in ("lockstep compile", "lockstep fetch", "lockstep pin", "lockstep eject"):
            assert command not in text, f"{workflow.name} still invokes `{command}`"


def test_gate_ci_1_no_generated_workflows_remain() -> None:
    workflows = ROOT / ".github" / "workflows"
    assert list(workflows.glob("aw-*.lock.yml")) == []
    assert list(workflows.glob("aw-*.md")) == []
    assert not (workflows / "shared").exists(), "flattened prompt layers were compiler output"
    assert not (workflows / "pipeline-ci.yml").exists()


def test_the_compiler_package_is_gone() -> None:
    assert not (ROOT / "src" / "lockstep").exists()
    assert not (ROOT / ".lockstep").exists(), "the spec tree went with its compiler"
    assert not (ROOT / "actions").exists()


def test_only_the_framework_console_script_survives() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    scripts = data["project"]["scripts"]
    assert "in-lockstep" in scripts
    assert "lockstep" not in scripts, "the compiler's entry point went with the compiler"


def test_the_wheel_ships_only_the_framework() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    packages = data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert packages == ["src/in_lockstep"]


def test_package_data_is_force_included() -> None:
    """Prompts and cases are not .py files, so a packaging change can silently drop them."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    include = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert "src/in_lockstep/prompts" in include
    assert "src/in_lockstep/evals" in include


def test_the_repository_configures_itself_in_python() -> None:
    """It self-hosts on the new thing, which is the point of the exercise."""
    module = ROOT / "lockstep.py"
    assert module.exists()
    text = module.read_text()
    assert "from in_lockstep import Lockstep" in text
    assert "lockstep.bind(" in text


def test_the_trampoline_carries_no_lifecycle_logic() -> None:
    """It invokes the CLI. Which workflows exist, and what they do, live in Python."""
    text = (ROOT / ".github" / "workflows" / "lockstep.yml").read_text()
    assert "in-lockstep review" in text
    # A timeout, because without one the CI default is 360 minutes rather than the 20 the
    # compiler used to emit.
    assert "timeout-minutes:" in text
    for leaked in ("guardrail", "max_turns", "deny_tools", "strategy"):
        assert leaked not in text, f"{leaked!r} is lifecycle logic and belongs in lockstep.py"


def test_nothing_imports_the_deleted_package() -> None:
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text()
        assert "from lockstep." not in text, f"{path} imports the compiler"
        assert "import lockstep\n" not in text, f"{path} imports the compiler"


def test_the_characterization_corpus_outlived_the_emitter() -> None:
    """The one artifact that had to survive: the composition order it recorded."""
    corpus = ROOT / "tests" / "characterization" / "corpus.json"
    assert corpus.exists()
    assert (ROOT / "tests" / "characterization" / "corpus-shipped.json").exists()
