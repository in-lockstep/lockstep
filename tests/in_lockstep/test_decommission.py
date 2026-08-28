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


def test_package_data_travels_inside_the_package() -> None:
    """Prompts and cases are not .py files, so a packaging change can silently drop them.

    They survive by living inside `src/in_lockstep/`, which the wheel target copies whole. A
    `force-include` mapping them a second time is not belt-and-braces: hatchling refuses a wheel
    that adds two files at one archive path, so the redundant table made the distribution
    unbuildable — silently, because nothing in `make ci` builds a wheel. `release-python.yml` is
    where the data is proven to arrive, by reading it back out of site-packages.
    """
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    wheel = data["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert "force-include" not in wheel, "duplicates what `packages` already carries"

    root = ROOT / "src" / "in_lockstep"
    assert list((root / "prompts").rglob("*.md")), "prompt bodies are the package data"
    assert list((root / "evals").rglob("*.json")), "eval cases are the package data"


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


def test_the_executor_runtime_is_gone() -> None:
    """`pipeline-exec` was the compiler's runtime and outlived it by one release.

    It reached 1.0 with nothing in the framework importing it, and was then deleted by decision
    (ADR 0001, the amendment). Half a deletion is the failure worth guarding: a stale `testpaths`
    entry or workspace member turns every later `uv sync` into a resolution error, and a lint or
    mypy stanza naming a path that no longer exists is dead configuration that reads as live.
    """
    assert not (ROOT / "packages").exists()

    text = (ROOT / "pyproject.toml").read_text()
    # Comments are stripped first: the manifest still *explains* the deleted distribution, which is
    # the record. What must be gone is configuration that would act on it.
    settings = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for trace in ("pipeline_exec", "pipeline-exec", "in-lockstep-exec"):
        assert trace not in settings, f"pyproject still configures {trace!r}"

    data = tomllib.loads(text)
    assert "workspace" not in data.get("tool", {}).get("uv", {}), "a workspace with no members left"
    assert data["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]
    assert data["tool"]["pytest"]["ini_options"]["pythonpath"] == ["src"]


def test_the_two_version_declarations_agree() -> None:
    """`pyproject.toml` names the distribution's version; `__version__` is what `--version` prints.

    They are separate declarations, and they drifted: the manifest said 0.1.0 while the package
    said 0.2.0.dev0. The release workflow's tag check reads only the manifest, so tagging would
    have published a wheel whose own `--version` disagreed with the name it was published under —
    and PyPI never lets a version be reused, so that is not a mistake a follow-up release undoes.
    """
    import in_lockstep

    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert in_lockstep.__version__ == manifest


def test_the_release_publishes_one_distribution() -> None:
    """Two Trusted Publishing exchanges existed because two projects did. One does now."""
    text = (ROOT / ".github" / "workflows" / "release-python.yml").read_text()
    assert "uv build --package in-lockstep-exec" not in text
    assert "matrix" not in text, "a one-element matrix is a fan-out over nothing"
