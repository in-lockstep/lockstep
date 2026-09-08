"""Drop-in detection: read the tree, fit the defaults to it.

The failure this closes is a Node repository silently getting pytest bound, so the tests are
mostly 'a repo shaped like X detects Y and binds Z', plus the JUnit ingestion that lets a
non-pytest runner report which test failed rather than only an exit code.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from in_lockstep.adapters import (
    Build,
    CommandBuild,
    CommandProvision,
    CommandRun,
    CommandTest,
    CommandValidate,
    Provision,
    Run,
    detected_bindings,
    parse_junit,
)
from in_lockstep.adapters.command import Test, Validate
from in_lockstep.adapters.pytest_adapter import PytestTest
from in_lockstep.adapters.ruff_adapter import RuffValidate
from in_lockstep.adapters.sandbox import Sandbox, SandboxResult
from in_lockstep.core.context import BUILD_MANIFESTS, MAKE_TARGETS_SHOWN
from in_lockstep.core.types import VENV_BIN, RunResult
from in_lockstep.lockstep import _detect_facts


def _write(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def test_a_python_repo_is_detected_as_python(tmp_path: Path) -> None:
    _write(tmp_path, {"pyproject.toml": "[tool.pytest.ini_options]\n[tool.ruff]\n"})
    facts = _detect_facts(tmp_path)
    assert facts.stack == "python"
    assert facts.pytest and facts.ruff
    binds = detected_bindings(facts)
    assert (Test, PytestTest) in [(i, type(x)) for i, x in binds]
    assert (Validate, RuffValidate) in [(i, type(x)) for i, x in binds]


def test_a_node_repo_binds_a_command_not_pytest(tmp_path: Path) -> None:
    """The headline: a Node repo must not silently get pytest bound."""
    _write(tmp_path, {"package.json": '{"scripts": {"test": "jest"}}'})
    facts = _detect_facts(tmp_path)
    assert facts.stack == "node"
    assert facts.test_command == ("npm", "test")
    kinds = {i: type(x) for i, x in detected_bindings(facts)}
    assert kinds[Test] is CommandTest
    assert Validate not in kinds, "no linter detected, so nothing is bound for Validate"


def test_a_node_repo_with_eslint_binds_command_validate(tmp_path: Path) -> None:
    _write(tmp_path, {"package.json": '{"scripts": {"test": "jest"}}', ".eslintrc.json": "{}"})
    facts = _detect_facts(tmp_path)
    assert facts.lint_command == ("npx", "eslint", ".")
    assert {i: type(x) for i, x in detected_bindings(facts)}[Validate] is CommandValidate


def test_an_empty_directory_binds_nothing_rather_than_guessing(tmp_path: Path) -> None:
    """A wrong default that runs is worse than an honest absence."""
    facts = _detect_facts(tmp_path)
    assert facts.stack == ""
    assert detected_bindings(facts) == []


def test_ruff_wins_over_eslint_when_both_are_present(tmp_path: Path) -> None:
    """A mixed repo that is primarily Python should not get a Node linter command."""
    _write(
        tmp_path,
        {"pyproject.toml": "[tool.ruff]\n", "package.json": '{"scripts":{"test":"x"}}', ".eslintrc": ""},
    )
    facts = _detect_facts(tmp_path)
    assert facts.ruff and not facts.lint_command


def test_the_facts_summary_reads_for_a_human(tmp_path: Path) -> None:
    _write(tmp_path, {"pyproject.toml": "[tool.pytest.ini_options]\n", "Dockerfile": "", "CLAUDE.md": ""})
    (tmp_path / "docs").mkdir()
    summary = _detect_facts(tmp_path).summary()
    assert "stack: python" in summary and "tests: pytest" in summary
    assert "Dockerfile" in summary and "docs/" in summary and "CLAUDE.md" in summary


def test_make_targets_are_read_without_phony_or_patterns(tmp_path: Path) -> None:
    _write(tmp_path, {"Makefile": ".PHONY: check\ncheck: fmt lint\n\tdo\nfmt:\n\tdo\n%.o: %.c\n\tdo\n"})
    facts = _detect_facts(tmp_path)
    assert facts.makefile
    assert facts.make_targets == ("check", "fmt")


def test_gitlab_ci_is_detected(tmp_path: Path) -> None:
    _write(tmp_path, {".gitlab-ci.yml": "stages: []\n"})
    assert _detect_facts(tmp_path).ci_host == "gitlab"


def test_a_python_tests_dir_alone_is_not_taken_for_pytest(tmp_path: Path) -> None:
    """A Django/unittest repo keeps tests in tests/ and does not run pytest; binding PytestTest
    from the directory name is the guess the detector refuses to make."""
    _write(tmp_path, {"setup.py": "", "tests/test_x.py": ""})
    (tmp_path / "tests").mkdir(exist_ok=True)
    facts = _detect_facts(tmp_path)
    assert facts.stack == "python"
    assert not facts.pytest, "no pytest marker, so no pytest binding"
    assert not detected_bindings(facts)


def test_a_conftest_is_a_pytest_marker(tmp_path: Path) -> None:
    _write(tmp_path, {"setup.py": "", "conftest.py": ""})
    assert _detect_facts(tmp_path).pytest


def test_eslint_flat_config_is_detected(tmp_path: Path) -> None:
    _write(tmp_path, {"package.json": '{"scripts": {"test": "x"}}', "eslint.config.mjs": "export default []"})
    assert _detect_facts(tmp_path).lint_command == ("npx", "eslint", ".")


def test_make_variable_assignments_are_not_read_as_targets(tmp_path: Path) -> None:
    _write(tmp_path, {"Makefile": "CC:=gcc\nBIN := app\ncheck:\n\tpytest\nfmt::\n\tblack .\n"})
    facts = _detect_facts(tmp_path)
    assert "CC" not in facts.make_targets and "BIN" not in facts.make_targets
    assert "check" in facts.make_targets and "fmt" in facts.make_targets


# -- the generic command adapters -------------------------------------------------------------


class _FakeSandbox:
    def __init__(self, exit_code: int, stdout: str = "", stderr: str = "") -> None:
        self._result = type("R", (), {"exit_code": exit_code, "stdout": stdout, "stderr": stderr})()
        self.commands: list[list[str]] = []
        self.cwds: list[str | None] = []

    async def run(self, command, *, cwd=None, timeout=900.0):  # noqa: ANN001
        self.commands.append(command)
        self.cwds.append(cwd)
        return self._result


def test_command_test_maps_a_clean_exit_to_success() -> None:
    adapter = CommandTest(["npm", "test"], sandbox=_FakeSandbox(0))
    outcome = asyncio.run(adapter.invoke(None, Test()))
    assert outcome.status.value == "succeeded"


def test_command_test_maps_a_nonzero_exit_to_failure() -> None:
    adapter = CommandTest(["npm", "test"], sandbox=_FakeSandbox(1, stdout="1 failed"))
    outcome = asyncio.run(adapter.invoke(None, Test()))
    assert outcome.status.value == "failed"
    assert any(f.id == "test.expectation_unmet" for f in outcome.findings)


def test_command_test_reads_a_junit_report_for_per_test_cases(tmp_path: Path) -> None:
    """Without this a non-pytest suite says red or green but never which test — exactly what a
    fix loop needs to reproduce."""
    (tmp_path / "report.xml").write_text(
        "<testsuite>"
        '<testcase classname="a" name="ok" time="0.1"/>'
        '<testcase classname="a" name="bad" time="0.2"><failure message="boom"/></testcase>'
        '<testcase classname="a" name="skip"><skipped/></testcase>'
        "</testsuite>"
    )
    adapter = CommandTest(["pytest"], sandbox=_FakeSandbox(1), junit="report.xml")
    ctx = type("C", (), {"repo": type("R", (), {"root": str(tmp_path)})})()
    outcome = asyncio.run(adapter.invoke(ctx, Test()))
    report = outcome.value
    assert report is not None
    assert report.total == 3 and report.failed == 1 and report.skipped == 1
    assert {c.id for c in report.cases} == {"a::ok", "a::bad", "a::skip"}
    assert any(c.outcome == "failed" and c.message == "boom" for c in report.cases)


def test_command_test_with_a_junit_report_that_collected_nothing_decides_nothing(tmp_path: Path) -> None:
    (tmp_path / "report.xml").write_text("<testsuite></testsuite>")
    adapter = CommandTest(["pytest"], sandbox=_FakeSandbox(0), junit="report.xml")
    ctx = type("C", (), {"repo": type("R", (), {"root": str(tmp_path)})})()
    outcome = asyncio.run(adapter.invoke(ctx, Test()))
    assert not outcome.decided, "a runner that collected nothing decided nothing"
    assert any(f.id == "test.no_tests_collected" for f in outcome.findings)


def test_a_red_run_is_decided_even_when_the_junit_report_is_missing(tmp_path: Path) -> None:
    """A configured report the runner never wrote (path mismatch, or a crash) must not turn a real
    red run into 'decided nothing' — a nonzero exit is a verdict."""
    adapter = CommandTest(["npm", "test"], sandbox=_FakeSandbox(1), junit="nowhere.xml")
    ctx = type("C", (), {"repo": type("R", (), {"root": str(tmp_path)})})()
    outcome = asyncio.run(adapter.invoke(ctx, Test()))
    assert outcome.status.value == "failed"
    assert outcome.decided, "a red run is decided regardless of the report"


def test_a_clean_exit_with_a_junit_report_that_ran_nothing_decides_nothing(tmp_path: Path) -> None:
    (tmp_path / "r.xml").write_text("<testsuite></testsuite>")
    adapter = CommandTest(["x"], sandbox=_FakeSandbox(0), junit="r.xml")
    ctx = type("C", (), {"repo": type("R", (), {"root": str(tmp_path)})})()
    outcome = asyncio.run(adapter.invoke(ctx, Test()))
    assert outcome.status.value == "succeeded" and not outcome.decided


def test_the_selector_is_passed_when_the_runner_knows_how(tmp_path: Path) -> None:
    """`Test.selector` narrows to one test only if the adapter is told the runner's flag; the
    seam is real so a reproducer is not silently widened to the whole suite."""
    sandbox = _FakeSandbox(0)
    adapter = CommandTest(["jest"], sandbox=sandbox, selector_arg=("--testNamePattern",))
    asyncio.run(adapter.invoke(None, Test(selector="login")))
    assert sandbox.commands[0] == ["jest", "--testNamePattern", "login"]


def test_a_junit_error_keeps_its_error_outcome(tmp_path: Path) -> None:
    report = parse_junit(
        '<testsuite><testcase name="t"><error message="import blew up"/></testcase></testsuite>'
    )
    assert report.failed == 1
    assert report.cases[0].outcome == "error", "an error is distinct from an assertion failure"


def test_command_test_expect_fail_inverts_the_verdict() -> None:
    """A reproducer that does not fail proves nothing."""
    green = CommandTest(["x"], sandbox=_FakeSandbox(0))
    assert asyncio.run(green.invoke(None, Test(expect="fail"))).status.value == "failed"
    red = CommandTest(["x"], sandbox=_FakeSandbox(1))
    assert asyncio.run(red.invoke(None, Test(expect="fail"))).status.value == "succeeded"


def test_command_validate_maps_exit_code_and_surfaces_output() -> None:
    clean = CommandValidate(["eslint"], sandbox=_FakeSandbox(0))
    assert asyncio.run(clean.invoke(None, Validate())).status.value == "succeeded"
    dirty = CommandValidate(["eslint"], sandbox=_FakeSandbox(2, stdout="3 problems"))
    outcome = asyncio.run(dirty.invoke(None, Validate()))
    assert outcome.status.value == "failed"
    assert "3 problems" in outcome.findings[0].message


def test_parse_junit_is_tolerant_of_malformed_reports() -> None:
    assert parse_junit("not xml at all").total == 0
    assert parse_junit("<testsuites></testsuites>").total == 0


def test_parse_junit_reads_a_testsuites_root() -> None:
    report = parse_junit('<testsuites><testsuite><testcase name="t"/></testsuite></testsuites>')
    assert report.total == 1 and report.passed == 1


def test_an_empty_command_is_refused() -> None:
    import pytest

    with pytest.raises(ValueError, match="needs a command"):
        CommandTest([])
    with pytest.raises(ValueError, match="needs a command"):
        CommandValidate([])


# -- build and run, bound to what the repository already has (issue 162) ----------------------
#
# Detection found `make build` and `make run`, printed them in the summary, and bound neither.
# The rule these tests state: the Makefile and package.json serve the verbs where an exit code is
# the whole answer, and a target that is not in the file is not guessed.


def test_a_makefile_serves_every_verb_it_has_a_target_for_when_nothing_structured_does(
    tmp_path: Path,
) -> None:
    """A Go repository: no pytest, no ruff, and a Makefile that says how it is tested, linted,
    built and run. All four verbs bind to it, each reporting an exit code."""
    _write(
        tmp_path,
        {
            "Makefile": (
                "build:\n\tgo build ./...\nrun:\n\t./app\n"
                "test:\n\tgo test ./...\nlint:\n\tgolangci-lint run\n"
            )
        },
    )
    facts = _detect_facts(tmp_path)
    assert facts.test_command == ("make", "test")
    assert facts.lint_command == ("make", "lint")
    assert facts.build_command == ("make", "build")
    assert facts.run_command == ("make", "run")
    kinds = {i: type(x) for i, x in detected_bindings(facts)}
    assert kinds == {Test: CommandTest, Validate: CommandValidate, Build: CommandBuild, Run: CommandRun}
    assert "tests: make test" in facts.summary() and "lint: make lint" in facts.summary()


def test_a_makefile_without_those_targets_binds_neither(tmp_path: Path) -> None:
    """Absent is not guessed. An invented `make build` is a binding that fails at run time."""
    _write(tmp_path, {"Makefile": "check: fmt lint\n\tdo\nfmt:\n\tdo\n"})
    facts = _detect_facts(tmp_path)
    assert facts.build_command == () and facts.run_command == ()
    bound = {i for i, _ in detected_bindings(facts)}
    assert Build not in bound and Run not in bound


def test_package_json_build_and_start_scripts_bind_build_and_run(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"package.json": '{"scripts": {"test": "jest", "build": "tsc", "start": "node dist/main.js"}}'},
    )
    facts = _detect_facts(tmp_path)
    assert facts.build_command == ("npm", "run", "build")
    assert facts.run_command == ("npm", "start")
    kinds = {i: type(x) for i, x in detected_bindings(facts)}
    assert kinds[Build] is CommandBuild and kinds[Run] is CommandRun and kinds[Test] is CommandTest


def test_a_makefile_target_beats_a_package_json_script_for_the_same_verb(tmp_path: Path) -> None:
    """The Makefile is the repository's own statement of how it is built, whatever it wraps; and
    package.json still serves the verb the Makefile says nothing about."""
    _write(
        tmp_path,
        {
            "Makefile": "build:\n\tnpm run build\n",
            "package.json": '{"scripts": {"build": "tsc", "start": "node ."}}',
        },
    )
    facts = _detect_facts(tmp_path)
    assert facts.build_command == ("make", "build")
    assert facts.run_command == ("npm", "start")


def test_pytest_and_ruff_still_beat_makefile_test_and_lint_targets(tmp_path: Path) -> None:
    """The precedence, stated: structured output wins the verb where the structure matters. A fix
    loop reproduces from pytest's per-test cases and a review reads ruff's per-rule findings;
    `make test` and `make lint` would replace both with an exit code. Build has no structured
    tool, so the Makefile serves it outright."""
    _write(
        tmp_path,
        {
            "pyproject.toml": "[tool.pytest.ini_options]\n[tool.ruff]\n",
            "Makefile": "test:\n\tpytest\nlint:\n\truff check\nbuild:\n\tpython -m build\n",
        },
    )
    facts = _detect_facts(tmp_path)
    assert facts.test_command == () and facts.lint_command == ()
    kinds = {i: type(x) for i, x in detected_bindings(facts)}
    assert kinds[Test] is PytestTest and kinds[Validate] is RuffValidate
    assert kinds[Build] is CommandBuild


def test_the_makefile_beats_package_json_for_test_as_it_does_for_build(tmp_path: Path) -> None:
    """One rule, not four: where nothing structured serves a verb, the Makefile is the repository's
    own statement and package.json is the fallback, whatever the target wraps."""
    _write(
        tmp_path,
        {
            "Makefile": "test:\n\tnpm test -- --ci\n",
            "package.json": '{"scripts": {"test": "jest", "start": "node ."}}',
        },
    )
    facts = _detect_facts(tmp_path)
    assert facts.test_command == ("make", "test")
    assert facts.run_command == ("npm", "start")


def test_eslint_still_serves_validate_when_the_makefile_has_no_lint_target(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"Makefile": "build:\n\ttsc\n", "package.json": '{"scripts": {"test": "x"}}', "eslint.config.js": ""},
    )
    facts = _detect_facts(tmp_path)
    assert facts.lint_command == ("npx", "eslint", ".")
    assert facts.build_command == ("make", "build")


def test_a_build_target_past_the_summary_cap_is_still_found(tmp_path: Path) -> None:
    """The fact is the whole target list; only the line a person reads is shortened. The old cap
    lived in the parser, so a `build` target ninth in the file was not a build target."""
    targets = [f"t{i}" for i in range(MAKE_TARGETS_SHOWN + 1)] + ["build"]
    _write(tmp_path, {"Makefile": "".join(f"{name}:\n\tdo\n" for name in targets)})
    facts = _detect_facts(tmp_path)
    assert facts.build_command == ("make", "build")
    assert len(facts.make_targets) == MAKE_TARGETS_SHOWN + 2
    line = next(s for s in facts.summary() if s.startswith("Makefile"))
    assert "+2 more" in line and "build" not in line


def test_the_facts_summary_names_the_build_and_run_commands(tmp_path: Path) -> None:
    _write(tmp_path, {"Makefile": "build:\n\tdo\nrun:\n\tdo\n"})
    summary = _detect_facts(tmp_path).summary()
    assert "build: make build" in summary and "run: make run" in summary


def test_command_build_passes_the_target_and_args_and_does_not_guess_artifacts() -> None:
    sandbox = _FakeSandbox(0, stdout="ok\n")
    outcome = asyncio.run(
        CommandBuild(["make", "build"], sandbox=sandbox).invoke(
            None, Build(target="release", args=("-j", "2"))
        )
    )
    assert sandbox.commands == [["make", "build", "release", "-j", "2"]]
    assert outcome.succeeded and outcome.decided
    assert outcome.value is not None
    assert outcome.value.artifacts == (), "what a build produced is not guessed from the tree"
    assert outcome.value is not None
    assert outcome.value.log == "ok"


def test_command_build_maps_a_nonzero_exit_to_a_blocking_finding_with_the_output_tail() -> None:
    sandbox = _FakeSandbox(2, stderr="\n".join(f"line {i}" for i in range(30)))
    outcome = asyncio.run(CommandBuild(["make", "build"], sandbox=sandbox).invoke(None, Build()))
    assert outcome.failed
    (finding,) = outcome.findings
    assert finding.id == "build.command_failed" and finding.blocking
    assert "make build exited 2" in finding.message
    assert "line 29" in finding.message and "line 5\n" not in finding.message


def test_command_run_returns_what_the_command_produced_where_it_was_asked_to_run() -> None:
    sandbox = _FakeSandbox(0, stdout="hello\n", stderr="warn\n")
    outcome = asyncio.run(
        CommandRun(["make", "run"], cwd="/bound", sandbox=sandbox).invoke(
            None, Run(command=("--once",), cwd="/asked")
        )
    )
    assert sandbox.commands == [["make", "run", "--once"]]
    assert sandbox.cwds == ["/asked"], "the request's cwd wins over the bound one"
    assert outcome.succeeded
    assert outcome.value == RunResult(exit_code=0, stdout="hello\n", stderr="warn\n")


def test_command_run_maps_a_nonzero_exit_to_a_blocking_finding_and_keeps_the_exit_code() -> None:
    sandbox = _FakeSandbox(3, stderr="boom")
    outcome = asyncio.run(CommandRun(["npm", "start"], sandbox=sandbox).invoke(None, Run()))
    assert outcome.failed
    assert outcome.value is not None
    assert outcome.value.exit_code == 3 and outcome.value.stderr == "boom"
    assert outcome.findings[0].id == "run.command_failed" and outcome.findings[0].blocking


class _RecordingSandbox(Sandbox):
    """A real `Sandbox`, so `dataclasses.replace` keeps its fields, that records instead of runs."""

    seen: list[tuple[tuple[str, ...], dict[str, str]]] = []

    async def run(self, command, *, cwd=None, timeout=900.0):  # noqa: ANN001
        type(self).seen.append((tuple(command), dict(self.extra_env)))
        return SandboxResult(exit_code=0, stdout="", stderr="", sandboxed=False, how="recorded")


def test_command_run_carries_the_requests_env_into_the_sandbox_beside_its_own() -> None:
    """The credential drop still holds: the request's variables join the sandbox's allowed set
    for one run, and the bound sandbox is not mutated for it."""
    _RecordingSandbox.seen.clear()
    sandbox = _RecordingSandbox(extra_env={"KEEP": "1"})
    outcome = asyncio.run(
        CommandRun(["make", "run"], sandbox=sandbox).invoke(None, Run(env=(("PORT", "8080"),)))
    )
    assert outcome.succeeded
    assert _RecordingSandbox.seen == [(("make", "run"), {"KEEP": "1", "PORT": "8080"})]
    assert sandbox.extra_env == {"KEEP": "1"}


def test_command_run_refuses_an_env_its_runner_cannot_carry_rather_than_dropping_it() -> None:
    """Run without the variables asked for is a different command from the one the workflow
    asked for, so a runner that cannot carry them gets a refusal and runs nothing."""
    sandbox = _FakeSandbox(0)
    outcome = asyncio.run(
        CommandRun(["make", "run"], sandbox=sandbox).invoke(None, Run(env=(("PORT", "8080"),)))
    )
    assert outcome.failed and outcome.value is None
    assert outcome.findings[0].id == "run.env_unsupported"
    assert sandbox.commands == []


# -- what the review of issue 162 found in the Makefile scan and the output tail ---------------


def test_a_posix_double_colon_assignment_is_not_a_target(tmp_path: Path) -> None:
    """`run::=./app` is an assignment. The old lookahead let it through, so a Makefile with that
    line and no `run` rule bound `make run`, which fails with "No rule to make target"."""
    _write(tmp_path, {"Makefile": "run::=./app\nbuild::=x\ncheck:\n\tdo\n"})
    facts = _detect_facts(tmp_path)
    assert facts.make_targets == ("check",)
    assert facts.run_command == () and facts.build_command == ()


def test_help_text_inside_a_define_block_is_not_a_target(tmp_path: Path) -> None:
    """A `define HELP` block full of `build:   build the image` lines scans like rules."""
    _write(
        tmp_path,
        {
            "Makefile": (
                "define HELP\nUsage: make <target>\nbuild:   build the image\nrun:     run it\nendef\n"
                "check:\n\tdo\n"
            )
        },
    )
    facts = _detect_facts(tmp_path)
    assert facts.make_targets == ("check",)
    assert facts.build_command == () and facts.run_command == ()


def test_space_before_the_colon_and_several_targets_on_one_line_are_read(tmp_path: Path) -> None:
    """Both are legal and common, and both left `build` unbound while `make build` worked."""
    _write(tmp_path, {"Makefile": "build : deps\n\tdo\nrun test: deps\n\tdo\ndeps:\n\tdo\n"})
    facts = _detect_facts(tmp_path)
    assert facts.make_targets == ("build", "run", "test", "deps")
    assert facts.build_command == ("make", "build") and facts.run_command == ("make", "run")


def test_a_failure_tail_keeps_stdout_and_stderr_apart_and_ends_where_the_text_does() -> None:
    """stdout without a trailing newline used to be glued to stderr's first line, showing a line
    nothing printed; and an empty tail used to leave the message ending in a newline."""
    glued = _FakeSandbox(2, stdout="building", stderr="error: x")
    outcome = asyncio.run(CommandBuild(["make", "build"], sandbox=glued).invoke(None, Build()))
    assert outcome.findings[0].message == "make build exited 2\nbuilding\nerror: x"
    assert outcome.value is not None
    assert outcome.value.log == "building\nerror: x"

    silent = _FakeSandbox(2)
    outcome = asyncio.run(CommandBuild(["make", "build"], sandbox=silent).invoke(None, Build()))
    assert outcome.findings[0].message == "make build exited 2"
    assert outcome.value is not None
    assert outcome.value.log == ""


# -- provisioning the repository's own environment (issue 185) ---------------------------------


def test_a_uv_lockfile_binds_a_locked_sync_and_a_project_table_without_one_binds_nothing(
    tmp_path: Path,
) -> None:
    """A lockfile with a frozen install mode: `--locked` refuses to rewrite the lock it installs
    from. A `[project]` pyproject with no uv.lock may be setuptools', PDM's or an uncommitted
    lock's; `uv sync` there is a guess that also writes a uv.lock into the tree, so it binds
    nothing, and a requirements.txt beside it is the layout that binds."""
    _write(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n", "uv.lock": "version = 1\n"})
    assert _detect_facts(tmp_path).provision_commands == (("uv", "sync", "--locked"),)
    (tmp_path / "uv.lock").unlink()
    assert _detect_facts(tmp_path).provision_commands == ()
    _write(tmp_path, {"requirements.txt": "pytest\n"})
    assert _detect_facts(tmp_path).provision_commands[0] == ("python", "-m", "venv", ".venv")


def test_gate_provision_1_every_lockfile_binds_its_own_tools_frozen_install(tmp_path: Path) -> None:
    """A lockfile is as discoverable as the next, and its own tool's frozen install is what each
    guarantees; for as long as only uv's and npm's bound, a Poetry team hand-wrote the line that
    was sitting in their tree (#316). A requirements.txt beside a foreign lock is that tool's
    export, not a second layout, so the lock's tool wins. Whether the tool is installed is `ls`'s
    resolution line and `doctor`'s DOC180, the same answer `uv` and `npm` get."""
    expected = {
        "poetry.lock": ("poetry", "install"),
        "pdm.lock": ("pdm", "sync"),
        "Pipfile.lock": ("pipenv", "sync"),
    }
    for lock, command in expected.items():
        root = tmp_path / lock.split(".")[0]
        _write(root, {"pyproject.toml": "[project]\nname = 'x'\n", lock: "", "requirements.txt": "six\n"})
        assert _detect_facts(root).provision_commands == (command,), lock
    _write(tmp_path / "yarn", {"package.json": "{}", "yarn.lock": ""})
    assert _detect_facts(tmp_path / "yarn").provision_commands == (("yarn", "install", "--frozen-lockfile"),)
    _write(tmp_path / "pnpm", {"package.json": "{}", "pnpm-lock.yaml": ""})
    assert _detect_facts(tmp_path / "pnpm").provision_commands == (("pnpm", "install", "--frozen-lockfile"),)
    # A pyproject with no lock at all still binds nothing: that one is a guess either way.
    _write(tmp_path / "poetry-only", {"pyproject.toml": "[tool.poetry]\nname = 'x'\n"})
    assert _detect_facts(tmp_path / "poetry-only").provision_commands == ()


def test_requirements_txt_provisions_a_venv_of_its_own_and_installs_into_it(tmp_path: Path) -> None:
    """Two steps, and the second names the interpreter the first creates, spelled from the same
    layout the adapters look in (`VENV_BIN`), so what `ls` prints is what runs. A
    requirements-dev.txt rides along, because that is where such a repository keeps pytest."""
    _write(tmp_path, {"requirements.txt": "six\n"})
    venv_python = os.path.join(*VENV_BIN, "python")
    assert _detect_facts(tmp_path).provision_commands == (
        ("python", "-m", "venv", ".venv"),
        (venv_python, "-m", "pip", "install", "-r", "requirements.txt"),
    )
    _write(tmp_path, {"requirements-dev.txt": "pytest\n"})
    assert _detect_facts(tmp_path).provision_commands[1][-2:] == ("-r", "requirements-dev.txt")


def test_gate_tooling_2_a_written_lint_script_is_reused_before_the_configured_linter_is_inferred(
    tmp_path: Path,
) -> None:
    """The principle `test`, `build` and `start` already follow: a written script is a decision.
    A `biome check` script beside an eslint config used to bind `npx eslint .`, and a repository
    with the script and no eslint config was not linted at all (#316)."""
    _write(tmp_path, {"package.json": '{"scripts": {"lint": "biome check"}}', "eslint.config.js": ""})
    assert _detect_facts(tmp_path).lint_command == ("npm", "run", "lint")
    _write(tmp_path / "bare", {"package.json": '{"scripts": {"lint": "standard"}}'})
    assert _detect_facts(tmp_path / "bare").lint_command == ("npm", "run", "lint")
    _write(tmp_path / "eslint", {"package.json": "{}", "eslint.config.js": ""})
    assert _detect_facts(tmp_path / "eslint").lint_command == ("npx", "eslint", ".")


def test_gate_tooling_2_the_decline_names_the_root_and_the_manifests_below_it(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"backend/pyproject.toml": "[project]\n", "web/package.json": "{}", "node_modules/x/go.mod": ""},
    )
    facts = _detect_facts(tmp_path)
    assert facts.below == ("backend/pyproject.toml", "web/package.json")
    assert "at the repository root" in facts.declined()
    assert "found backend/pyproject.toml, web/package.json one level down" in facts.declined()
    assert "at the repository root" not in _detect_facts(tmp_path / "backend").declined()


