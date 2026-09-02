"""Drop-in detection: read the tree, fit the defaults to it.

The failure this closes is a Node repository silently getting pytest bound, so the tests are
mostly 'a repo shaped like X detects Y and binds Z', plus the JUnit ingestion that lets a
non-pytest runner report which test failed rather than only an exit code.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from in_lockstep.adapters import (
    Build,
    CommandBuild,
    CommandRun,
    CommandTest,
    CommandValidate,
    Run,
    detected_bindings,
    parse_junit,
)
from in_lockstep.adapters.command import Test, Validate
from in_lockstep.adapters.pytest_adapter import PytestTest
from in_lockstep.adapters.ruff_adapter import RuffValidate
from in_lockstep.adapters.sandbox import Sandbox, SandboxResult
from in_lockstep.core.context import MAKE_TARGETS_SHOWN
from in_lockstep.core.types import RunResult
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
    assert outcome.value.artifacts == (), "what a build produced is not guessed from the tree"
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
    assert outcome.value.log == "building\nerror: x"

    silent = _FakeSandbox(2)
    outcome = asyncio.run(CommandBuild(["make", "build"], sandbox=silent).invoke(None, Build()))
    assert outcome.findings[0].message == "make build exited 2"
    assert outcome.value.log == ""
