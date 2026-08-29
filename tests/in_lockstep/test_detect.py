"""Drop-in detection: read the tree, fit the defaults to it.

The failure this closes is a Node repository silently getting pytest bound, so the tests are
mostly 'a repo shaped like X detects Y and binds Z', plus the JUnit ingestion that lets a
non-pytest runner report which test failed rather than only an exit code.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from in_lockstep.adapters import CommandTest, CommandValidate, detected_bindings, parse_junit
from in_lockstep.adapters.command import Test, Validate
from in_lockstep.adapters.pytest_adapter import PytestTest
from in_lockstep.adapters.ruff_adapter import RuffValidate
from in_lockstep.core.types import TestSpec, ValidateSpec
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

    async def run(self, command, *, cwd=None, timeout=900.0):  # noqa: ANN001
        self.commands.append(command)
        return self._result


def test_command_test_maps_a_clean_exit_to_success() -> None:
    adapter = CommandTest(["npm", "test"], sandbox=_FakeSandbox(0))
    outcome = asyncio.run(adapter.invoke(None, TestSpec()))
    assert outcome.status.value == "succeeded"


def test_command_test_maps_a_nonzero_exit_to_failure() -> None:
    adapter = CommandTest(["npm", "test"], sandbox=_FakeSandbox(1, stdout="1 failed"))
    outcome = asyncio.run(adapter.invoke(None, TestSpec()))
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
    outcome = asyncio.run(adapter.invoke(ctx, TestSpec()))
    report = outcome.value
    assert report.total == 3 and report.failed == 1 and report.skipped == 1
    assert {c.id for c in report.cases} == {"a::ok", "a::bad", "a::skip"}
    assert any(c.outcome == "failed" and c.message == "boom" for c in report.cases)


def test_command_test_with_a_junit_report_that_collected_nothing_decides_nothing(tmp_path: Path) -> None:
    (tmp_path / "report.xml").write_text("<testsuite></testsuite>")
    adapter = CommandTest(["pytest"], sandbox=_FakeSandbox(0), junit="report.xml")
    ctx = type("C", (), {"repo": type("R", (), {"root": str(tmp_path)})})()
    outcome = asyncio.run(adapter.invoke(ctx, TestSpec()))
    assert not outcome.decided, "a runner that collected nothing decided nothing"
    assert any(f.id == "test.no_tests_collected" for f in outcome.findings)


def test_a_red_run_is_decided_even_when_the_junit_report_is_missing(tmp_path: Path) -> None:
    """A configured report the runner never wrote (path mismatch, or a crash) must not turn a real
    red run into 'decided nothing' — a nonzero exit is a verdict."""
    adapter = CommandTest(["npm", "test"], sandbox=_FakeSandbox(1), junit="nowhere.xml")
    ctx = type("C", (), {"repo": type("R", (), {"root": str(tmp_path)})})()
    outcome = asyncio.run(adapter.invoke(ctx, TestSpec()))
    assert outcome.status.value == "failed"
    assert outcome.decided, "a red run is decided regardless of the report"


def test_a_clean_exit_with_a_junit_report_that_ran_nothing_decides_nothing(tmp_path: Path) -> None:
    (tmp_path / "r.xml").write_text("<testsuite></testsuite>")
    adapter = CommandTest(["x"], sandbox=_FakeSandbox(0), junit="r.xml")
    ctx = type("C", (), {"repo": type("R", (), {"root": str(tmp_path)})})()
    outcome = asyncio.run(adapter.invoke(ctx, TestSpec()))
    assert outcome.status.value == "succeeded" and not outcome.decided


def test_the_selector_is_passed_when_the_runner_knows_how(tmp_path: Path) -> None:
    """`TestSpec.selector` narrows to one test only if the adapter is told the runner's flag; the
    seam is real so a reproducer is not silently widened to the whole suite."""
    sandbox = _FakeSandbox(0)
    adapter = CommandTest(["jest"], sandbox=sandbox, selector_arg=("--testNamePattern",))
    asyncio.run(adapter.invoke(None, TestSpec(selector="login")))
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
    assert asyncio.run(green.invoke(None, TestSpec(expect="fail"))).status.value == "failed"
    red = CommandTest(["x"], sandbox=_FakeSandbox(1))
    assert asyncio.run(red.invoke(None, TestSpec(expect="fail"))).status.value == "succeeded"


def test_command_validate_maps_exit_code_and_surfaces_output() -> None:
    clean = CommandValidate(["eslint"], sandbox=_FakeSandbox(0))
    assert asyncio.run(clean.invoke(None, ValidateSpec())).status.value == "succeeded"
    dirty = CommandValidate(["eslint"], sandbox=_FakeSandbox(2, stdout="3 problems"))
    outcome = asyncio.run(dirty.invoke(None, ValidateSpec()))
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
