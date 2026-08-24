"""The compiler was the one floating dependency in a system where nothing floats.

Actions pin to a commit, the executor image to a digest, inherited pipelines to a commit — and then
the check enforcing all of that installed its own compiler from a version range. A release could
change what a consumer's security gate ran without a line changing in their repository, which is the
exact event pinning exists to catch, in the one place nobody was looking.
"""

from __future__ import annotations

import json

import pytest
import yaml

from lockstep import __version__
from lockstep.checks import doctor
from lockstep.emit import compile_spec
from lockstep.emit.context import Pins, is_local_requirement, requirement_name
from lockstep.lifecycle import pin
from lockstep.spec.load import load_spec

CI = ".github/workflows/pipeline-ci.yml"


def installs(root):
    ci = yaml.safe_load(compile_spec(root).files[CI])
    return [
        step["run"]
        for job in ci["jobs"].values()
        for step in job["steps"]
        if step.get("run", "").startswith("uv tool install")
    ]


def lock(root):
    return json.loads((root / ".pipeline/pins.lock").read_text())


def codes(report):
    return {finding.code for finding in report.findings}


# --- what a generated gate installs -----------------------------------------


def test_the_gate_installs_an_exact_version(basic_spec_dir):
    assert installs(basic_spec_dir) == [f'uv tool install "in-lockstep=={__version__}"'] * 3


def test_no_generated_check_installs_a_range(basic_spec_dir):
    for line in installs(basic_spec_dir):
        assert ">=" not in line and "<" not in line, line


def test_an_unpinned_compiler_still_compiles_from_the_manifest_range(basic_root):
    """Refusing would make `pin` a prerequisite for a first compile; the doctor check covers it."""
    pins = lock(basic_root)
    del pins["capabilities"]["compiler"]
    (basic_root / ".pipeline/pins.lock").write_text(json.dumps(pins), encoding="utf-8")
    assert installs(basic_root) == ['uv tool install "in-lockstep>=0.1,<1.0"'] * 3


# --- pinning -----------------------------------------------------------------


def test_pin_records_the_compiler_that_produced_the_output(basic_root):
    """The only version known to reproduce it, which is what the gate asks CI to do."""
    data, _notes, _unresolved = pin(load_spec(basic_root), basic_root, offline=True)
    assert data["capabilities"]["compiler"] == {
        "requirement": "in-lockstep>=0.1,<1.0",
        "version": __version__,
    }


def test_a_changed_pin_is_called_out_rather_than_swapped_in(basic_root):
    """A compiler upgrade is a decision; the note is what makes it one."""
    pins = lock(basic_root)
    pins["capabilities"]["compiler"] = {"requirement": "in-lockstep>=0.1,<1.0", "version": "0.0.9"}
    (basic_root / ".pipeline/pins.lock").write_text(json.dumps(pins), encoding="utf-8")
    _data, notes, _unresolved = pin(load_spec(basic_root), basic_root, offline=True)
    assert any("0.0.9 -> " in note and "review before committing" in note for note in notes)


def test_a_local_path_compiler_is_not_pinned(basic_root):
    """This repository compiling itself: the checkout is the version, there is nothing to record."""
    manifest = basic_root / "pipeline.yaml"
    manifest.write_text(
        manifest.read_text().replace("compiler: in-lockstep>=0.1,<1.0", 'compiler: "."'), encoding="utf-8"
    )
    data, notes, _unresolved = pin(load_spec(basic_root), basic_root, offline=True)
    assert any("local path, not pinned" in note for note in notes)
    assert "compiler" not in data["capabilities"]

    (basic_root / ".pipeline/pins.lock").write_text(json.dumps(data), encoding="utf-8")
    assert installs(basic_root) == ['uv tool install "."'] * 3


def test_a_lock_pinned_against_a_different_requirement_is_refused(basic_root):
    """The same rule the other capabilities follow: a pin describing something else is not a pin."""
    from lockstep.errors import EmitError

    manifest = basic_root / "pipeline.yaml"
    manifest.write_text(
        manifest.read_text().replace("compiler: in-lockstep>=0.1,<1.0", "compiler: in-lockstep>=2"),
        encoding="utf-8",
    )
    with pytest.raises(EmitError) as error:
        compile_spec(basic_root)
    assert "capabilities.compiler" in error.value.message


# --- doctor -------------------------------------------------------------------


def test_an_unpinned_compiler_is_reported(basic_root):
    pins = lock(basic_root)
    del pins["capabilities"]["compiler"]
    (basic_root / ".pipeline/pins.lock").write_text(json.dumps(pins), encoding="utf-8")
    assert "DOC023" in codes(doctor(load_spec(basic_root), basic_root))


def test_compiling_with_a_different_compiler_than_the_pin_is_reported(basic_root):
    """The committed output came from one version and is about to be checked by another."""
    pins = lock(basic_root)
    pins["capabilities"]["compiler"]["version"] = "0.0.9"
    (basic_root / ".pipeline/pins.lock").write_text(json.dumps(pins), encoding="utf-8")
    report = doctor(load_spec(basic_root), basic_root)
    assert "DOC024" in codes(report)
    finding = next(f for f in report.findings if f.code == "DOC024")
    assert "0.0.9" in finding.message and __version__ in finding.message


def test_a_matching_pin_says_nothing(basic_spec_dir):
    assert not {"DOC023", "DOC024"} & codes(doctor(load_spec(basic_spec_dir), basic_spec_dir))


# --- the requirement helpers --------------------------------------------------


@pytest.mark.parametrize(
    ("requirement", "name"),
    [
        ("in-lockstep>=0.1,<1.0", "in-lockstep"),
        ("in-lockstep==0.4.0", "in-lockstep"),
        ("in-lockstep", "in-lockstep"),
        ("in-lockstep[extra]>=1", "in-lockstep"),
    ],
)
def test_the_distribution_name_survives_any_range(requirement, name):
    assert requirement_name(requirement) == name


@pytest.mark.parametrize("requirement", [".", "./compiler", "../lockstep", "/opt/lockstep", "file:///x"])
def test_paths_are_recognised_as_local(requirement):
    assert is_local_requirement(requirement)


@pytest.mark.parametrize("requirement", ["in-lockstep", "in-lockstep>=0.1", "lockstep==1.0"])
def test_distributions_are_not(requirement):
    assert not is_local_requirement(requirement)


def test_an_unset_requirement_falls_back_to_the_distribution_name():
    assert Pins().compiler_install() == "in-lockstep"
