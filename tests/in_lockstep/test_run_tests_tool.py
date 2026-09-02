"""Every code-writing verb can run the suite against its own staged change.

Before this, a model wrote an implementation, stopped, and learned whether it worked only after the
run was over and paid for. Run 33582850420 is what that costs: `tdd.not_green`, 13 failing tests of
1644, $13.88 — for a diff a single test run would have rejected.

`run_script` could not close the gap. `WorktreeRunner` runs it against HEAD rather than the staged
change, deliberately — "its documented job is to tell the model what the existing behaviour is" —
in a container with `--network=none` and no project dependencies. Reaching for the network there
would reopen the exfiltration channel that flag closes, so the answer is not to loosen it.

The answer is that the framework ALREADY runs the suite over a staged change (`verdict_over_staged`)
and simply never offered it to the model.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from in_lockstep.ai.builtins import DEFAULT_TEST_RUNS, Workspace, read_write_execute
from in_lockstep.core.changes import ChangeGuard


def _runner(tmp_path: Path, tests: Any = None, **over: Any):
    workspace = Workspace(root=tmp_path, guard=ChangeGuard())
    _tools, runner = read_write_execute(workspace, tests=tests, **over)
    return runner


def _call(runner: Any, **args: Any) -> str:
    from in_lockstep.ai.tools import BUILTIN_SERVER

    return asyncio.run(runner(BUILTIN_SERVER, "run_tests", args))


# -- the tool exists and is declared where every verb sees it -----------------------------------


def test_the_tool_is_declared_beside_run_script(tmp_path: Path) -> None:
    workspace = Workspace(root=tmp_path, guard=ChangeGuard())
    tools, _runner = read_write_execute(workspace)
    names = {t.name for t in tools.definitions()}
    assert {"run_tests", "run_script", "write_file"} <= names


def test_both_writing_verbs_get_it_without_naming_it() -> None:
    """The point of putting it in `read_write_execute`, which has one caller — `AiStrategy._session`.
    Implement and fix reach it through the same base, so neither opts in and neither can forget to.
    Adding it per strategy would be the hand-copied-frozenset mistake `AGENCY` already documents."""
    from in_lockstep.adapters.ai.fix import DiagnoseThenFix
    from in_lockstep.adapters.ai.oneshot import Oneshot
    from in_lockstep.adapters.ai.tdd import TDD

    for strategy in (Oneshot, TDD, DiagnoseThenFix):
        session = strategy(lambda ctx: None)._session(object())
        assert "run_tests" in {t.name for t in session.tools.definitions()}, strategy.__name__


def test_a_verb_that_only_subclasses_the_base_still_gets_it() -> None:
    """ "By construction" has to mean a strategy somebody writes tomorrow, not the three shipped
    today. A new subclass that declares nothing but the required agency inherits the tool."""
    from in_lockstep.adapters.ai.implement import ImplementStrategy

    class Bare(ImplementStrategy):
        id = "implement/bare"

    session = Bare(lambda ctx: None)._session(object())
    assert "run_tests" in {t.name for t in session.tools.definitions()}


def test_the_tool_declares_no_capability_the_set_did_not_already_hold(tmp_path: Path) -> None:
    """`EXECUTES_CODE` is already declared by `read_write_execute`, and that declaration is what
    egress enforcement, `ApprovalGate` and `Retry` all key on. Adding a tool must not widen it —
    a set that grew a capability here would change what three controls do, quietly."""
    from in_lockstep.ai.builtins import read_write
    from in_lockstep.core.verbs import Capability

    workspace = Workspace(root=tmp_path, guard=ChangeGuard())
    full, _ = read_write_execute(workspace)
    writing_only, _ = read_write(workspace)

    # The executing set declares exactly one capability the writing set does not, and `run_tests`
    # did not add it — `run_script` already had it, which is why this tool is free to exist here.
    assert full.capabilities() - writing_only.capabilities() == {Capability.EXECUTES_CODE}


# -- refusing rather than pretending ------------------------------------------------------------


def test_with_no_runner_it_refuses_and_says_what_to_bind(tmp_path: Path) -> None:
    """The same shape `run_script` has with no `CommandRunner`: a tool that cannot work says so,
    rather than returning something a model would read as a pass."""
    out = _call(_runner(tmp_path))
    assert out.startswith("refused:")
    assert "Test" in out and "bind" in out.lower()


def test_the_call_cap_is_a_refusal_that_names_the_number(tmp_path: Path) -> None:
    """A model that runs the suite after every edit exhausts its turns before it finishes. The
    refusal says the count, because a model retrying a call that can never work burns exactly the
    turns it needed to finish."""
    calls: list[tuple[str, ...]] = []

    async def tests(paths: tuple[str, ...] = ()) -> str:
        calls.append(paths)
        return "3 passed, 0 failed, 0 skipped of 3"

    runner = _runner(tmp_path, tests=tests, max_test_runs=2)
    assert not _call(runner).startswith("refused:")
    assert not _call(runner).startswith("refused:")

    third = _call(runner)
    assert third.startswith("refused:")
    assert "2 time(s)" in third
    assert len(calls) == 2, "the capped call never reached the runner"


def test_the_cap_comes_from_the_policy(tmp_path: Path) -> None:
    """Declared beside `max_turns`, so a repository that wants a model to iterate harder raises it
    in the same place it raises the turn cap."""
    from in_lockstep.ai.invoker import InvokePolicy

    assert InvokePolicy().max_test_runs == DEFAULT_TEST_RUNS

    from in_lockstep.adapters.ai.oneshot import Oneshot

    strategy = Oneshot(lambda ctx: None, policy=InvokePolicy(max_test_runs=9))
    assert strategy._session(object()).run_tool.max_test_runs == 9


def test_a_runner_that_raises_becomes_a_message_not_a_crash(tmp_path: Path) -> None:
    """A tool result is something a model reacts to. Raising here would end the session with the
    staged work unreported, which is the loss this tool exists to prevent."""

    async def tests(paths: tuple[str, ...] = ()) -> str:
        raise RuntimeError("docker is not running")

    out = _call(_runner(tmp_path, tests=tests))
    assert out.startswith("error:") and "docker is not running" in out


def test_paths_reach_the_runner(tmp_path: Path) -> None:
    """A full suite here is about a hundred seconds, so an unnarrowed loop spends its budget
    waiting rather than working."""
    seen: list[tuple[str, ...]] = []

    async def tests(paths: tuple[str, ...] = ()) -> str:
        seen.append(paths)
        return "ok"

    _call(_runner(tmp_path, tests=tests), paths=["tests/a.py", "  ", "tests/b.py"])
    assert seen == [("tests/a.py", "tests/b.py")], "blank entries are dropped, order kept"


# -- what the model is told ---------------------------------------------------------------------


class _Report:
    def __init__(self, total: int, passed: int = 0, failed: int = 0, cases: tuple[Any, ...] = ()) -> None:
        self.total, self.passed, self.failed, self.skipped, self.cases = total, passed, failed, 0, cases


class _Case:
    def __init__(self, id: str, outcome: str) -> None:
        self.id, self.outcome = id, outcome


def _render(outcome: Any) -> str:
    from in_lockstep.adapters.ai.strategy import _rendered

    return _rendered(outcome)


class _Outcome:
    def __init__(self, value: Any, reason: str | None = None) -> None:
        self.value, self.reason = value, reason


def test_a_suite_that_collected_nothing_is_not_reported_as_passing() -> None:
    """The distinction that has already cost this repository two runs. A green suite that ran none
    of the new tests looks exactly like a green suite that ran them, and a tool blurring it here
    would hand the model the same lie in a friendlier format."""
    out = _render(_Outcome(_Report(total=0)))
    assert "NOTHING WAS COLLECTED" in out
    assert "not a pass" in out
    assert "passed" not in out.split("This is")[0].lower() or "0 passed" not in out


def test_failures_are_named_and_passes_are_not() -> None:
    """The failing tests are the entire reason to have run this. A result truncated by
    `max_tool_result_chars` must not lose them to a list of 1,600 passes."""
    cases = (_Case("tests/x.py::test_a", "failed"), _Case("tests/x.py::test_b", "passed"))
    out = _render(_Outcome(_Report(total=2, passed=1, failed=1, cases=cases)))
    assert "test_a" in out
    assert "test_b" not in out


def test_a_green_run_says_only_that_everything_which_ran_passed() -> None:
    out = _render(_Outcome(_Report(total=3, passed=3)))
    assert "3 passed" in out and "Everything that ran, passed" in out


def test_an_outcome_with_no_report_says_so_rather_than_inventing_one() -> None:
    out = _render(_Outcome(None, reason="test.refused"))
    assert "did not produce a report" in out and "test.refused" in out


def test_nothing_staged_is_a_refusal_rather_than_a_pointless_run(tmp_path: Path) -> None:
    """Running the suite over an empty change set tests the code exactly as it already is, which
    tells the model nothing and costs a hundred seconds to say."""
    from in_lockstep.adapters.ai.oneshot import Oneshot

    class _Ctx:
        container = None

    session = Oneshot(lambda ctx: None, repo_root=str(tmp_path))._session(_Ctx())
    out = asyncio.run(session.run_tool.tests(()))
    assert out.startswith("refused:")


@pytest.mark.parametrize("name", ["run_tests"])
def test_the_description_warns_that_a_subset_is_not_a_verdict(tmp_path: Path, name: str) -> None:
    """The one dangerous affordance here: a model can run a passing subset and talk itself into
    finishing. The framework's own full run still decides, and the tool says so."""
    workspace = Workspace(root=tmp_path, guard=ChangeGuard())
    tools, _ = read_write_execute(workspace)
    (tool,) = [t for t in tools.definitions() if t.name == name]
    assert "decides" in tool.description
    assert "staged" in tool.description
