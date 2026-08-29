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

import pytest
from click.testing import CliRunner

from in_lockstep.ai.builtins import ToolRunnerImpl, Workspace, read_only, read_write
from in_lockstep.cli import main
from in_lockstep.core.verbs import Capability

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


def test_a_huge_read_is_truncated(workspace: Workspace) -> None:
    """A tool result is model input; an unbounded one is an unbounded prompt next turn."""
    from in_lockstep.ai.builtins import MAX_READ_CHARS

    (workspace.root / "big.txt").write_text("x" * (MAX_READ_CHARS + 100))
    _, run = read_only(workspace)
    assert "[truncated]" in asyncio.run(run("builtin", "read_file", {"path": "big.txt"}))


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
def test_apply_inline_refuses_a_protected_write(tmp_path: Path, monkeypatch, path: str) -> None:
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


def test_apply_inline_actually_writes_an_allowed_change(tmp_path: Path, monkeypatch) -> None:
    """The local default has to work, or people will reach past it."""
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    result = CliRunner().invoke(
        main, ["apply-inline", "--from-artifact", str(_artifact(tmp_path, "src/ok.py"))]
    )
    assert result.exit_code == 0, result.output
    assert (work / "src" / "ok.py").read_text() == "x"


def test_apply_inline_dry_run_writes_nothing(tmp_path: Path, monkeypatch) -> None:
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
    from in_lockstep.llm.types import LLMOutput, Message, ToolCall
    from in_lockstep.privileged.egress import UnsandboxedEgress

    workspace = Workspace(root=tmp_path)
    tools, run_tool = read_write(workspace)

    table = CostTable()
    table.add("m", Rate(1.0, 1.0))

    class Scripted:
        """Asks for lockstep.py, is refused, then writes somewhere it may."""

        def __init__(self) -> None:
            self.seen: list[str] = []

        def name(self) -> str:
            return "scripted"

        async def generate(self, input):
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


def test_the_staged_set_then_crosses_the_guard_again_at_apply(tmp_path: Path, monkeypatch) -> None:
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
