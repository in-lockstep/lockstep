"""Golden-file tests.

The compiler is a pure function from spec to files, so the whole output tree is pinned. Set
LOCKSTEP_REGEN=1 to rewrite the goldens after an intentional change, then read the diff.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lockstep.emit import compile_spec

HERE = Path(__file__).parent
FIXTURE = HERE / "fixtures" / "basic"
GOLDEN = HERE / "golden" / "basic"
REGEN = os.environ.get("LOCKSTEP_REGEN") == "1"


def golden_files() -> dict[str, str]:
    if not GOLDEN.is_dir():
        return {}
    return {
        str(path.relative_to(GOLDEN)): path.read_text(encoding="utf-8")
        for path in sorted(GOLDEN.rglob("*"))
        if path.is_file()
    }


@pytest.fixture(scope="module")
def plan_files() -> dict[str, str]:
    return compile_spec(FIXTURE).files


def test_regenerate_goldens(plan_files):
    if not REGEN:
        pytest.skip("set LOCKSTEP_REGEN=1 to rewrite goldens")
    for path in sorted(GOLDEN.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    for relative, content in plan_files.items():
        target = GOLDEN / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def test_output_tree_matches_the_golden_tree(plan_files):
    assert sorted(plan_files) == sorted(golden_files())


@pytest.mark.parametrize("relative", sorted(golden_files()) or ["<no goldens>"])
def test_generated_file_matches_golden(relative, plan_files):
    if relative == "<no goldens>":
        pytest.skip("goldens not generated yet")
    assert plan_files[relative] == golden_files()[relative]


def test_compilation_is_deterministic():
    assert compile_spec(FIXTURE).files == compile_spec(FIXTURE).files


def test_compilation_is_independent_of_location(basic_root):
    """Nothing in the output may depend on the absolute path the spec happens to live at."""
    assert compile_spec(basic_root).files == compile_spec(FIXTURE).files
