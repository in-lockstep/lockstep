"""GATE-GUARD-1 — a write to a protected path is refused on all three enforcement points.

The three are named in the gate because they are reached differently and can disagree: the in-loop
tool boundary is where a model asks, `--apply-inline` is where a laptop writes, and
`apply --from-artifact` is where the privileged job writes something a different job produced.
Two of them did not exist until the builtin tool runner did — the invoker accepted `tools` and
`run_tool` and every shipped verb passed neither, so the "boundary" was a place rather than a
thing.

The tests below drive one list of Tier-1 paths through all three, so a rule that holds in one and
not another cannot pass.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from in_lockstep.ai.builtins import (
    ToolRunnerImpl,
    Workspace,
    read_only,
    read_write,
    read_write_execute,
)
from in_lockstep.cli import main
from in_lockstep.core.verbs import Capability
from in_lockstep.llm.interface import LLMProvider
from in_lockstep.llm.types import LLMInput, LLMOutput, ToolCall

# One list, three enforcement points. A path protected in one place and not another is the failure
# mode a single shared list is here to make impossible.
TIER_1 = [
    "lockstep.py",
    ".in-lockstep/ledger/x.json",
    ".github/workflows/ci.yml",
    ".git/hooks/pre-commit",
    "pyproject.toml",
    "conftest.py",
    "CODEOWNERS",
    ".env",
]


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(root=tmp_path)


# -- 1. the in-loop tool boundary --------------------------------------------------------------


@pytest.mark.parametrize("path", TIER_1)
def test_the_tool_boundary_refuses_a_protected_write(workspace: Workspace, path: str) -> None:
    _, run = read_write(workspace)
    result = asyncio.run(run("builtin", "write_file", {"path": path, "contents": "x"}))
    assert result.startswith("refused:"), result
    assert workspace.changes == [], "a refused write must stage nothing"


@pytest.mark.parametrize("path", TIER_1)
def test_the_tool_boundary_refuses_a_protected_deletion(workspace: Workspace, path: str) -> None:
    _, run = read_write(workspace)
    result = asyncio.run(run("builtin", "delete_file", {"path": path}))
    assert result.startswith("refused:"), result


def test_a_refusal_is_a_tool_result_not_an_exception(workspace: Workspace) -> None:
    """The model asked for something it may not have. Telling it so lets it choose again.

    Raising would end the run and spend the turns already paid for, and a refusal is information —
    it says which rule and which tier, so the next turn can be different.
    """
    _, run = read_write(workspace)
    result = asyncio.run(run("builtin", "write_file", {"path": "lockstep.py", "contents": "x"}))
    assert "tier 1" in result and "rule" in result


def test_an_ordinary_write_is_staged_not_written(workspace: Workspace) -> None:
    """A write does not touch the disk, so an interrupted loop leaves no half-written tree."""
    _, run = read_write(workspace)
    result = asyncio.run(run("builtin", "write_file", {"path": "src/app.py", "contents": "x = 1"}))
    assert result.startswith("ok:")
    assert not (workspace.root / "src" / "app.py").exists()
    assert workspace.changeset().changes[0].path == "src/app.py"


def test_correcting_a_write_replaces_it(workspace: Workspace) -> None:
    """Last write wins, so `apply` is never left guessing which of two versions was meant."""
    _, run = read_write(workspace)
    asyncio.run(run("builtin", "write_file", {"path": "a.py", "contents": "first"}))
    asyncio.run(run("builtin", "write_file", {"path": "a.py", "contents": "second"}))
    changes = workspace.changeset().changes
    assert len(changes) == 1
    assert changes[0].contents == "second"


def test_a_read_only_set_offers_no_writer(workspace: Workspace) -> None:
    tools, _ = read_only(workspace)
    assert tools.names() == ["list_files", "read_file", "search_text"]
    # The name of this test is the property; the list above is the membership. Asserting the
    # capability separately is what keeps a tool added to this set from quietly bringing a
    # dangerous declaration with it.
    assert tools.read_only
    assert Capability.WRITES_FILES not in tools.capabilities()


def test_a_read_write_set_declares_that_it_writes(workspace: Workspace) -> None:
    """The declaration is what makes egress mandatory and the approval gate apply."""
    tools, _ = read_write(workspace)
    assert Capability.WRITES_FILES in tools.capabilities()


def test_reading_outside_the_repository_is_refused(workspace: Workspace) -> None:
    """A model that can read `../../.ssh/id_rsa` has exfiltrated it into the transcript."""
    _, run = read_only(workspace)
    assert asyncio.run(run("builtin", "read_file", {"path": "../outside"})).startswith("refused:")


def test_reading_a_real_file_works(workspace: Workspace) -> None:
    (workspace.root / "notes.txt").write_text("hello")
    _, run = read_only(workspace)
    assert asyncio.run(run("builtin", "read_file", {"path": "notes.txt"})) == "hello"


def test_a_huge_read_is_truncated_and_says_how_to_read_the_rest(workspace: Workspace) -> None:
    """A tool result is model input; an unbounded one is an unbounded prompt next turn.

    And a truncation that does not say how to continue is what a model works around rather than
    uses: run 34361896059 spent turns trying to reach line 800 of a file it was editing (#402).
    """
    from in_lockstep.ai.builtins import MAX_READ_CHARS

    (workspace.root / "big.txt").write_text("x\n" * MAX_READ_CHARS)
    _, run = read_only(workspace)
    answer = asyncio.run(run("builtin", "read_file", {"path": "big.txt"}))
    assert "truncated" in answer
    assert f"has {MAX_READ_CHARS} line(s)" in answer, "the size it could not show"
    assert "`offset`" in answer and "`limit`" in answer, "and how to ask for the rest"
    # Inside the cap, not on top of it. Appending the sentence past `MAX_READ_CHARS` is what put it
    # where the invoker's own bound removed it, and a model was then told a file was cut with no
    # way stated to reach the rest (#412). `test_read_delivery.py` asserts it survives delivery.
    assert len(answer) <= MAX_READ_CHARS


def test_listing_matches_a_glob(workspace: Workspace) -> None:
    (workspace.root / "a.py").write_text("")
    (workspace.root / "b.txt").write_text("")
    _, run = read_only(workspace)
    assert asyncio.run(run("builtin", "list_files", {"glob": "*.py"})) == "a.py"


def test_a_missing_file_is_an_error_not_a_crash(workspace: Workspace) -> None:
    _, run = read_only(workspace)
    assert asyncio.run(run("builtin", "read_file", {"path": "nope.py"})).startswith("error:")


# -- 2 and 3. the two apply paths --------------------------------------------------------------


def _artifact(tmp_path: Path, path: str) -> Path:
    payload = tmp_path / "changeset.json"
    payload.write_text(json.dumps({"changes": [{"path": path, "contents": "x", "author": "agent"}]}))
    return payload


@pytest.mark.parametrize("path", TIER_1)
def test_apply_inline_refuses_a_protected_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    result = CliRunner().invoke(main, ["apply-inline", "--from-artifact", str(_artifact(tmp_path, path))])
    assert result.exit_code == 3, result.output
    assert "refused" in result.output


@pytest.mark.parametrize("path", TIER_1)
def test_apply_from_artifact_refuses_a_protected_write(tmp_path: Path, path: str) -> None:
    result = CliRunner().invoke(
        main, ["apply", "--from-artifact", str(_artifact(tmp_path, path)), "--dry-run"]
    )
    assert result.exit_code == 3, result.output


def test_apply_inline_actually_writes_an_allowed_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local default has to work, or people will reach past it."""
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    result = CliRunner().invoke(
        main, ["apply-inline", "--from-artifact", str(_artifact(tmp_path, "src/ok.py"))]
    )
    assert result.exit_code == 0, result.output
    assert (work / "src" / "ok.py").read_text() == "x"


def test_apply_inline_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    result = CliRunner().invoke(
        main, ["apply-inline", "--from-artifact", str(_artifact(tmp_path, "src/ok.py")), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert not (work / "src" / "ok.py").exists()


def test_all_three_paths_share_one_protected_list() -> None:
    """The failure a shared list prevents: a path guarded in one place and not another.

    Asserted rather than assumed, because the three points are reached differently and nothing
    else would notice one of them drifting.
    """
    from in_lockstep.core.changes import DENY_ALWAYS, ChangeGuard

    guard = ChangeGuard()
    for path in TIER_1:
        assert guard.check_path(path) is not None, f"{path} is not actually Tier 1"
    assert len(DENY_ALWAYS) > len(TIER_1), "the sample should be a subset, not the whole list"


def test_the_runner_is_the_only_way_in(workspace: Workspace) -> None:
    """`ToolSet` is the dispatch table; a name it does not contain has nothing to reach."""
    runner = ToolRunnerImpl(workspace)
    assert asyncio.run(runner("elsewhere", "write_file", {})).startswith("refused:")
    assert asyncio.run(runner("builtin", "rm_rf", {})).startswith("refused:")


# -- the boundary as the gate describes it: inside a running loop -------------------------------


def test_a_model_that_asks_for_a_protected_write_is_refused_and_can_continue(tmp_path: Path) -> None:
    """The in-loop half, end to end.

    The gate says the write is refused at the tool boundary. What makes that the right place
    rather than the guard at apply time is the second turn: the model gets the refusal as data,
    within its budget, and writes somewhere legitimate instead. Refusing at apply would discover
    the problem after every turn had been paid for.
    """
    from in_lockstep.ai.invoker import AiInvoker, InvokePolicy
    from in_lockstep.ai.pricing import CostTable, Rate
    from in_lockstep.core.spend import Budget, Spend
    from in_lockstep.llm.types import LLMOutput, Message
    from in_lockstep.privileged.egress import UnsandboxedEgress

    workspace = Workspace(root=tmp_path)
    tools, run_tool = read_write(workspace)

    table = CostTable()
    table.add("m", Rate(1.0, 1.0))

    class Scripted(LLMProvider):
        """Asks for lockstep.py, is refused, then writes somewhere it may."""

        def __init__(self) -> None:
            self.seen: list[str] = []

        def name(self) -> str:
            return "scripted"

        async def generate(self, input: LLMInput) -> LLMOutput:
            self.seen.append(input.messages[-1].content if input.messages else "")
            if len(self.seen) == 1:
                return LLMOutput(
                    tool_calls=[
                        ToolCall(id="1", name="write_file", input={"path": "lockstep.py", "contents": "evil"})
                    ]
                )
            if len(self.seen) == 2:
                return LLMOutput(
                    tool_calls=[
                        ToolCall(id="2", name="write_file", input={"path": "src/ok.py", "contents": "fine"})
                    ]
                )
            return LLMOutput(content="done")

    provider = Scripted()
    invoker = AiInvoker(
        provider,
        model="m",
        cost_table=table,
        spend=Spend(budget=Budget(usd=1.0)),
        egress=UnsandboxedEgress(),
    )
    result = asyncio.run(
        invoker.run(
            system="s",
            messages=[Message(role="user", content="go")],
            tools=tools,
            run_tool=run_tool,
            policy=InvokePolicy(max_turns=4),
        )
    )

    assert result.content == "done"
    assert "refused" in provider.seen[1], "the refusal reached the model as data"
    staged = [c.path for c in workspace.changeset().changes]
    assert staged == ["src/ok.py"], f"a refused write must stage nothing: {staged}"


def test_the_staged_set_then_crosses_the_guard_again_at_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Twice on the privileged path, and the second time treating the first as untrusted.

    A previous turn having produced a change is not a reason to trust it: a third-party MCP server
    can stage one without going through `Workspace` at all.
    """
    from in_lockstep.core.types import FileChange

    workspace = Workspace(root=tmp_path)
    # Staged around `Workspace.record`, which is exactly what a third-party MCP server does.
    workspace.changes.append(FileChange(path=".github/workflows/ci.yml", contents="evil"))

    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    payload = tmp_path / "cs.json"
    payload.write_text(
        json.dumps(
            {
                "changes": [
                    {"path": c.path, "contents": c.contents, "author": "agent"} for c in workspace.changes
                ]
            }
        )
    )
    result = CliRunner().invoke(main, ["apply-inline", "--from-artifact", str(payload)])
    assert result.exit_code == 3, result.output


# -- GATE-WORKSPACE-1: what a session staged is what it sees ----------------------------------


def test_gate_workspace_1_search_text_finds_a_symbol_at_the_line_the_session_staged_it(
    workspace: Workspace,
) -> None:
    """GATE-WORKSPACE-1. The disk says one thing and the session's write says another; the
    search answers from the write, at the staged line, and a file that exists only in the
    staged set is searched too."""
    (workspace.root / "a.py").write_text("x = 1\n")
    _, run = read_write(workspace)
    asyncio.run(
        run("builtin", "write_file", {"path": "a.py", "contents": "x = 1\n\ndef fresh():\n    pass\n"})
    )
    asyncio.run(run("builtin", "write_file", {"path": "b.py", "contents": "from a import fresh\n"}))
    hits = asyncio.run(run("builtin", "search_text", {"pattern": "fresh"}))
    assert hits.splitlines() == ["a.py:3: def fresh():", "b.py:1: from a import fresh"]
    assert (workspace.root / "a.py").read_text() == "x = 1\n", "the disk was never written"


def test_gate_workspace_1_a_staged_deletion_hides_the_file_from_search_and_listing(
    workspace: Workspace,
) -> None:
    """GATE-WORKSPACE-1. A file the session deleted is gone from the session's view of the tree
    although it is still on disk, and `list_files` names the staged new file beside the rest."""
    (workspace.root / "old.py").write_text("gone = True\n")
    (workspace.root / "keep.py").write_text("kept = True\n")
    _, run = read_write(workspace)
    asyncio.run(run("builtin", "delete_file", {"path": "old.py"}))
    asyncio.run(run("builtin", "write_file", {"path": "new.py", "contents": "fresh = True\n"}))
    assert asyncio.run(run("builtin", "search_text", {"pattern": "gone"})) == "(no matches)"
    assert asyncio.run(run("builtin", "list_files", {"glob": "*.py"})).splitlines() == ["keep.py", "new.py"]


def test_gate_workspace_1_the_three_readers_agree_and_the_guard_still_applies(workspace: Workspace) -> None:
    """GATE-WORKSPACE-1. `read_file`, `list_files` and `search_text` describe one tree, and a
    protected name stays out of the listing and the search whether it is on disk or staged."""
    (workspace.root / ".env").write_text("API_KEY=secret\n")
    _, run = read_write(workspace)
    asyncio.run(run("builtin", "write_file", {"path": "note.txt", "contents": "the key is not here\n"}))
    assert asyncio.run(run("builtin", "read_file", {"path": "note.txt"})) == "the key is not here\n"
    assert asyncio.run(run("builtin", "list_files", {"glob": "*"})).splitlines() == ["note.txt"]
    assert (
        asyncio.run(run("builtin", "search_text", {"pattern": "secret|key"}))
        == "note.txt:1: the key is not here"
    )


# -- GATE-READ-1: a long file can be read past its truncation ---------------------------------------


def test_gate_read_1_a_range_returns_the_lines_that_were_asked_for(workspace: Workspace) -> None:
    """GATE-READ-1. Lines, not characters: the unit `search_text` answers in, the unit a traceback
    names, and the unit a model asked in when it could not -- "extract lines 770-820"."""
    (workspace.root / "long.py").write_text("".join(f"line {n}\n" for n in range(1, 1001)))
    _, run = read_only(workspace)

    answer = asyncio.run(run("builtin", "read_file", {"path": "long.py", "offset": 770, "limit": 3}))
    assert "line 770\nline 771\nline 772\n" in answer
    assert "line 769" not in answer and "line 773" not in answer
    assert "[long.py lines 770-772 of 1000]" in answer, "the window says where it is in the file"


def test_gate_read_1_a_range_past_the_end_answers_with_the_length(workspace: Workspace) -> None:
    """GATE-READ-1. An answer, not an error and not silence: the model asked about a place, and
    where the file ends is what it needed to know. Empty output reads as an empty file."""
    (workspace.root / "short.py").write_text("one\ntwo\n")
    _, run = read_only(workspace)
    answer = asyncio.run(run("builtin", "read_file", {"path": "short.py", "offset": 90}))
    assert answer == "error: short.py has 2 line(s), so line 90 is past its end"


def test_gate_read_1_the_cap_still_bounds_a_range(workspace: Workspace) -> None:
    """GATE-READ-1. A caller may move the window, never raise it: what a read returns is re-sent on
    every later turn, so the ceiling is the invoker's and not the model's to choose."""
    from in_lockstep.ai.builtins import MAX_READ_CHARS

    (workspace.root / "wide.txt").write_text("".join("x" * 200 + "\n" for _ in range(1000)))
    _, run = read_only(workspace)
    answer = asyncio.run(run("builtin", "read_file", {"path": "wide.txt", "offset": 1, "limit": 999}))
    assert len(answer) <= MAX_READ_CHARS, "the header and the notice are inside the cap too (#412)"
    assert "truncated" in answer and "Raise `offset` to read on" in answer


def test_gate_read_1_a_staged_file_is_readable_past_the_cap(workspace: Workspace) -> None:
    """GATE-READ-1 meets GATE-WORKSPACE-1. What a session staged is what it sees -- and a file a
    session WROTE is the one it is most likely to be unable to read back, which is how a change to
    a 192k document was staged by something that had seen a fifth of it."""
    from in_lockstep.ai.builtins import MAX_READ_CHARS

    _, run = read_write(workspace)
    # Derived from the cap rather than a round number, so raising the cap cannot leave this test
    # asserting truncation of a file that now fits -- which is what a literal 5,000 did (#412).
    lines = MAX_READ_CHARS // 8
    body = "".join(f"staged {n}\n" for n in range(1, lines + 1))
    asyncio.run(run("builtin", "write_file", {"path": "big.py", "contents": body}))

    whole = asyncio.run(run("builtin", "read_file", {"path": "big.py"}))
    assert "truncated" in whole and f"has {lines} line(s)" in whole

    end = asyncio.run(run("builtin", "read_file", {"path": "big.py", "offset": lines - 2, "limit": 2}))
    assert f"staged {lines - 2}\nstaged {lines - 1}\n" in end, "the end of a staged file is reachable"
    assert "staged 1\n" not in end


def test_a_range_is_still_refused_where_the_whole_file_would_be(workspace: Workspace) -> None:
    """The guard judges the path, not the slice. A range is not a way round `check_read`."""
    (workspace.root / ".env").write_text("SECRET=1\nMORE=2\n")
    _, run = read_only(workspace)
    answer = asyncio.run(run("builtin", "read_file", {"path": ".env", "offset": 1, "limit": 1}))
    assert answer.startswith("refused:") and "SECRET" not in answer


# -- GATE-SCRIPT-1: what a session may run, and what it can ------------------------------------------


class _Runner:
    """A command runner that records what it was asked and reports a missing binary as 127."""

    def __init__(self, *, executables: tuple[str, ...] = (), exit_code: int = 127) -> None:
        self.executables = executables
        self.exit_code = exit_code
        self.seen: list[list[str]] = []

    async def run(self, command: list[str], *, cwd: str | None = None, timeout: float = 900.0) -> Any:
        self.seen.append(list(command))
        return type(
            "R",
            (),
            {
                "exit_code": self.exit_code,
                "stdout": "2 failed" if self.exit_code == 1 else "",
                "stderr": f"{command[0]}: not found" if self.exit_code == 127 else "",
                "how": "container:docker",
            },
        )()


async def _ok(paths: tuple[str, ...] = ()) -> str:
    return "ok"


def test_gate_script_1_a_sandbox_that_declares_its_programs_refuses_the_rest_before_running(
    workspace: Workspace,
) -> None:
    """GATE-SCRIPT-1. The allowlist is what a model MAY run; the image decides what it CAN, and
    where the sandbox says which, the answer costs no container start at all."""
    runner = _Runner(executables=("python3",))
    _, run = read_write_execute(workspace, commands=runner, tests=_ok)

    answer = asyncio.run(run("builtin", "run_script", {"command": ["pytest", "-q"]}))
    assert "'pytest' is not in this session's sandbox" in answer
    assert "not what is installed here" in answer, "the allowlist never promised otherwise"
    assert runner.seen == [], "a container was started to learn what the binding already said"

    allowed = asyncio.run(run("builtin", "run_script", {"command": ["python3", "-c", "pass"]}))
    assert runner.seen == [["python3", "-c", "pass"]], "a declared program still runs"
    assert "not in this session's sandbox" not in allowed


def test_gate_script_1_the_redirect_names_the_tools_this_session_actually_has(
    workspace: Workspace,
) -> None:
    """GATE-SCRIPT-1, derived rather than tabulated. The first version of this carried a map from
    program name to advice, which guessed what a program is for in somebody else's repository and,
    worse, named `run_tests` where no Test verb is bound -- sending a model from one dead end to
    another."""
    both = _Runner(executables=("python3",))
    _, run = read_write_execute(workspace, commands=both, tests=_ok, validates=_ok)
    answer = asyncio.run(run("builtin", "run_script", {"command": ["pytest"]}))
    assert "`run_tests`" in answer and "`run_validate`" in answer
    assert "staged writes" in answer, "and why they are not run_script"

    bare = _Runner(executables=("python3",))
    _, run = read_write_execute(workspace, commands=bare)
    answer = asyncio.run(run("builtin", "run_script", {"command": ["pytest"]}))
    assert "`run_tests`" not in answer, "a tool nothing is bound to is not advice"
    assert "Nothing in this session executes the repository's own tools" in answer


def test_gate_script_1_a_declared_program_that_is_missing_says_the_declaration_is_stale(
    workspace: Workspace,
) -> None:
    """GATE-SCRIPT-1, the backstop. A declaration decides what the model is TOLD and never what is
    true: an image changes, a tuple does not, and the run that meets the drift should point at the
    line to fix rather than leave somebody concluding the tool is broken."""
    runner = _Runner(executables=("pytest",))
    _, run = read_write_execute(workspace, commands=runner, tests=_ok)

    answer = asyncio.run(run("builtin", "run_script", {"command": ["pytest", "-q"]}))
    assert runner.seen, "a declared program is attempted, not refused"
    assert "declaration is stale" in answer and "where the sandbox is bound" in answer


def test_gate_script_1_an_undeclared_sandbox_still_learns_from_the_attempt(
    workspace: Workspace,
) -> None:
    """GATE-SCRIPT-1. Empty is unknown, not "none": nothing is refused early, and a program that
    turns out to be absent costs one turn and says so rather than returning a bare exit line."""
    runner = _Runner()
    _, run = read_write_execute(workspace, commands=runner, tests=_ok)

    answer = asyncio.run(run("builtin", "run_script", {"command": ["pytest", "-q"]}))
    assert runner.seen == [["pytest", "-q"]], "an undeclared sandbox is not second-guessed"
    assert "not in this session's sandbox" in answer and "stale" not in answer


def test_gate_script_1_the_declaration_decides_what_the_description_promises(
    workspace: Workspace,
) -> None:
    """GATE-SCRIPT-1, where the model reads it before spending anything. A list of twelve programs
    under the word "Allowed" is read as what is there, which is the only reading available to
    something that cannot look."""
    declared, _ = read_write_execute(workspace, commands=_Runner(executables=("python3", "make")))
    described = declared.resolve("run_script").description
    assert "Programs available here: python3, make" in described
    assert "refused before it runs" in described
    assert "pytest" not in described, "a program the image lacks is not offered"

    unknown, _ = read_write_execute(workspace, commands=_Runner())
    described = unknown.resolve("run_script").description
    assert "a policy, not an inventory" in described

    empty, _ = read_write_execute(workspace, commands=_Runner(executables=("cmake",)))
    described = empty.resolve("run_script").description
    assert "carries none of the programs you may run" in described


def test_a_program_that_runs_and_fails_is_still_reported_as_a_failure(workspace: Workspace) -> None:
    """The control on the redirect: 127 is "not there", and every other exit code is an answer
    about the code. Folding the two together would tell a model its failing suite was missing."""
    _, run = read_write_execute(workspace, commands=_Runner(executables=("pytest",), exit_code=1), tests=_ok)
    answer = asyncio.run(run("builtin", "run_script", {"command": ["pytest", "-q"]}))
    assert answer.startswith("exit 1 (container:docker)") and "2 failed" in answer
    assert "not in this session's sandbox" not in answer and "stale" not in answer


# -- GATE-SCRIPT-1: the declaration is checked where a probe is free --------------------------------


class _Image:
    """A runner standing in for a bound sandbox: an image, a declaration, and a shell."""

    def __init__(
        self,
        *,
        has: tuple[str, ...],
        executables: Any = (),
        shell: bool = True,
        versions: dict[str, str] | None = None,
    ):
        self.image = "example.test/image:tag"
        self.executables = executables
        self._has = has
        self._shell = shell
        # What each program prints for `--version`. Absent is not empty: an image whose programs
        # answer nothing is a real case, and the paste falls back to names there (#419).
        self._versions = versions or {}

    async def run(self, command: list[str], *, cwd: str | None = None, timeout: float = 900.0) -> Any:
        if not self._shell:
            return type("R", (), {"exit_code": 127, "stdout": "", "stderr": "sh: not found", "how": "x"})()
        script = command[-1]
        found = [p for p in self._has if f"command -v {p} " in script]
        # A shell exits with the status of its LAST statement, and this models that, because it is
        # the thing that went wrong for real: without a trailing `exit 0` the probe's status is the
        # last `command -v`, which is 127 whenever the last program happens to be missing, and
        # every image would be reported as unaskable. A double that always returned 0 would let
        # that regression back in silently.
        last = script.rsplit(";", 1)[-1].strip()
        if last == "exit 0":
            code = 0
        else:
            from in_lockstep.ai.builtins import ALLOWED_COMMANDS

            asked = [p for p in ALLOWED_COMMANDS if f"command -v {p} " in last]
            code = 0 if asked and asked[-1] in self._has else 127
        # `<program>\t<what it said>`, the shape the probe asks for.
        out = "".join(f"{p}\t{self._versions.get(p, '')}\n" for p in found)
        return type("R", (), {"exit_code": code, "stdout": out, "stderr": "", "how": "x"})()


def _doctor_over(runner: Any) -> Any:
    from in_lockstep.doctor import Report, _sandbox_executables

    lockstep = type("L", (), {"workshop": type("W", (), {"commands": runner})()})()
    report = Report()
    _sandbox_executables(report, lockstep, Path("."))
    return report


def test_gate_script_1_doctor_says_what_to_declare_when_nothing_is_declared() -> None:
    """GATE-SCRIPT-1. A declaration nobody can check goes stale silently, and one nobody can
    DISCOVER has to be known in advance -- which is what a paid run found out by trying 36 times.
    The probe belongs in a diagnostic, where a container start costs nothing that matters."""
    report = _doctor_over(_Image(has=("python3", "make", "git")))
    (finding,) = [c for c in report.checks if c.code == "DOC183"]
    assert "carries 2 of the 12" in finding.message, "git is not one run_script offers"
    assert 'executables=("python3", "make")' in finding.hint


def test_gate_script_1_doctor_pastes_the_versions_the_image_reported() -> None:
    """GATE-SCRIPT-1. `DOC184` compares declared versions across bindings, and a declaration nobody
    can obtain without running the image by hand is one nobody writes (#419). So the probe captures
    what each program printed and hands back the mapping to paste."""
    report = _doctor_over(
        _Image(has=("python3", "make"), versions={"python3": "Python 3.12.7", "make": "GNU Make 4.3"})
    )
    (finding,) = [c for c in report.checks if c.code == "DOC183"]
    assert 'executables={"python3": "Python 3.12.7", "make": "GNU Make 4.3"}' in finding.hint


def test_gate_script_1_doctor_names_a_version_the_image_no_longer_reports() -> None:
    """GATE-SCRIPT-1. A mutable tag moves and the declaration does not: `python3.11-bookworm` went
    from 3.11.16 to 3.11.14 between two runs half an hour apart (#419). A WARNING and not an error,
    because this half needs the probe and a laptop with no runtime must not fail `doctor`."""
    report = _doctor_over(
        _Image(
            has=("python3",),
            executables={"python3": "Python 3.11.16"},
            versions={"python3": "Python 3.11.14"},
        )
    )
    warnings = [c for c in report.checks if c.code == "DOC183" and c.severity.value == "warning"]
    assert warnings and "1 version(s)" in warnings[0].message
    assert "3.11.16" in warnings[0].hint and "3.11.14" in warnings[0].hint


def test_gate_script_1_doctor_names_a_declaration_that_has_drifted_from_its_image() -> None:
    """GATE-SCRIPT-1. An image changes and a tuple does not. The model is offered what the tuple
    says, so the drift is a warning naming what is gone rather than a note."""
    report = _doctor_over(_Image(has=("python3",), executables=("python3", "pytest")))
    warnings = [c for c in report.checks if c.code == "DOC183" and c.severity.value == "warning"]
    assert warnings and "pytest" in warnings[0].message
    assert "drifted" in warnings[0].hint


def test_a_declaration_that_matches_its_image_says_nothing() -> None:
    """The quiet case, which is most of them: a check that speaks when there is nothing to say is
    one people stop reading."""
    assert _doctor_over(_Image(has=("python3", "make"), executables=("python3", "make"))).checks == []


def test_an_image_that_cannot_be_asked_says_so_rather_than_reporting_it_empty() -> None:
    """A distroless image has no `sh`, and a machine may have no runtime. Neither means the image
    carries nothing, and reporting that would send somebody to fix a declaration that is right."""
    report = _doctor_over(_Image(has=("python3",), executables=("python3",), shell=False))
    (finding,) = [c for c in report.checks if c.code == "DOC183"]
    assert "could not ask" in finding.message and "unchecked" in finding.message


def test_a_runner_with_no_image_is_not_this_checks_business() -> None:
    """`run_script` may be unbound, or bound to something that is not a container at all."""
    assert _doctor_over(None).checks == []
    assert _doctor_over(type("R", (), {"image": ""})()).checks == []