def test_a_package_lock_binds_npm_ci_and_a_package_json_alone_binds_nothing(tmp_path: Path) -> None:
    """`npm ci` refuses without a lock, and `npm install` beside a credential is unpinned."""
    _write(tmp_path, {"package.json": '{"scripts": {"test": "jest"}}'})
    assert _detect_facts(tmp_path).provision_commands == ()
    _write(tmp_path, {"package-lock.json": "{}"})
    assert _detect_facts(tmp_path).provision_commands == (("npm", "ci"),)


def test_a_python_service_with_a_node_front_end_provisions_both_in_order(tmp_path: Path) -> None:
    _write(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n", "uv.lock": "", "package-lock.json": "{}"})
    assert _detect_facts(tmp_path).provision_commands == (("uv", "sync", "--locked"), ("npm", "ci"))


def test_a_makefile_deps_target_speaks_for_the_repository_and_install_is_never_read(tmp_path: Path) -> None:
    """The repository's own statement wins alone, as `make build` does for Build. `install` is
    not that statement: by GNU convention it copies the built software onto the system."""
    _write(tmp_path, {"Makefile": "deps:\n\tgo mod download\n", "uv.lock": "", "package-lock.json": "{}"})
    assert _detect_facts(tmp_path).provision_commands == (("make", "deps"),)
    _write(tmp_path / "gnu", {"Makefile": "install:\n\tcp app /usr/local/bin\n"})
    assert _detect_facts(tmp_path / "gnu").provision_commands == ()


def test_the_facts_summary_names_the_steps_and_detection_binds_provision_first(tmp_path: Path) -> None:
    """First, because it is where the other bindings' tools come from."""
    _write(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n", "uv.lock": "", "package-lock.json": "{}"})
    facts = _detect_facts(tmp_path)
    assert "provision: uv sync --locked then npm ci" in facts.summary()
    bound = detected_bindings(facts)
    assert bound[0][0] is Provision and isinstance(bound[0][1], CommandProvision)
    assert bound[0][1].steps == facts.provision_commands


# --- Beyond Python and Node: GATE-TOOLING-2 ---------------------------------------------------
#
# O1's first half. A repository that says how it builds itself in `Cargo.toml`, `go.mod`,
# `pom.xml` or `build.gradle` was served only through a Makefile it may not have had, and `ls`
# printed nothing rather than saying what had been looked for.


def test_a_rust_repo_binds_cargo_for_test_and_build(tmp_path: Path) -> None:
    """GATE-TOOLING-2: a Cargo.toml states how the repository tests and builds itself."""
    _write(tmp_path, {"Cargo.toml": '[package]\nname = "x"\n'})
    facts = _detect_facts(tmp_path)
    assert facts.stack == "rust"
    assert facts.test_command == ("cargo", "test")
    assert facts.build_command == ("cargo", "build")
    kinds = {i: type(x) for i, x in detected_bindings(facts)}
    assert kinds[Test] is CommandTest and kinds[Build] is CommandBuild


def test_a_go_repo_binds_go_test_and_go_build(tmp_path: Path) -> None:
    """GATE-TOOLING-2, the same for a go.mod."""
    _write(tmp_path, {"go.mod": "module example.com/x\n"})
    facts = _detect_facts(tmp_path)
    assert facts.stack == "go"
    assert facts.test_command == ("go", "test", "./...")
    assert facts.build_command == ("go", "build", "./...")


def test_a_library_crate_gets_no_cargo_run(tmp_path: Path) -> None:
    """`cargo run` on a crate with no bin target fails at run time — the wrong default that runs.

    The control is the same tree with a `src/main.rs`, which must bind it.
    """
    _write(tmp_path, {"Cargo.toml": '[package]\nname = "x"\n'})
    assert _detect_facts(tmp_path).run_command == ()

    _write(tmp_path, {"src/main.rs": "fn main() {}"})
    assert _detect_facts(tmp_path).run_command == ("cargo", "run")


def test_a_go_module_with_no_main_gets_no_run_command(tmp_path: Path) -> None:
    """A module whose command lives under `cmd/` names it in a path nothing here can know."""
    _write(tmp_path, {"go.mod": "module example.com/x\n", "cmd/serve/main.go": "package main"})
    assert _detect_facts(tmp_path).run_command == ()

    _write(tmp_path, {"main.go": "package main"})
    assert _detect_facts(tmp_path).run_command == ("go", "run", ".")


def test_a_jvm_repo_binds_nothing_until_its_wrapper_is_on_disk(tmp_path: Path) -> None:
    """`mvn` and `gradle` are not guaranteed to be installed; `./mvnw` is the repository's own."""
    _write(tmp_path, {"pom.xml": "<project/>"})
    facts = _detect_facts(tmp_path)
    assert facts.stack == "jvm"
    assert facts.test_command == () and facts.build_command == ()

    _write(tmp_path, {"mvnw": "#!/bin/sh\n"})
    facts = _detect_facts(tmp_path)
    assert facts.test_command == ("./mvnw", "test")
    assert facts.build_command == ("./mvnw", "package")


def test_a_gradle_repo_binds_its_wrapper(tmp_path: Path) -> None:
    _write(tmp_path, {"build.gradle.kts": "", "gradlew": "#!/bin/sh\n"})
    facts = _detect_facts(tmp_path)
    assert facts.stack == "jvm"
    assert facts.test_command == ("./gradlew", "test")
    assert facts.build_command == ("./gradlew", "build")


def test_a_linter_is_bound_only_where_the_repository_configured_one(tmp_path: Path) -> None:
    """The eslint rule, applied to clippy and golangci-lint.

    `cargo clippy` and `go vet` would both run for any valid manifest, so binding them is not a
    guess about whether they work — it is a guess about whether this repository treats them as its
    linter, and a Validate bound to a checker nobody chose reports findings nobody agreed to.
    """
    _write(tmp_path, {"Cargo.toml": '[package]\nname = "x"\n'})
    assert _detect_facts(tmp_path).lint_command == ()
    _write(tmp_path, {"Cargo.toml": '[package]\nname = "x"\n[lints.clippy]\nall = "warn"\n'})
    assert _detect_facts(tmp_path).lint_command == ("cargo", "clippy")

    go_repo = tmp_path / "go"
    go_repo.mkdir()
    _write(go_repo, {"go.mod": "module example.com/x\n"})
    assert _detect_facts(go_repo).lint_command == (), "go vet is not bound without configuration"
    _write(go_repo, {".golangci.yml": ""})
    assert _detect_facts(go_repo).lint_command == ("golangci-lint", "run")


def test_a_written_target_beats_a_command_derived_from_a_manifest(tmp_path: Path) -> None:
    """A `test` target is a decision; `cargo test` is inferred from a file being present."""
    _write(tmp_path, {"Cargo.toml": '[package]\nname = "x"\n', "Makefile": "test:\n\tcargo test --all\n"})
    assert _detect_facts(tmp_path).test_command == ("make", "test")


def test_the_stack_names_every_ecosystem_present_not_the_first(tmp_path: Path) -> None:
    """A Go service with a package.json front end is two true facts."""
    _write(tmp_path, {"go.mod": "module example.com/x\n", "package.json": '{"scripts":{"test":"jest"}}'})
    assert _detect_facts(tmp_path).stack == "node, go"


def test_every_advertised_manifest_is_one_detection_actually_reads(tmp_path: Path) -> None:
    """The decline names files; each has to be load-bearing, or it is advertising a lie.

    This is the direction that rots. `BUILD_MANIFESTS` is prose next to a reader that could stop
    opening one of them and nothing else would notice — the same shape as a gate row citing a gate
    nobody wrote. Each name, alone in a directory, must produce a fact.
    """
    minimal = {
        "pyproject.toml": "[project]\nname = 'x'\n",
        "setup.py": "from setuptools import setup\n",
        "requirements.txt": "click\n",
        "package.json": '{"name":"x"}',
        "Makefile": "test:\n\techo\n",
        "Cargo.toml": '[package]\nname = "x"\n',
        "go.mod": "module example.com/x\n",
        "pom.xml": "<project/>",
        "build.gradle": "",
        "build.gradle.kts": "",
        # GATE-TOOLING-3's seven. A glob entry is written under a real name it matches.
        "Gemfile": "source 'https://rubygems.org'\n",
        "Rakefile": "task :test do\nend\n",
        "composer.json": '{"name":"x/y"}',
        "mix.exs": "defmodule X.MixProject do\nend\n",
        "*.csproj": ("app.csproj", '<Project Sdk="Microsoft.NET.Sdk"/>'),
        "*.sln": ("app.sln", "Microsoft Visual Studio Solution File\n"),
        "CMakeLists.txt": "project(x)\n",
        "Package.swift": "// swift-tools-version:5.10\n",
        "BUILD.bazel": "",
    }
    assert set(minimal) == set(BUILD_MANIFESTS), "a manifest was advertised without a case here"
    for name in BUILD_MANIFESTS:
        root = tmp_path / name.replace(".", "_").replace("/", "_").replace("*", "glob")
        root.mkdir()
        spelled = minimal[name]
        filename, content = spelled if isinstance(spelled, tuple) else (name, spelled)
        _write(root, {filename: content})
        facts = _detect_facts(root)
        assert facts.summary(), f"{name} is advertised by the decline and detection reads nothing from it"


def test_a_repository_detection_cannot_serve_is_told_what_was_looked_for(tmp_path: Path) -> None:
    """GATE-TOOLING-2's second half: declining by name.

    A Dockerfile is the control. It makes `summary()` non-empty without making anything bindable,
    so a decline keyed on "was anything found at all" would go quiet on exactly the tree that
    needs it. (A README does not serve as that control: `summary()` does not report one, which is
    the sort of thing a control is for.)
    """
    _write(tmp_path, {"README.md": "# x\n", "Dockerfile": "FROM scratch\n"})
    facts = _detect_facts(tmp_path)
    assert facts.summary(), "the control is only meaningful if something was found"
    declined = facts.declined()
    for name in BUILD_MANIFESTS:
        assert name in declined, f"the decline does not name {name}"


def test_a_manifest_that_named_no_command_declines_differently(tmp_path: Path) -> None:
    """A pom.xml does state how the repository builds. Saying otherwise sends its owner to fix
    the thing that is not broken, so the decline names the wrapper instead."""
    _write(tmp_path, {"pom.xml": "<project/>"})
    declined = _detect_facts(tmp_path).declined()
    assert "jvm is here" in declined and "./mvnw" in declined
    assert "nothing here states how this repository builds" not in declined


def test_a_servable_repository_declines_nothing(tmp_path: Path) -> None:
    """The other side of the ratchet: a decline that fired on a bound repository would be noise."""
    _write(tmp_path, {"Cargo.toml": '[package]\nname = "x"\n'})
    assert _detect_facts(tmp_path).declined() == ""


# --- The seven the row named: GATE-TOOLING-3 ---------------------------------------------------


def test_gate_tooling_3_mix_exs_binds_mix_test_and_provisions_only_from_a_lock(tmp_path: Path) -> None:
    """GATE-TOOLING-3: `mix test` is guaranteed by the toolchain for any project; `mix deps.get`
    only where a `mix.lock` says what to fetch."""
    _write(tmp_path, {"mix.exs": "defmodule X.MixProject do\nend\n"})
    facts = _detect_facts(tmp_path)
    assert facts.stack == "elixir" and facts.test_command == ("mix", "test")
    assert facts.provision_commands == ()
    _write(tmp_path, {"mix.lock": "%{}\n"})
    assert _detect_facts(tmp_path).provision_commands == (("mix", "deps.get"),)


def test_gate_tooling_3_a_dotnet_project_or_solution_binds_dotnet_test_build_and_restore(
    tmp_path: Path,
) -> None:
    """GATE-TOOLING-3: the SDK guarantees all three, and the project file carries the project's
    own name, so the manifest is a glob and either spelling is read."""
    _write(tmp_path, {"Widgets.csproj": '<Project Sdk="Microsoft.NET.Sdk"/>'})
    facts = _detect_facts(tmp_path)
    assert facts.stack == "dotnet"
    assert facts.test_command == ("dotnet", "test") and facts.build_command == ("dotnet", "build")
    assert facts.provision_commands == (("dotnet", "restore"),)
    solution = tmp_path / "sln"
    solution.mkdir()
    _write(solution, {"Widgets.sln": "Microsoft Visual Studio Solution File\n"})
    assert _detect_facts(solution).test_command == ("dotnet", "test")


def test_gate_tooling_3_package_swift_binds_swift_test_and_build_and_resolves_only_from_a_lock(
    tmp_path: Path,
) -> None:
    """GATE-TOOLING-3."""
    _write(tmp_path, {"Package.swift": "// swift-tools-version:5.10\n"})
    facts = _detect_facts(tmp_path)
    assert facts.stack == "swift"
    assert facts.test_command == ("swift", "test") and facts.build_command == ("swift", "build")
    assert facts.provision_commands == ()
    _write(tmp_path, {"Package.resolved": "{}"})
    assert _detect_facts(tmp_path).provision_commands == (("swift", "package", "resolve"),)


def test_gate_tooling_3_bazel_binds_only_inside_a_workspace(tmp_path: Path) -> None:
    """GATE-TOOLING-3: a BUILD file outside a workspace is not a Bazel repository, and `bazel`
    there fails at run time -- the wrong default that runs. The decline names the missing file."""
    _write(tmp_path, {"BUILD.bazel": ""})
    facts = _detect_facts(tmp_path)
    assert facts.stack == "bazel" and facts.test_command == () and facts.build_command == ()
    assert "MODULE.bazel" in facts.declined()
    _write(tmp_path, {"MODULE.bazel": 'module(name = "x")\n'})
    facts = _detect_facts(tmp_path)
    assert facts.test_command == ("bazel", "test", "//...") and facts.build_command == (
        "bazel",
        "build",
        "//...",
    )
    assert facts.provision_commands == (), "Bazel fetches its own"


def test_gate_tooling_3_composer_test_binds_only_from_a_written_script(tmp_path: Path) -> None:
    """GATE-TOOLING-3: a written script is a decision, the rule package.json already follows.
    `composer install` only from a `composer.lock`."""
    _write(tmp_path, {"composer.json": '{"name":"x/y"}'})
    facts = _detect_facts(tmp_path)
    assert facts.stack == "php" and facts.test_command == ()
    assert "composer test" in facts.declined()
    _write(tmp_path, {"composer.json": '{"name":"x/y","scripts":{"test":"phpunit"}}', "composer.lock": "{}"})
    facts = _detect_facts(tmp_path)
    assert facts.test_command == ("composer", "test")
    assert facts.provision_commands == (("composer", "install"),)


def test_gate_tooling_3_rake_test_binds_only_from_a_rakefile_that_defines_the_task(tmp_path: Path) -> None:
    """GATE-TOOLING-3: `bundle exec rake test` out of a Rakefile that defines `test` -- `task
    :test`, `task "test"`, or a `Rake::TestTask` -- and nothing out of one that does not. A
    Rakefile with no Gemfile is a ruby fact and no `bundle`."""
    _write(tmp_path, {"Gemfile": "source 'https://rubygems.org'\n", "Rakefile": "task :build do\nend\n"})
    facts = _detect_facts(tmp_path)
    assert facts.stack == "ruby" and facts.test_command == ()
    assert "Rake::TestTask" in facts.declined()
    for spelling in ("task :test do\nend\n", 'task "test" do\nend\n', "Rake::TestTask.new do |t|\nend\n"):
        _write(tmp_path, {"Rakefile": spelling})
        assert _detect_facts(tmp_path).test_command == ("bundle", "exec", "rake", "test"), spelling
    # `task :tests` and a comment are not the task.
    _write(tmp_path, {"Rakefile": "# task :test\ntask :tests do\nend\n"})
    assert _detect_facts(tmp_path).test_command == ()
    _write(tmp_path, {"Rakefile": "task :test do\nend\n", "Gemfile.lock": "GEM\n"})
    assert _detect_facts(tmp_path).provision_commands == (("bundle", "install"),)
    alone = tmp_path / "alone"
    alone.mkdir()
    _write(alone, {"Rakefile": "task :test do\nend\n"})
    assert _detect_facts(alone).stack == "ruby" and _detect_facts(alone).test_command == ()


def test_gate_tooling_3_cmake_binds_ctest_only_where_testing_was_enabled(tmp_path: Path) -> None:
    """GATE-TOOLING-3: `ctest` in a project that never called `enable_testing()` finds nothing and
    exits 8. The build half binds regardless; the configure step is never invented."""
    _write(tmp_path, {"CMakeLists.txt": "project(x)\n"})
    facts = _detect_facts(tmp_path)
    assert facts.stack == "cpp" and facts.test_command == ()
    assert facts.build_command == ("cmake", "--build", "build")
    assert "enable_testing()" in facts.declined() or facts.declined() == ""
    _write(tmp_path, {"CMakeLists.txt": "project(x)\nenable_testing()\nadd_test(NAME t COMMAND t)\n"})
    assert _detect_facts(tmp_path).test_command == ("ctest", "--test-dir", "build")


def test_gate_tooling_3_a_written_target_still_beats_every_derived_command(tmp_path: Path) -> None:
    """The precedence #237 stated holds for the seven: a `test` target is a decision."""
    _write(tmp_path, {"mix.exs": "", "Makefile": "test:\n\tmix test --trace\n"})
    assert _detect_facts(tmp_path).test_command == ("make", "test")


def test_gate_tooling_3_a_manifest_one_level_down_is_named_under_either_spelling(tmp_path: Path) -> None:
    """The decline's one-level-down scan reads the glob entries the way the root does."""
    _write(tmp_path, {"README.md": "# x\n", "api/Widgets.csproj": "<Project/>", "web/Gemfile": ""})
    declined = _detect_facts(tmp_path).declined()
    assert "api/*.csproj" in declined and "web/Gemfile" in declined
