"""The red and green verdicts are the framework's, not a script's claim (GATE-VERDICT-1).

O7 says the model is invoked only where a judgment is required, and the tdd strategy calls the
deterministic Test run between its two model steps "the whole argument of this framework". For as
long as `PytestTest` read a missing summary as zeros, that run could be a staged module calling
`os._exit(0)` at import: a green suite of zero tests, `decided=True`, and a change marked ready
for review on the strength of it (#313). What is held here: a run that reported nothing decides
nothing, a crash with no summary is `errored` and not red, a suite that ran something still
decides, and the model cannot hand pytest an option dressed as a path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from in_lockstep.adapters.pytest_adapter import PytestTest
from in_lockstep.core.outcome import Status
from in_lockstep.core.types import Test, TestReport, TestVerdict


class _Sandbox:
    def __init__(self, exit_code: int, stdout: str = "") -> None:
        self._result = type("R", (), {"exit_code": exit_code, "stdout": stdout, "stderr": ""})()
        self.commands: list[list[str]] = []

    async def run(self, command, *, cwd=None, timeout=900.0):  # noqa: ANN001
        self.commands.append(command)
        return self._result


def _run(exit_code: int, stdout: str, *, expect: str = "pass") -> Any:
    adapter = PytestTest(args=["-q"], cwd=".", sandbox=_Sandbox(exit_code, stdout))
    return asyncio.run(adapter.invoke(None, Test(expect=expect)))


def _verdict(outcome: Any) -> TestVerdict:
    return TestVerdict.of(outcome.status.value, outcome.decided, outcome.value or TestReport())


# -- a run that reported nothing decides nothing -------------------------------------------------


def test_gate_verdict_1_a_clean_exit_with_no_summary_decides_nothing_and_is_not_green() -> None:
    """`os._exit(0)` at import, or a summary that never reached stdout: not a pass."""
    outcome = _run(0, "")
    assert outcome.status is Status.SUCCEEDED and not outcome.decided
    assert [f.id for f in outcome.findings] == ["test.no_summary"]
    assert not _verdict(outcome).green, "the writing verbs turn on this, and it must not be green"
    assert not _verdict(outcome).red


def test_gate_verdict_1_a_failing_exit_with_no_summary_is_errored_not_red() -> None:
    """`os._exit(1)` at import used to satisfy `expect="fail"`: exit 1 with zero counts read as red.
    A runner that never reported is a broken run, and not evidence about the change."""
    outcome = _run(1, "", expect="fail")
    assert outcome.status is Status.ERRORED and outcome.reason == "test.no_summary"
    assert not _verdict(outcome).red and not _verdict(outcome).green


def test_gate_verdict_1_a_summary_in_which_nothing_ran_decides_nothing() -> None:
    """Every test skipped or deselected is the exit-5 case with a different exit code."""
    outcome = _run(0, "3 skipped, 2 deselected in 0.10s")
    assert outcome.status is Status.SUCCEEDED and not outcome.decided
    assert outcome.value.skipped == 3
    assert [f.id for f in outcome.findings] == ["test.nothing_ran"]
    assert not _verdict(outcome).green


def test_gate_verdict_1_a_suite_that_ran_and_passed_still_decides_green() -> None:
    """The positive control, or the three above would also hold for an adapter that never decides."""
    outcome = _run(0, "3 passed in 0.12s")
    assert outcome.status is Status.SUCCEEDED and outcome.decided
    assert _verdict(outcome).green and outcome.value.passed == 3


def test_gate_verdict_1_a_suite_that_ran_and_failed_still_decides_red() -> None:
    outcome = _run(1, "2 passed, 1 failed in 0.12s")
    assert outcome.status is Status.FAILED and outcome.decided
    assert _verdict(outcome).red and outcome.value.failed == 1
    assert _run(1, "2 passed, 1 failed in 0.12s", expect="fail").status is Status.SUCCEEDED


def test_gate_verdict_1_a_verdict_read_back_with_zero_counts_is_not_green_on_its_status_alone() -> None:
    """The second lock: `implement/propose` reads the verdict from an artifact and never held the
    Outcome, so `green` refuses zeros itself rather than trusting the writer decided honestly."""
    assert not TestVerdict(status="succeeded", decided=True).green
    assert TestVerdict(status="succeeded", decided=True, total=1, passed=1).green


def test_gate_verdict_1_a_staged_module_that_exits_at_import_is_not_a_green_suite(tmp_path: Path) -> None:
    """The real pytest, on the exact shape the audit traced: the module exits before pytest can
    print a line, and the run is undecided rather than a pass of zero tests."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "test_exit.py").write_text("import os\n\nos._exit(0)\n")
    outcome = asyncio.run(PytestTest(args=["-q"], cwd=".").invoke(None, Test(root=str(root))))
    assert outcome.status is Status.SUCCEEDED and not outcome.decided, outcome
    assert not _verdict(outcome).green


# -- the model cannot hand pytest an option ------------------------------------------------------


def test_gate_verdict_1_run_tests_refuses_a_path_that_looks_like_an_option(tmp_path: Path) -> None:
    from in_lockstep.ai.builtins import Workspace, read_write_execute
    from in_lockstep.ai.tools import BUILTIN_SERVER
    from in_lockstep.core.changes import ChangeGuard

    seen: list[tuple[str, ...]] = []

    async def tests(paths: tuple[str, ...] = ()) -> str:
        seen.append(paths)
        return "ok"

    _tools, runner = read_write_execute(Workspace(root=tmp_path, guard=ChangeGuard()), tests=tests)
    answer = asyncio.run(runner(BUILTIN_SERVER, "run_tests", {"paths": ["tests/a.py", "-p", "evil"]}))
    assert answer.startswith("refused: '-p' looks like a pytest option"), answer
    assert seen == [], "the runner was never reached, so the option never reached pytest's argv"
    assert asyncio.run(runner(BUILTIN_SERVER, "run_tests", {"paths": ["tests/a.py"]})) == "ok"
