"""Pipelines the compiler ships, and the path that starts by running them.

Adopting this framework began with writing a pipeline. That is the wrong first step for a team whose
problem is that they have no AI-SDLC yet, and it is a strange thing to ask of people who are
adopting an opinion in the first place.

So the framework ships pipelines, and ships them the one way that does not trap anybody: they are
**inherited**, not copied. Inheriting is what makes every later step available without giving
anything up — tune an agent inside its band, overlay a step, or write a pipeline of your own that
runs beside them.

The property these protect above the rest is that a repository which authored *nothing* gets a
pipeline that lints clean and compiles.
"""

from __future__ import annotations

import json

import pytest
import yaml

from lockstep import library
from lockstep.checks import Severity, doctor, lint
from lockstep.emit import compile_spec
from lockstep.lifecycle import fetch, pin, write_pins
from lockstep.scaffold import scaffold
from lockstep.spec.load import load_manifest_only, load_spec

SHIPPED = sorted(library.pipelines())


@pytest.fixture
def adopter(tmp_path, basic_root):
    """A repository whose entire content is what `lockstep init --adopt` writes."""
    root = tmp_path / "adopter"
    scaffold(root, "acme-app", "repo", adopt=tuple(SHIPPED))
    # The pins the fixture already resolved; `pin` itself is exercised separately below.
    (root / ".pipeline").mkdir(exist_ok=True)
    (root / ".pipeline" / "pins.lock").write_text(
        (basic_root / ".pipeline" / "pins.lock").read_text(), encoding="utf-8"
    )
    manifest = root / "pipeline.yaml"
    locked = json.loads((root / ".pipeline" / "pins.lock").read_text())
    tag = locked["capabilities"]["actions"]["tag"]
    manifest.write_text(
        manifest.read_text().replace("actions@actions-v1.0.0", f"actions@{tag}"), encoding="utf-8"
    )
    fetch(load_manifest_only(root), root)
    return root


# --- what ships -------------------------------------------------------------


def test_something_ships(tmp_path):
    """The claim is that adopting does not begin with authoring. It needs something to be true."""
    assert SHIPPED, "no pipelines ship, so `--adopt` promises something that does not exist"


def test_every_shipped_pipeline_is_a_pipeline():
    for name, path in library.pipelines().items():
        assert (path / "pipeline.yaml").is_file(), name


def test_no_shipped_pipeline_carries_a_script():
    """A script here would be untested code arriving in every repository that adopts it.

    The repo's own suite cannot reach into the library, so `lockstep lint`'s "scripts need tests"
    rule would be enforced on adopters for code they did not write. Builtins and safe outputs only.
    """
    for name, path in library.pipelines().items():
        assert not list((path / "scripts").glob("*")) if (path / "scripts").is_dir() else True, name


def test_no_shipped_pipeline_claims_capabilities(tmp_path):
    """An inherited pipeline runs under the consumer's capabilities.

    A version pinned here would be a second opinion about which code runs, held by a repository
    that is not the one running it.
    """
    for name, path in library.pipelines().items():
        manifest = yaml.safe_load((path / "pipeline.yaml").read_text()) or {}
        assert "capabilities" not in manifest, name


# --- the zero-authoring path ------------------------------------------------


def test_adopting_writes_a_manifest_and_a_profile_and_nothing_else(tmp_path):
    written = scaffold(tmp_path / "a", "acme", "repo", adopt=tuple(SHIPPED))
    assert sorted(written) == [".gitignore", "README.md", "pipeline.yaml", "profiles/repo.md"]


def test_a_repository_that_authored_nothing_lints_clean(adopter):
    report = lint(load_spec(adopter))
    assert [f.code for f in report.findings if f.severity is Severity.ERROR] == []


def test_a_repository_that_authored_nothing_compiles(adopter):
    files = compile_spec(adopter).files
    assert any(path.endswith("triage-triage.yml") for path in files)
    assert any(path.startswith(".github/workflows/aw-") for path in files)


