"""`lockstep init`: the pipeline you get on day one.

The scaffold is a working pipeline, not a set of empty directories — so these tests hold it to the
same standard as any other: it must compile, lint clean, and pass the drift gate.
"""

from __future__ import annotations

import json

import pytest
import yaml
from click.testing import CliRunner

from lockstep.checks import doctor, lint
from lockstep.cli import EXIT_OK, EXIT_SPEC, main
from lockstep.emit import compile_spec
from lockstep.emit.writer import check_plan, write_plan
from lockstep.errors import LockstepError
from lockstep.scaffold import scaffold
from lockstep.spec.load import load_spec

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


def run(*args):
    return CliRunner().invoke(main, list(args))


@pytest.fixture
def pipeline(tmp_path):
    root = tmp_path / "new"
    root.mkdir()
    scaffold(root, "release-notes", "staging")
    # The scaffold inherits the retro pipeline, so its definitions have to be on disk before
    # anything compiles. For a `lockstep:` upstream this copies from the installed compiler — no
    # network, no lock entry — which is why `init` can reasonably print it as the next step.
    from lockstep.lifecycle import fetch
    from lockstep.spec.load import load_manifest_only

    fetch(load_manifest_only(root), root)
    return root


@pytest.fixture
def pinned(pipeline):
    from lockstep.lifecycle import pin, write_pins

    data, _, _ = pin(load_spec(pipeline), pipeline, actions_sha=SHA, exec_digest=DIGEST, offline=True)
    external = data.setdefault("external", {})
    external["actions/checkout"] = {"tag": "v4", "sha": "c" * 40}
    # The scaffold retains run history, which puts a metering job in every workflow.
    for action in ("actions/download-artifact", "actions/upload-artifact"):
        external[action] = {"tag": "v5", "sha": "d" * 40}
    write_pins(pipeline, data)
    return pipeline


def test_the_scaffold_parses_as_a_spec(pipeline):
    spec = load_spec(pipeline)
    assert spec.manifest.name == "release-notes"
    assert "release-notes" in spec.commands
    assert "summarizer" in spec.agents


def test_the_scaffold_demonstrates_the_shape_that_matters(pipeline):
    """A deterministic step producing work, an agent fanned out over it, a deterministic consumer."""
    steps = load_spec(pipeline).commands["release-notes"].steps
    assert [step.kind.value for step in steps] == ["script", "agent", "builtin"]
    assert steps[1].foreach is not None
    assert steps[1].parallel == 3


def test_the_scaffold_lints_clean(pipeline):
    assert lint(load_spec(pipeline)).findings == []


def test_the_scaffold_only_needs_pinning_to_be_target_ready(pipeline):
    codes = {finding.code for finding in doctor(load_spec(pipeline), pipeline).findings}
    # DOC023 is the compiler pin — also a thing `lockstep pin` resolves, not a defect in the spec.
    assert codes <= {"DOC001", "DOC002", "DOC012", "DOC023"}


def test_a_pinned_scaffold_is_target_ready(pinned):
    assert doctor(load_spec(pinned), pinned).ok


def test_a_pinned_scaffold_compiles(pinned):
    files = compile_spec(pinned).files
    assert ".github/workflows/release-notes.yml" in files
    assert ".github/workflows/aw-summarizer.md" in files
    assert ".github/workflows/pipeline-ci.yml" in files
    assert "SECRETS.md" in files


def test_a_compiled_scaffold_passes_its_own_drift_gate(pinned):
    write_plan(pinned, compile_spec(pinned))
    assert check_plan(pinned, compile_spec(pinned)).clean


def test_the_scaffolded_agent_is_read_only(pinned):
    text = compile_spec(pinned).files[".github/workflows/aw-summarizer.md"]
    front = yaml.safe_load(text.split("---")[1])
    assert front["permissions"] == "read-all"
    assert front["max-ai-credits"] == 20


def test_the_scaffolded_script_produces_matrix_ready_items(pipeline):
    import importlib.util

    spec = importlib.util.spec_from_file_location("collect_items", pipeline / "scripts" / "collect-items.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    items = module.collect(3)
    assert len(items) == 3
    assert all("key" in item for item in items)
    assert len({item["key"] for item in items}) == 3


def test_the_scaffolded_eval_case_is_valid_json(pipeline):
    case = pipeline / "evals" / "summarizer" / "cases" / "one-item.json"
    assert json.loads(case.read_text())["input"]["key"]


def test_scaffolding_over_existing_files_is_refused(pipeline):
    with pytest.raises(LockstepError) as excinfo:
        scaffold(pipeline, "release-notes", "staging")
    assert "already exist" in excinfo.value.render()


def test_force_overwrites(pipeline):
    (pipeline / "pipeline.yaml").write_text("clobbered", encoding="utf-8")
    scaffold(pipeline, "release-notes", "staging", force=True)
    assert "spec: 1" in (pipeline / "pipeline.yaml").read_text()


def test_an_unusable_name_is_refused(tmp_path):
    with pytest.raises(LockstepError):
        scaffold(tmp_path, "not a name!", "staging")


def test_init_reports_what_to_do_next(tmp_path):
    result = run("init", "--dir", str(tmp_path / "p"), "--name", "demo")
    assert result.exit_code == EXIT_OK
    assert "lockstep pin" in result.output
    assert "lockstep compile" in result.output


def test_init_refuses_an_unknown_target(tmp_path):
    assert (
        run("init", "--dir", str(tmp_path / "p"), "--name", "demo", "--target", "jenkins").exit_code
        != EXIT_OK
    )


def test_init_into_a_dirty_directory_is_refused(tmp_path):
    run("init", "--dir", str(tmp_path / "p"), "--name", "demo")
    assert run("init", "--dir", str(tmp_path / "p"), "--name", "demo").exit_code == EXIT_SPEC
