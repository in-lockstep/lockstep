"""What an adopter's own type checker can see. Discharges `GATE-PLUGIN-2`.

O2 makes a promise about how little a person writes by hand — `init` leaves them one Python file —
and O8 makes a promise about extending the framework from that file without editing ours. Both
promises land on the same module, and until this the module was the one place an adopter's tooling
went blind: no PEP 561 marker shipped, so `mypy` answered every framework import with
`import-untyped` and the only way past it was `ignore_missing_imports`, which silences the
adopter's own mistakes along with ours.

Two halves, and the second is the one worth having. Shipping the marker is a one-line change.
Shipping a marker over a scaffold that then reports four errors would be worse than not shipping
one, because the adopter meets those errors on their first run and concludes the framework does not
type-check. So the assertions below are about what `init` actually writes, not about the marker.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from in_lockstep.cli import main

ROOT = Path(__file__).resolve().parents[2]


def _package_dir() -> Path:
    import in_lockstep

    return Path(in_lockstep.__file__).parent


def test_the_package_carries_the_marker_where_python_finds_it():
    """Beside `__init__.py` of the imported package, not at a path in the source layout.

    Asserted against the import rather than against `src/`, because those are the same directory
    for an editable install and different ones for a wheel, and the wheel is the case that matters.
    """
    assert (_package_dir() / "py.typed").is_file()


def test_the_wheel_ships_the_directory_the_marker_is_in():
    """The marker only travels if the build copies the package directory whole.

    `artifacts` exists in this file for the cassettes, so the packaging config has already been
    surprised once by a build that collected `.py` files and nothing else. `py.typed` has no
    extension either.
    """
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    packages = config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert "src/in_lockstep" in packages
    assert (ROOT / "src" / "in_lockstep" / "py.typed").is_file()


def _scaffolded(tmp_path: Path, *flags: str) -> str:
    """A real `init` run in a real empty repository, not a rendered template."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as here:
        root = Path(here)
        (root / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
        (root / "uv.lock").touch()
        (root / "tests").mkdir()
        subprocess.run(["git", "init", "-q", "."], cwd=root, check=True)
        result = runner.invoke(main, ["init", *flags])
        assert result.exit_code == 0, result.output
        return (root / ".lockstep" / "lockstep.py").read_text()


def _top_level_names(source: str) -> list[str]:
    return [
        node.name
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    ]


@pytest.mark.parametrize("flags", [(), ("--implement",), ("--fix",), ("--implement", "--fix")])
def test_no_scaffold_defines_the_same_name_twice(tmp_path, flags):
    """`--implement` and `--fix` each carry `_last_unsuccessful`, so asking for both wrote it
    twice — the second shadowing the first, in a file `init` had just generated.

    The parametrisation is the point rather than decoration: the defect existed only in the
    combination, and each verb alone was clean. A test over one flag would have stayed green
    through the entire life of the bug.
    """
    names = _top_level_names(_scaffolded(tmp_path, *flags))
    duplicated = sorted({name for name in names if names.count(name) > 1})
    assert not duplicated, f"init {' '.join(flags) or '(no flags)'} defines {duplicated} twice"


def test_the_scaffold_still_registers_every_workflow_it_advertises(tmp_path):
    """The control for the test above. Dropping a duplicate definition must not drop a workflow,
    and the cheap way to satisfy a de-duplicator is to delete more than it should."""
    source = _scaffolded(tmp_path, "--implement", "--fix")
    for workflow in ("implement/from-ticket", "implement/propose", "fix/from-ticket", "fix/propose"):
        assert f'@workflow(id="{workflow}")' in source
    # And the surviving helper is still reachable from both halves that call it — each naming its
    # own family, which is what stops a `/fix` report quoting an `implement/` run (#251). The two
    # calls differing while the two DEFINITIONS stay byte-identical is exactly the arrangement the
    # de-duplicator requires, so asserting both together is asserting that they can coexist.
    assert source.count("def _last_unsuccessful(") == 1
    assert source.count('_last_unsuccessful(key, "implement/")') == 1
    assert source.count('_last_unsuccessful(key, "fix/")') == 1


@pytest.mark.parametrize("flags", [(), ("--implement", "--fix")])
def test_what_init_writes_survives_strict_mypy(tmp_path, flags):
    """The assertion the marker exists to make true, run the way an adopter would run it.

    `--strict` because that is what this repository holds itself to, and shipping a marker while
    handing an extension author a module they cannot hold to the same standard would be a
    half-kept promise. Run from a directory with no config of its own, so the repository's
    `[tool.mypy]` cannot supply a setting an adopter would not have.
    """
    source = _scaffolded(tmp_path, *flags)
    module = tmp_path / "adopter_lockstep.py"
    module.write_text(source)
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", "--no-incremental", str(module)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
