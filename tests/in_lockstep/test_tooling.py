"""Where the deterministic adapters find the tools they run (GATE-TOOLING-1, issue 167).

The adapters bind to what the repository already has. Which interpreter ran pytest was
`sys.executable`: right under `uv run` in the checkout, wrong for every installed copy, whose
interpreter holds `in-lockstep` and nothing of the repository's. These tests state the order
(the repository's `.venv`, this process only when it lives inside the repository, then PATH), that
nothing is guessed past it, and that `ls` and `doctor` show the answer before a run.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from in_lockstep.adapters import CommandBuild, CommandTest, CommandValidate, PytestTest, RuffValidate, tooling
from in_lockstep.adapters.sandbox import Sandbox
from in_lockstep.cli import main
from in_lockstep.core.types import Build, Locatable, Test, Validate


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A directory to `init` in, with the CI variables that would make `detect()` look elsewhere
    removed. The same shape as test_cli's fixture, for the same reason it clears `GITHUB_*`."""
    monkeypatch.chdir(tmp_path)
    for name in [k for k in os.environ if k.startswith("GITHUB_")]:
        monkeypatch.delenv(name, raising=False)
    yield tmp_path


def _executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


class _Recording:
    def __init__(self) -> None:
        self.command: list[str] = []

    async def run(self, command, *, cwd=None, timeout=900.0):  # noqa: ANN001
        self.command = list(command)
        return type("R", (), {"exit_code": 0, "stdout": "[]", "stderr": ""})()


