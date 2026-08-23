"""Adoption into a repository that already exists.

A pipeline added to somebody else's project lives in `.lockstep/` and takes exactly one directory
at the root. Everything the compiler reads has to follow it there, and everything it writes has to
address the definitions from the repository root, because that is where the runner will be
standing. The failure mode these tests exist for is the silent one: an input the compiler quietly
stops reading, which looks like a working pipeline that ignores your overlay.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from lockstep.emit import compile_spec
from lockstep.emit.writer import write_plan
from lockstep.lifecycle import eject
from lockstep.spec.load import load_spec

FIXTURES = Path(__file__).parent / "fixtures"

# What the repository looked like before anyone thought about pipelines: a Makefile, some source,
# its own tests, and a CI workflow that runs `make ci`.
EXISTING = {
    "Makefile": "ci: lint test\n\nlint:\n\truff check src\n\ntest:\n\tpytest tests -q\n",
    "src/app.py": "def greet(name: str) -> str:\n    return f'hello {name}'\n",
    "tests/test_app.py": "from app import greet\n\n\ndef test_greet():\n    assert greet('x') == 'hello x'\n",
    ".github/workflows/ci.yml": (
        "name: ci\non: [push, pull_request]\njobs:\n"
        "  make:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make ci\n"
    ),
}


@pytest.fixture
def adopted_root(tmp_path: Path) -> Path:
    """An existing project that has adopted the basic pipeline into `.lockstep/`."""
    root = tmp_path / "existing"
    for relative, content in EXISTING.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    shutil.copytree(FIXTURES / "basic", root / ".lockstep")
    return root


def test_the_pipeline_is_found_in_the_directory_it_adopted_into(adopted_root):
    spec = load_spec(adopted_root)
    assert spec.in_lockstep_dir
    assert spec.home == adopted_root / ".lockstep"
    assert spec.repo_path("scripts/fetch-issues.py") == ".lockstep/scripts/fetch-issues.py"


def test_scripts_are_invoked_from_the_repository_root(adopted_root):
    text = compile_spec(adopted_root).files[".github/workflows/generate-tests.yml"]
    assert ".lockstep/scripts/fetch-issues.py" in text
    # Not the bare path: the runner checks out the repository, not the definitions directory.
    assert " scripts/fetch-issues.py" not in text


def test_overlays_still_reach_the_generated_workflow(adopted_root):
    """The silent-failure regression: overlays are spec input and must follow the spec."""
    text = compile_spec(adopted_root).files[".github/workflows/generate-tests.yml"]
    jobs = yaml.safe_load(text)["jobs"]
    assert jobs["fetch-issues"]["timeout-minutes"] == 30
    assert "# overlays: .lockstep/overlays/github/generate-tests.yml@" in text


def test_prompt_overlays_resolve_fragments_from_the_definitions(adopted_root):
    text = compile_spec(adopted_root).files[".github/workflows/aw-story-extractor.md"]
    assert "## Acme conventions" in text


def test_generated_ci_runs_the_pipelines_tests_not_the_projects(adopted_root):
    ci = yaml.safe_load(compile_spec(adopted_root).files[".github/workflows/pipeline-ci.yml"])
    run = ci["jobs"]["scripts"]["steps"][-1]["run"]
    assert ".lockstep/tests" in run
    # `pytest tests` here would run the *project's* suite, in an environment set up for neither.
    assert "pytest tests " not in run
    paths = (ci.get("on") or ci.get(True))["pull_request"]["paths"]
    assert ".lockstep/scripts/**" in paths


def test_adoption_costs_the_repository_one_directory(adopted_root):
    before = {path.name for path in adopted_root.iterdir()}
    write_plan(adopted_root, compile_spec(adopted_root))
    assert {path.name for path in adopted_root.iterdir()} == before
    assert before == {"Makefile", "src", "tests", ".github", ".lockstep"}


def test_the_ci_that_was_already_there_is_left_alone(adopted_root):
    existing = adopted_root / ".github/workflows/ci.yml"
    write_plan(adopted_root, compile_spec(adopted_root))
    assert existing.read_text() == EXISTING[".github/workflows/ci.yml"]


def test_compiler_state_lives_with_the_definitions(adopted_root):
    plan = compile_spec(adopted_root)
    write_plan(adopted_root, plan)
    target = ".github/workflows/generate-tests.yml"
    eject(adopted_root, target, plan.files[target])

    assert (adopted_root / ".lockstep/.pipeline/ejected.yaml").is_file()
    assert (adopted_root / ".lockstep/.pipeline/eject-base" / target).is_file()
    assert (adopted_root / ".lockstep/.pipeline/compile-manifest.json").is_file()
    # A second `.pipeline/` at the root would be exactly the pollution `.lockstep/` prevents.
    assert not (adopted_root / ".pipeline").exists()


def test_an_ejected_file_survives_a_recompile(adopted_root):
    plan = compile_spec(adopted_root)
    write_plan(adopted_root, plan)
    target = ".github/workflows/generate-tests.yml"
    eject(adopted_root, target, plan.files[target])

    mine = "# mine now\n"
    (adopted_root / target).write_text(mine, encoding="utf-8")
    write_plan(adopted_root, compile_spec(adopted_root))
    assert (adopted_root / target).read_text() == mine


def test_a_repository_that_is_the_pipeline_is_unaffected(basic_root):
    spec = load_spec(basic_root)
    assert not spec.in_lockstep_dir
    assert spec.repo_path("scripts/fetch-issues.py") == "scripts/fetch-issues.py"


def test_watched_paths_are_written_from_the_repository_root(adopted_root):
    """They name things outside the pipeline, so the `.lockstep/` prefix would be wrong."""
    manifest = adopted_root / ".lockstep" / "pipeline.yaml"
    manifest.write_text(
        manifest.read_text().replace(
            "    out: .github/workflows", "    out: .github/workflows\n    watch: [src/**]"
        ),
        encoding="utf-8",
    )
    ci = yaml.safe_load(compile_spec(adopted_root).files[".github/workflows/pipeline-ci.yml"])
    paths = (ci.get("on") or ci.get(True))["pull_request"]["paths"]
    assert "src/**" in paths
    assert ".lockstep/src/**" not in paths


def test_only_generated_files_are_marked_generated(adopted_root):
    """The output directory is shared with workflows the repository already had.

    `*.yml linguist-generated` collapsed those in every pull request diff — including the CI that
    is the one gate a compiler change cannot rewrite.
    """
    (adopted_root / ".github/workflows").mkdir(parents=True, exist_ok=True)
    (adopted_root / ".github/workflows/ci.yml").write_text("name: CI\n", encoding="utf-8")
    files = compile_spec(adopted_root).files
    attributes = files[".github/workflows/.gitattributes"]

    assert "*.yml linguist-generated" not in attributes
    marked = {line.split(" ", 1)[0] for line in attributes.splitlines() if "linguist-generated" in line}
    generated = {p.removeprefix(".github/workflows/") for p in files if p.startswith(".github/workflows/")}
    # gh-aw writes the lock files after this compile, so they stay a pattern.
    assert marked - {"*.lock.yml"} == generated - {".gitattributes"}
    assert "ci.yml" not in marked
