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
import inspect
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
    # What `init` writes REGISTERS the workflows now; it does not define them. The de-duplicator
    # still runs over the configuration half, and dropping too much there would take a
    # registration with it — which is the property this control is for.
    for family in ("implement", "fix"):
        assert f"from in_lockstep.workflows import {family} as {family}_workflows" in source
        assert f"{family}_workflows.register()" in source
    # And the helper the two blocks used to carry twice is one definition in the framework, so
    # there is no second copy for a de-duplicator to have to delete.
    assert "def _last_unsuccessful(" not in source
    assert "def last_unsuccessful(" not in source


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


def test_what_init_writes_carries_no_workflow_bodies(tmp_path):
    """`GATE-PLUGIN-3`. The point of the change: an adopter gets a reference, not a copy.

    `init --implement --fix` used to append ~560 lines of workflow source. Two copies of a process
    is two places to fix it, and the two had drifted 96 lines of real code apart by the time anybody
    measured — with only this repository's copy under test. Worse, a fix could never reach a tree
    that had already run `init`.

    Asserted as an absence of BODIES rather than a line count, because a line count is satisfied by
    reformatting. What must not be there is the code that decides what a run does.
    """
    source = _scaffolded(tmp_path, "--implement", "--fix")
    for body in ("async def implement_from_ticket", "async def fix_from_ticket", "await ctx.do("):
        assert body not in source, f"init still writes a workflow body: {body}"
    assert "from in_lockstep.workflows import" in source


def test_the_shipped_process_can_be_read_without_being_copied(tmp_path):
    """`GATE-PLUGIN-3`, the other half. Reading was the part people wanted; owning was what made
    every fix land twice and never reach a repository that had already scaffolded.

    `init --eject` was built first and dropped: it reintroduced the import-merging machinery this
    change exists to retire, and left duplicate imports when both verbs were ejected into one file.
    `show-workflow` gets the same value with none of it, because printing is not forking.

    Printed from `inspect.getsource` on the module that is actually imported, so what a person
    reads is what runs -- which a second copy in their own file could never promise.
    """
    from click.testing import CliRunner

    from in_lockstep.cli import main
    from in_lockstep.workflows import implement

    listing = CliRunner().invoke(main, ["show-workflow"])
    assert listing.exit_code == 0
    assert "implement/propose" in listing.output and "fix/report" in listing.output

    one = CliRunner().invoke(main, ["show-workflow", "implement/report"])
    assert one.exit_code == 0
    assert "async def implement_report" in one.output
    # The shipped text, not a paraphrase: a line only the framework module carries.
    assert one.output.strip() in inspect.getsource(implement)

    unknown = CliRunner().invoke(main, ["show-workflow", "nope/x"])
    assert unknown.exit_code != 0
    assert "Shipped:" in unknown.output, "a refusal names what does exist"


def test_nothing_init_writes_is_a_workflow_body(tmp_path):
    """The property `--eject` used to violate on request. There is no flag that puts a copy of a
    process into an adopter's module any more, which is what makes one copy a fact rather than a
    default."""
    source = _scaffolded(tmp_path, "--implement", "--fix")
    for body in ("async def implement_from_ticket", "async def fix_from_ticket", "await ctx.do("):
        assert body not in source