def test_gate_tooling_1_a_binary_is_found_in_the_venv_then_beside_the_interpreter_then_on_path(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    monkeypatch.setattr(tooling.shutil, "which", lambda name: "/usr/local/bin/ruff")
    beside = _executable(tmp_path / "env" / "bin" / "python")

    on_path = tooling.binary("ruff", str(tmp_path), Sandbox(), beside=str(beside))
    assert on_path.path == "/usr/local/bin/ruff" and on_path.how == "ruff on PATH"
    # This platform's layout only: a `not found` line should name places this machine could have.
    assert on_path.tried == (
        str(tmp_path.resolve() / ".venv/bin/ruff"),
        str(tmp_path / "env/bin/ruff"),
        "ruff on PATH",
    )

    next_to_python = _executable(tmp_path / "env" / "bin" / "ruff")
    assert tooling.binary("ruff", str(tmp_path), Sandbox(), beside=str(beside)).path == str(next_to_python)

    in_venv = _executable(tmp_path / ".venv" / "bin" / "ruff")
    found = tooling.binary("ruff", str(tmp_path), Sandbox(), beside=str(beside))
    assert found.path == str(in_venv) and found.how == "the repository's .venv"

    monkeypatch.setattr(tooling.shutil, "which", lambda name: None)
    nowhere = tooling.binary("make", str(tmp_path), Sandbox())
    assert nowhere.path is None and nowhere.render().startswith("make  not found  (looked for ")


def test_a_containerized_adapter_resolves_the_name_inside_the_image(monkeypatch) -> None:  # noqa: ANN001
    """This host's filesystem says nothing about what the image has, so nothing is probed."""
    monkeypatch.setattr(Sandbox, "runtime", lambda self: "/usr/bin/docker")
    box = Sandbox(image="python:3.12")
    assert tooling.interpreter("/repo", box).path == "python"
    assert tooling.binary("ruff", "/repo", box).path == "ruff"
    assert "container" in tooling.interpreter("/repo", box).how


def test_ruff_validate_runs_the_repositorys_ruff(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """GATE-TOOLING-1 for the linter: `ruff` on the sandbox's PATH was the tool's, or nothing."""
    monkeypatch.setattr(tooling.shutil, "which", lambda name: None)
    venv_ruff = _executable(tmp_path / ".venv" / "bin" / "ruff")
    _executable(tmp_path / ".venv" / "bin" / "python")
    sandbox = _Recording()
    ctx = type("C", (), {"repo": type("R", (), {"root": str(tmp_path)})})()

    outcome = asyncio.run(RuffValidate(sandbox=sandbox).invoke(ctx, Validate()))
    assert outcome.succeeded
    assert sandbox.command[0] == str(venv_ruff)


def test_ruff_missing_everywhere_is_a_refusal_naming_what_it_tried(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(tooling.shutil, "which", lambda name: None)
    monkeypatch.setattr(tooling.sys, "executable", "/opt/tool/bin/python")
    sandbox = _Recording()
    ctx = type("C", (), {"repo": type("R", (), {"root": str(tmp_path)})})()
    outcome = asyncio.run(RuffValidate(sandbox=sandbox).invoke(ctx, Validate()))
    assert outcome.status.value == "errored"
    assert ".venv/bin/ruff" in outcome.reason and "ruff on PATH" in outcome.reason
    assert sandbox.command == []


def test_a_command_adapter_says_where_its_binary_comes_from(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(tooling.shutil, "which", lambda name: "/usr/local/bin/npm" if name == "npm" else None)
    adapter = CommandTest(["npm", "test"])
    assert isinstance(adapter, Locatable)
    (where,) = adapter.locations("/repo")
    assert where.tool == "npm" and where.path == "/usr/local/bin/npm"


def _python_repo(repo: Path) -> None:
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n[tool.ruff]\n")
    assert CliRunner().invoke(main, ["init"]).exit_code == 0


def test_ls_prints_where_each_deterministic_binding_resolved_its_tool(repo: Path) -> None:
    """A wrong answer is something to see before a run, not to infer from a red suite after."""
    _python_repo(repo)
    python = _executable(repo / ".venv" / "bin" / "python")
    ruff = _executable(repo / ".venv" / "bin" / "ruff")
    result = CliRunner().invoke(main, ["ls"])
    assert result.exit_code == 0, result.output
    assert f"python  {python}  (the repository's .venv)" in result.output
    assert f"ruff  {ruff}  (the repository's .venv)" in result.output


def test_doctor_refuses_a_bound_tool_it_cannot_find(repo: Path, monkeypatch) -> None:  # noqa: ANN001
    """DOC180: the path tried is in the message, because "not installed" was the unhelpful half of
    what the two first-time users read."""
    _python_repo(repo)
    monkeypatch.setattr(tooling.shutil, "which", lambda name: None)
    monkeypatch.setattr(tooling.sys, "executable", "")
    result = CliRunner().invoke(main, ["doctor"])
    assert "DOC180" in result.output, result.output
    assert "Test -> PytestTest found no python" in result.output
    # doctor wraps long lines, and a tmp path is long; compare with whitespace removed.
    assert str(repo / ".venv" / "bin" / "python") in "".join(result.output.split())


def test_doctor_probes_that_the_found_interpreter_can_run_pytest(repo: Path, monkeypatch) -> None:  # noqa: ANN001
    """DOC181: found is not enough; an interpreter with no pytest in it is the #167 failure with
    a nicer path. The probe runs `import pytest` there."""
    _python_repo(repo)
    _executable(
        repo / ".venv" / "bin" / "python",
        "#!/bin/sh\necho 'ModuleNotFoundError: No module named pytest' >&2\nexit 1\n",
    )
    _executable(repo / ".venv" / "bin" / "ruff")
    result = CliRunner().invoke(main, ["doctor"])
    assert "DOC181" in result.output, result.output
    assert "import pytest" in result.output and "No module named pytest" in result.output


# -- what the review of issue 167 found ---------------------------------------------------------


def test_a_relative_root_resolves_to_an_absolute_path(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """`PytestTest(cwd="packages/api")` is an existing shape. A relative resolved path is execed
    by the sandbox relative to a different working directory, where it does not exist."""
    monkeypatch.chdir(tmp_path)
    python = _executable(tmp_path / "packages" / "api" / ".venv" / "bin" / "python")
    (where,) = PytestTest(cwd="packages/api").locations("/ignored")
    assert where.path == str(python.resolve())
    assert Path(where.path).is_absolute()


def test_a_red_suite_that_mentions_the_missing_module_phrase_is_still_red(tmp_path: Path) -> None:
    """The shape, not the substring: a suite asserting on that text ends in a summary line, and
    `python -m pytest` with no pytest ends in the phrase with no summary at all."""
    _executable(tmp_path / ".venv" / "bin" / "python")

    class _RedButChatty:
        async def run(self, command, *, cwd=None, timeout=900.0):  # noqa: ANN001
            out = (
                "FAILED test_x.py::test_x - AssertionError: assert 'No module named pytest' == ''\n"
                "1 failed in 0.01s\n"
            )
            return type("R", (), {"exit_code": 1, "stdout": out, "stderr": ""})()

    ctx = type("C", (), {"repo": type("R", (), {"root": str(tmp_path)})})()
    outcome = asyncio.run(PytestTest(sandbox=_RedButChatty()).invoke(ctx, Test()))
    assert outcome.status.value == "failed"
    assert outcome.findings[0].id == "test.expectation_unmet"


def test_a_command_adapter_runs_the_repositorys_venv_tool_and_keeps_a_path_tool_bare(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    """What `ls` prints is what runs: a `mypy` that lives only in the repository's `.venv` is
    substituted by its path, which is on nobody's PATH; a `make` found on PATH keeps its name,
    since the sandbox's PATH would find the same one."""
    mypy = _executable(tmp_path / ".venv" / "bin" / "mypy")
    monkeypatch.setattr(tooling.shutil, "which", lambda name: "/usr/bin/make" if name == "make" else None)
    ctx = type("C", (), {"repo": type("R", (), {"root": str(tmp_path)})})()

    sandbox = _Recording()
    asyncio.run(CommandValidate(["mypy", "src"], sandbox=sandbox).invoke(ctx, Validate()))
    assert sandbox.command == [str(mypy.resolve()), "src"]

    sandbox = _Recording()
    asyncio.run(CommandBuild(["make", "build"], sandbox=sandbox).invoke(ctx, Build()))
    assert sandbox.command == ["make", "build"]


def test_exit_127_is_an_error_naming_where_the_tool_was_looked_for(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """The shell's "no such command" is an environment fact, not a verdict on the change."""
    monkeypatch.setattr(tooling.shutil, "which", lambda name: None)

    class _Missing:
        async def run(self, command, *, cwd=None, timeout=900.0):  # noqa: ANN001
            return type("R", (), {"exit_code": 127, "stdout": "", "stderr": "sh: mypy: not found"})()

    ctx = type("C", (), {"repo": type("R", (), {"root": str(tmp_path)})})()
    outcome = asyncio.run(CommandValidate(["mypy", "src"], sandbox=_Missing()).invoke(ctx, Validate()))
    assert outcome.status.value == "errored"
    assert outcome.reason.startswith("mypy could not be run; looked for ")
    assert "mypy on PATH" in outcome.reason


def test_an_argv0_with_a_directory_in_it_is_the_callers_own_path(tmp_path: Path) -> None:
    """`./gradlew` is relative to where the sandbox runs it, which is not this process's working
    directory, so it is reported as bound rather than looked for and found missing."""
    relative = tooling.binary("./gradlew", str(tmp_path), Sandbox())
    assert relative.path == "./gradlew" and relative.how.startswith("as bound")
    assert relative.probe == ()

    script = _executable(tmp_path / "tools" / "run.sh")
    absolute = tooling.binary(str(script), str(tmp_path), Sandbox())
    assert absolute.path == str(script) and absolute.how == "as bound"

    missing = tooling.binary(str(tmp_path / "tools" / "gone.sh"), str(tmp_path), Sandbox())
    assert missing.path is None and missing.tried == (str(tmp_path / "tools" / "gone.sh"),)


def test_beside_the_interpreter_uses_this_platforms_executable_suffix(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """On Windows the file beside `python.exe` is `ruff.exe`; the venv branch knew and the
    beside branch did not."""
    monkeypatch.setattr(tooling, "_EXE", ".exe")
    monkeypatch.setattr(tooling.shutil, "which", lambda name: None)
    python = _executable(tmp_path / "env" / "bin" / "python.exe")
    ruff = _executable(tmp_path / "env" / "bin" / "ruff.exe")
    found = tooling.binary("ruff", str(tmp_path), Sandbox(), beside=str(python))
    assert found.path == str(ruff) and found.how == "beside the interpreter the suite runs on"


def test_the_python_probe_is_isolated_from_the_working_directory(tmp_path: Path) -> None:
    """`doctor` runs the probe; `-I` keeps a `pytest.py` in the change under review off its path."""
    _executable(tmp_path / ".venv" / "bin" / "python")
    found = tooling.interpreter(str(tmp_path), Sandbox())
    assert found.probe[1:] == ("-I", "-c", "import pytest")
