"""This repository compiles its own drift gate.

Every other pipeline installs a released compiler, which makes its output a function of its spec
alone. Here the compiler is the repository, so the output is a function of `src/` too — and the
gate has to be built for that or it passes on exactly the change worth checking.

The rest of these are the properties that let the gate run at all: it names nothing that has not
been published, and it does not take ownership of the hand-written workflow beside it.
"""

from __future__ import annotations

import yaml

from lockstep.checks import doctor, lint
from lockstep.emit.plan import compile_spec
from lockstep.emit.writer import check_plan
from lockstep.spec.load import load_spec

CI = ".github/workflows/pipeline-ci.yml"


def gate(repo_root):
    return yaml.safe_load(compile_spec(repo_root).files[CI])


def triggers(repo_root):
    # `on:` is YAML 1.1's boolean true once parsed.
    workflow = gate(repo_root)
    return (workflow.get("on") or workflow.get(True))["pull_request"]["paths"]


# --- the gate is compiled, and matches what is committed ---------------------


def test_the_committed_gate_is_what_the_spec_compiles_to(repo_root):
    """The claim of self-hosting, checked the same way the gate checks everything else."""
    report = check_plan(repo_root, compile_spec(repo_root))
    assert report.missing == []
    assert report.modified == []
    assert report.orphaned == []


def test_the_pipeline_lives_in_the_lockstep_directory(repo_root):
    """The compiler's own source is not a pipeline directory, so the pipeline gets its own."""
    spec = load_spec(repo_root)
    assert spec.in_lockstep_dir


def test_the_spec_and_the_target_both_pass_their_own_checks(repo_root):
    spec = load_spec(repo_root)
    assert lint(spec).findings == []
    assert doctor(spec, repo_root).findings == []


# --- built for a compiler that can move -------------------------------------


def test_the_gate_installs_the_checkout_rather_than_a_release(repo_root):
    """Installing the published `lockstep` would check this branch's spec with main's compiler."""
    runs = [step["run"] for job in gate(repo_root)["jobs"].values() for step in job["steps"] if "run" in step]
    installs = [run for run in runs if run.startswith("uv tool install")]
    assert installs, "the gate has to install a compiler from somewhere"
    assert all(run == 'uv tool install "."' for run in installs), installs


def test_a_change_to_the_emitter_triggers_the_gate(repo_root):
    """`src/` is an input to the output here. A gate that cannot see it is a gate on the spec only."""
    assert "src/**" in triggers(repo_root)


def test_what_the_compiler_is_built_from_triggers_the_gate(repo_root):
    paths = triggers(repo_root)
    assert {"pyproject.toml", "uv.lock"} <= set(paths)


# --- it can actually run ----------------------------------------------------


def test_the_gate_names_nothing_unpublished(repo_root):
    """The composite actions and the executor image do not exist yet; this gate needs neither."""
    text = compile_spec(repo_root).files[CI]
    assert "container:" not in text
    assert "/actions/" not in text
    assert "0000000000000000" not in text


def test_the_only_workflows_generated_here_are_the_gate_and_its_marker(repo_root):
    """A pipeline with no steps compiles to its own CI and nothing else — which is the point."""
    workflows = {p for p in compile_spec(repo_root).files if p.startswith(".github/workflows/")}
    assert workflows == {CI, ".github/workflows/.gitattributes"}


# --- and it does not disturb the workflow that gates the compiler ------------


def test_the_hand_written_workflow_is_not_marked_generated(repo_root):
    """`make ci` is the gate no compiler change can rewrite; collapsing it in diffs hides that."""
    attributes = compile_spec(repo_root).files[".github/workflows/.gitattributes"]
    assert "*.yml" not in attributes
    assert "ci.yml linguist-generated" not in attributes.replace("pipeline-ci.yml", "")


def test_the_hand_written_workflow_survives_a_compile(repo_root):
    """It is not generated, so pruning must never claim it."""
    assert (repo_root / ".github/workflows/ci.yml").is_file()
    assert ".github/workflows/ci.yml" not in compile_spec(repo_root).files


def test_the_surface_calls_an_unused_capability_unused(repo_root):
    """`UNPINNED` on a capability the output never names reads as a pipeline that is not ready."""
    from lockstep.emit.show_surface import render

    surface = render(repo_root)
    assert "- capability actions: `(unused)`" in surface
    assert "UNPINNED" not in surface


def test_the_surface_states_what_a_daily_ceiling_actually_permits(tmp_path):
    """Per agent, per day — so a repository's real daily exposure is that times its agent count.

    Printing only the configured number would let a repository read "5000 credits a day" off its own
    surface document and be wrong by however many agents it has.
    """
    from lockstep.emit.show_surface import _daily_budget_line

    assert _daily_budget_line(5000, 7) == (
        "- daily ceiling: 5000 credits per agent per day — up to 35000 across 7 agent(s)"
    )
    assert "bounds one execution, not a day of them" in _daily_budget_line(None, 7)