def test_the_shipped_agent_arrives_with_the_shipped_baseline(adopter):
    """Inherited or not, an agent gets the invariants. The two mechanisms have to compose."""
    files = compile_spec(adopter).files
    agent = next(text for path, text in files.items() if "/aw-" in path and path.endswith(".md"))
    assert "lockstep:guardrails/baseline.md" in agent


def test_adopting_an_unknown_pipeline_names_the_ones_that_exist(tmp_path):
    from lockstep.scaffold import ScaffoldError

    with pytest.raises(ScaffoldError, match="no shipped pipeline named"):
        scaffold(tmp_path / "a", "acme", "repo", adopt=("no-such-pipeline",))


# --- how it is pinned -------------------------------------------------------


def test_a_shipped_pipeline_is_pinned_by_the_compiler_not_a_commit(adopter):
    """There is no second thing to pin: the pipelines travel inside the compiler.

    So unlike a local path this *is* reproducible, and `doctor` must not report it as unpinned.
    """
    data, notes, unresolved = pin(load_manifest_only(adopter), adopter, offline=True)
    assert "inherits" not in data or data["inherits"] == {}
    assert any("shipped with the compiler" in note for note in notes)

    write_pins(adopter, data)
    codes = {f.code for f in doctor(load_spec(adopter), adopter).findings}
    assert "DOC017" not in codes, "reported as an unpinnable local path"
    assert "DOC018" not in codes, "reported as an unpinned upstream"


def test_inheriting_a_pipeline_this_compiler_does_not_ship_is_an_error(adopter):
    manifest = adopter / "pipeline.yaml"
    manifest.write_text(
        manifest.read_text().replace("lockstep:triage", "lockstep:no-such-thing"), encoding="utf-8"
    )
    report = doctor(load_spec(adopter), adopter)
    finding = next(f for f in report.findings if f.code == "DOC023")
    assert "does not ship" in finding.message


def test_fetching_one_that_does_not_ship_says_what_does(adopter):
    from lockstep.errors import LockstepError

    manifest = adopter / "pipeline.yaml"
    manifest.write_text(
        manifest.read_text().replace("lockstep:triage", "lockstep:no-such-thing"), encoding="utf-8"
    )
    with pytest.raises(LockstepError, match="ships with this compiler"):
        fetch(load_manifest_only(adopter), adopter)


# --- the growth path --------------------------------------------------------


def test_a_shipped_agent_can_be_tuned_without_forking_it(adopter):
    """Bands are why `uses:` is enough for the first change somebody wants to make."""
    manifest = adopter / "pipeline.yaml"
    manifest.write_text(
        manifest.read_text().replace(
            "capabilities:",
            "commands:\n  triage:\n    from: triage\n    agents:\n      triage-analyst:\n"
            "        max-ai-credits: 40\n\ncapabilities:",
        ),
        encoding="utf-8",
    )
    files = compile_spec(adopter).files
    agent = next(text for path, text in files.items() if "/aw-" in path and path.endswith(".md"))
    assert "max-ai-credits: 40" in agent


def test_a_pipeline_of_your_own_runs_beside_the_shipped_ones(adopter):
    """Nothing is given up by adding one. That is the whole argument for inheriting rather than copying."""
    (adopter / "commands").mkdir(exist_ok=True)
    (adopter / "commands" / "ours.md").write_text(
        "---\nname: ours\ndescription: Something this team does\n"
        "github:\n  triggers:\n    workflow_dispatch: true\n---\n\n"
        "## Steps\n\n1. **Read the issue** → builtin: issue-fetch\n"
        '   - args: --issue="1" --output={output_dir}/issue.json\n',
        encoding="utf-8",
    )
    files = compile_spec(adopter).files
    assert any(path.endswith("ours.yml") for path in files)
    assert any(path.endswith("triage-triage.yml") for path in files)
