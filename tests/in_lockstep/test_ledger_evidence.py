"""Item 17: a ledger record is evidence, and evidence says when, against what, and under whom.

Three halves. Provenance: every record now carries `ts`, `head`, and which configuration
constrained the run — the fields whose absence made joining a run to a release archaeology.
Reading: `report` aggregates whatever store the repository records into, and `history --explain`
renders one record whole. Transcripts: a failed AI session leaves its per-turn conversation
behind, because metadata is not a transcript and debugging a failed run was re-running it.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from in_lockstep.ai.invoker import AiInvoker, InvocationFailed, InvokePolicy
from in_lockstep.ai.pricing import CostTable, Rate
from in_lockstep.ai.retry import RetryPolicy
from in_lockstep.ai.transcript import TranscriptWriter
from in_lockstep.cli import main
from in_lockstep.core.spend import Spend
from in_lockstep.llm.interface import LLMProvider
from in_lockstep.llm.types import LLMInput, LLMOutput, Message, TokenUsage, ToolCall
from in_lockstep.platform.ledger import GitLedger, InRepoLedger
from in_lockstep.platform.ledger.store import SCHEMA
from in_lockstep.privileged.egress import UnsandboxedEgress


def _repo(path: Path) -> Path:
    def run(*args: str) -> None:
        subprocess.run(args, cwd=path, capture_output=True, check=True)

    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.test")
    run("git", "config", "user.name", "t")
    (path / "README.md").write_text("hello\n")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "initial")
    return path


@pytest.fixture()
def hermetic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A cwd of its own and no ambient CI: provenance must reflect the run, not this machine's
    environment. EVERY `GITHUB_*` variable goes, the same rule test_cli's repo fixture applies —
    on a real runner `GITHUB_WORKSPACE` redirects `Lockstep.detect()` to the checkout, and these
    tests would silently measure the framework's own repository instead of their tmp one."""
    for var in [v for v in os.environ if v.startswith("GITHUB_")] + ["GITLAB_CI"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


# -- provenance on every record (schema 4) --------------------------------------------


def test_a_run_record_says_when_against_what_and_under_which_config(hermetic: Path) -> None:
    """The archaeology fields. A record used to say what was spent and decided but not when or
    against which commit — so `ts`, `head` and `config` are asserted here as hard requirements,
    not rendered niceties."""
    _repo(hermetic)
    assert CliRunner().invoke(main, ["init"]).exit_code == 0
    (hermetic / "sample.py").write_text("x = 1\n")
    result = CliRunner().invoke(main, ["run", "selfcheck", "--paths", str(hermetic)])
    assert "spend" in result.output, result.output

    record = asyncio.run(GitLedger(root=hermetic).read("selfcheck-local"))
    assert record is not None
    assert record["schema"] == SCHEMA
    assert str(record["ts"]).endswith("+00:00"), "wall-clock UTC, unambiguous forever"
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=hermetic, capture_output=True, text=True
    ).stdout.strip()
    assert record["head"] == head
    assert "config" in record, "which lockstep.py constrained the run is part of the evidence"
    assert "base" not in record, "no CI, no base ref — absent, never fabricated"


def test_provenance_marks_a_dirty_tree_and_carries_the_ci_base(monkeypatch, tmp_path: Path) -> None:
    """`head` on a dirty tree does not describe what the run saw, and the record must say so;
    on CI, the base ref and host-computed actor corroborate the approval trail."""
    from in_lockstep.cli import _provenance
    from in_lockstep.lockstep import Lockstep

    for var in [v for v in os.environ if v.startswith("GITHUB_")] + ["GITLAB_CI"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    monkeypatch.setenv("GITHUB_ACTOR", "octocat")
    monkeypatch.chdir(_repo(tmp_path))
    (tmp_path / "README.md").write_text("edited\n")

    lockstep = Lockstep.detect()
    lockstep.config_source = "trusted ref origin/main"
    out = _provenance(lockstep)
    assert out["dirty"] is True
    assert out["base"] == "main"
    assert out["ci_actor"] == "octocat"
    assert out["config"] == "trusted ref origin/main"


# -- report ---------------------------------------------------------------------------


def _seed(ledger: InRepoLedger, run_id: str, record: dict) -> None:
    asyncio.run(ledger.append(run_id, record))


def test_report_groups_the_ledger_and_keeps_absent_distinct_from_zero(hermetic: Path) -> None:
    """`report --by kind` over a store where one kind was never token-measured: the unmeasured
    column renders `-`, because a reassuring 0 nobody measured is the number this ledger exists
    to refuse."""
    ledger = InRepoLedger()
    _seed(ledger, "r1", {"kind": "review", "status": "succeeded", "tokens": 100, "cost_usd": 0.02})
    _seed(ledger, "r2", {"kind": "review", "status": "failed", "tokens": 300, "cost_usd": 0.04})
    _seed(ledger, "w1", {"kind": "workflow", "status": "succeeded"})

    result = CliRunner().invoke(main, ["report"])
    assert result.exit_code == 0, result.output
    review_row = next(line for line in result.output.splitlines() if line.startswith("review"))
    assert " 2 " in review_row and " 1 " in review_row, "two runs, one failed"
    assert "400" in review_row and "$" in review_row
    workflow_row = next(line for line in result.output.splitlines() if line.startswith("workflow"))
    assert "-" in workflow_row, "unmeasured tokens are absent, not zero"


def test_report_emits_json_for_the_fleet_scanner(hermetic: Path) -> None:
    ledger = InRepoLedger()
    _seed(ledger, "r1", {"kind": "review", "model": "anthropic:m", "status": "succeeded", "cost_usd": 0.02})

    result = CliRunner().invoke(main, ["report", "--by", "model", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["anthropic:m"]["runs"] == 1
    assert payload["anthropic:m"]["tokens"] is None, "absent stays null in JSON too"


def test_report_with_no_records_says_so(hermetic: Path) -> None:
    result = CliRunner().invoke(main, ["report"])
    assert result.exit_code == 0
    assert "no records yet" in result.output


# -- history --explain ----------------------------------------------------------------


def test_history_explain_renders_one_record_whole(hermetic: Path) -> None:
    _repo(hermetic)
    ledger = GitLedger(root=hermetic)
    asyncio.run(
        ledger.append(
            "fix-9",
            {
                "kind": "fix",
                "status": "failed",
                "reason": "fix.tests_failed",
                "ts": "2026-08-29T20:00:00+00:00",
                "head": "abc123",
                "config": "trusted ref origin/main",
                "model": "anthropic:m",
                "approval": {"by": "octocat", "attended": False},
                "cost_usd": 0.31,
                "tokens": 5000,
                "findings": {"count": 1, "items": [{"id": "fix.red", "message": "reproducer stayed red"}]},
            },
        )
    )
    result = CliRunner().invoke(main, ["history", "--explain", "fix-9"])
    assert result.exit_code == 0, result.output
    assert "failed  (fix.tests_failed)" in result.output
    assert "trusted ref origin/main" in result.output
    assert "octocat  (unattended)" in result.output
    assert "fix.red: reproducer stayed red" in result.output


def test_history_explain_refuses_an_unknown_run_by_name(hermetic: Path) -> None:
    _repo(hermetic)
    result = CliRunner().invoke(main, ["history", "--explain", "never-ran"])
    assert result.exit_code != 0
    assert "no record for 'never-ran'" in result.output


# -- per-turn transcripts -------------------------------------------------------------


class _Provider(LLMProvider):
    def __init__(self, replies: list) -> None:
        self.replies = list(replies)

    def name(self) -> str:
        return "stub"

    async def generate(self, input: LLMInput) -> LLMOutput:
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _invoker(provider: LLMProvider, writer: TranscriptWriter) -> AiInvoker:
    table = CostTable()
    table.add("m", Rate(0.0, 0.0))
    return AiInvoker(
        provider,
        model="m",
        cost_table=table,
        spend=Spend(),
        retry=RetryPolicy(attempts=1, base_delay=0),
        egress=UnsandboxedEgress(),
        transcript=writer,
    )


def _answer(content: str, *, tool_calls: list[ToolCall] | None = None) -> LLMOutput:
    return LLMOutput(
        content=content,
        tool_calls=tool_calls or [],
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def test_an_answered_session_leaves_its_transcript_including_the_final_answer(tmp_path: Path) -> None:
    writer = TranscriptWriter("run-1", root=tmp_path)
    ai = _invoker(_Provider([_answer("the answer")]), writer)
    asyncio.run(ai.run(system="be brief", messages=[Message(role="user", content="question")]))

    lines = writer.path().read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["ended"] == "answered"
    assert record["model"] == "m"
    assert [m["role"] for m in record["messages"]] == ["user", "assistant"]
    assert record["messages"][-1]["content"] == "the answer"


def test_a_provider_failure_still_persists_what_the_session_had(tmp_path: Path) -> None:
    """The roadmap's exact complaint: a failed run left metadata and no transcript. The partial
    history — here, the user turn the provider never answered — must survive the raise."""
    from in_lockstep.llm.interface import AuthenticationError

    writer = TranscriptWriter("run-2", root=tmp_path)
    ai = _invoker(_Provider([AuthenticationError("401", status_code=401)]), writer)
    with pytest.raises(InvocationFailed):
        asyncio.run(ai.run(system="s", messages=[Message(role="user", content="question")]))

    record = json.loads(writer.path().read_text())
    assert record["ended"] == "provider_error"
    assert [m["role"] for m in record["messages"]] == ["user"]


def test_an_exhausted_session_records_every_turn_and_tool_result(tmp_path: Path) -> None:
    from in_lockstep.ai.tools import Tool, ToolSet

    call = ToolCall(id="t1", name="read_file", input={"path": "x"})
    writer = TranscriptWriter("run-3", root=tmp_path)
    ai = _invoker(_Provider([_answer("looking", tool_calls=[call])]), writer)

    async def run_tool(server: str, name: str, args: dict) -> str:
        return "file contents"

    asyncio.run(
        ai.run(
            system="s",
            messages=[Message(role="user", content="go")],
            tools=ToolSet.of(Tool(server="builtin", name="read_file")),
            run_tool=run_tool,
            policy=InvokePolicy(max_turns=1),
        )
    )
    record = json.loads(writer.path().read_text())
    assert record["ended"] == "exhausted"
    roles = [m["role"] for m in record["messages"]]
    assert roles == ["user", "assistant", "tool_result"]
    assert record["messages"][1]["tool_calls"] == ["read_file"]
    assert record["messages"][2]["content"] == "file contents"


def test_transcripts_are_redacted_on_the_way_to_disk(tmp_path: Path) -> None:
    """A transcript is the file most likely to be pasted whole into an issue, and a tool result
    is where a credential most plausibly rides into one."""
    secret = "sk-" + "a1b2c3d4e5f6g7h8i9"
    writer = TranscriptWriter("run-4", root=tmp_path)
    ai = _invoker(_Provider([_answer(f"found {secret} in config")]), writer)
    asyncio.run(ai.run(system="s", messages=[Message(role="user", content="scan")]))

    text = writer.path().read_text()
    assert secret not in text
    assert "***" in text


def test_two_invocations_in_one_run_append_rather_than_overwrite(tmp_path: Path) -> None:
    """One run is several sessions — TDD talks to the model per phase — and the transcript reads
    in order, all of it."""
    writer = TranscriptWriter("run-5", root=tmp_path)
    for content in ("first", "second"):
        ai = _invoker(_Provider([_answer(content)]), writer)
        asyncio.run(ai.run(system="s", messages=[Message(role="user", content="go")]))
    lines = [json.loads(line) for line in writer.path().read_text().splitlines()]
    assert [ln["messages"][-1]["content"] for ln in lines] == ["first", "second"]


def test_oversized_content_is_bounded_and_the_cut_is_named(tmp_path: Path) -> None:
    from in_lockstep.ai.transcript import MAX_CONTENT_CHARS

    writer = TranscriptWriter("run-6", root=tmp_path)
    writer.append(
        model="m",
        ended="answered",
        messages=[Message(role="tool_result", content="x" * (MAX_CONTENT_CHARS + 500))],
    )
    record = json.loads(writer.path().read_text())
    assert len(record["messages"][0]["content"]) == MAX_CONTENT_CHARS
    assert record["messages"][0]["truncated_chars"] == 500


def test_the_explain_view_points_at_the_transcript_when_one_exists(hermetic: Path) -> None:
    _repo(hermetic)
    asyncio.run(GitLedger(root=hermetic).append("run-7", {"kind": "review", "status": "errored"}))
    TranscriptWriter("run-7").append(model="m", ended="provider_error", messages=[])

    result = CliRunner().invoke(main, ["history", "--explain", "run-7"])
    assert result.exit_code == 0, result.output
    assert "transcripts/run-7.jsonl" in result.output


# -- the rolling daily ceiling (item 18) ----------------------------------------------


def test_spent_in_window_sums_only_placeable_recent_records() -> None:
    """A record with no timestamp cannot be placed in a window and does not count — the honest
    reading for pre-provenance records, and the ledger's measurement-nobody-took rule."""
    from datetime import UTC, datetime

    from in_lockstep.platform.ledger import spent_in_window

    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
    records = [
        {"ts": "2026-08-29T11:00:00+00:00", "cost_usd": 0.30},  # inside the window
        {"ts": "2026-08-29T11:30:00+00:00", "cost_usd": 0.20},  # inside
        {"ts": "2026-08-28T11:00:00+00:00", "cost_usd": 5.00},  # 25h old: outside
        {"cost_usd": 9.99},  # schema-3: no ts, cannot be placed
        {"ts": "not-a-time", "cost_usd": 9.99},
        {"ts": "2026-08-29T11:45:00+00:00"},  # no cost recorded: absent is not zero, and not a sum
    ]
    assert spent_in_window(records, now=now) == pytest.approx(0.50)


def _spending_lockstep():
    """A lifecycle the ceiling is in scope for: one bound adapter that declares it spends, and a
    declared budget so GATE-BUDGET-1 stays satisfied."""
    from in_lockstep.core.spend import Budget
    from in_lockstep.core.verbs import Capability
    from in_lockstep.lockstep import Lockstep

    class _SpendVerb:
        pass

    class _Spender:
        capabilities = frozenset({Capability.SPENDS_BUDGET})

    lockstep = Lockstep.detect()
    lockstep.bind(_SpendVerb, _Spender())
    lockstep.budget = Budget(usd=2.00)
    return lockstep


def test_the_daily_ceiling_refuses_a_run_pre_start_and_the_window_rolls(hermetic: Path, monkeypatch) -> None:
    """The partition the crosswalk row said was Lost: per repository, per rolling day, refused
    before the run starts — from the same store every run writes."""
    from datetime import UTC, datetime, timedelta

    from in_lockstep.core.spend import DailySpendExceeded

    _repo(hermetic)
    ledger = GitLedger(root=hermetic)
    now = datetime.now(UTC)
    asyncio.run(ledger.append("r1", {"ts": now.isoformat(timespec="seconds"), "cost_usd": 0.80}))

    monkeypatch.setenv("IN_LOCKSTEP_DAILY_LIMIT", "0.75")
    lockstep = _spending_lockstep()
    with pytest.raises(DailySpendExceeded, match=r"\$0\.80 in the last 24h"):
        lockstep.context(run_id="next")
    assert DailySpendExceeded.reason == "cost.daily_exceeded"

    monkeypatch.setenv("IN_LOCKSTEP_DAILY_LIMIT", "1.00")
    assert lockstep.context(run_id="next") is not None, "under the ceiling, the run starts"

    # The same spend recorded 25 hours ago no longer counts: the window rolls.
    old = (now - timedelta(hours=25)).isoformat(timespec="seconds")
    asyncio.run(ledger.append("r1", {"ts": old, "cost_usd": 0.80}))
    monkeypatch.setenv("IN_LOCKSTEP_DAILY_LIMIT", "0.75")
    assert lockstep.context(run_id="next") is not None


def test_no_declared_daily_limit_means_no_ledger_read(hermetic: Path, monkeypatch) -> None:
    """Advisory-first, resolved tension #4: the ceiling is an opt-in an organisation states, not
    a default that surprises every laptop."""
    monkeypatch.delenv("IN_LOCKSTEP_DAILY_LIMIT", raising=False)
    _repo(hermetic)
    assert _spending_lockstep().context(run_id="r") is not None


def test_a_lifecycle_that_cannot_spend_is_not_gated_by_the_spend_ceiling(hermetic: Path, monkeypatch) -> None:
    """Scoped like GATE-BUDGET-1: refusing a free selfcheck because yesterday's agent runs were
    expensive teaches people the refusal is noise."""
    from datetime import UTC, datetime

    from in_lockstep.lockstep import Lockstep

    _repo(hermetic)
    ledger = GitLedger(root=hermetic)
    asyncio.run(ledger.append("r1", {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "cost_usd": 9.0}))
    monkeypatch.setenv("IN_LOCKSTEP_DAILY_LIMIT", "1.00")
    assert Lockstep.detect().context(run_id="r") is not None


def test_a_malformed_daily_limit_is_loud_not_silently_unenforced(hermetic: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IN_LOCKSTEP_DAILY_LIMIT", "one dollar")
    _repo(hermetic)
    assert _spending_lockstep().context(run_id="r") is not None
    assert "not a number; ceiling not enforced" in capsys.readouterr().out


def test_the_cli_exits_blocked_when_the_ceiling_refuses(hermetic: Path, monkeypatch) -> None:
    """BLOCKED, not failed: the exit code is how a trampoline's `if` tells 'over the daily
    window' from 'broken'."""
    from datetime import UTC, datetime

    _repo(hermetic)
    assert CliRunner().invoke(main, ["init"]).exit_code == 0
    ledger = GitLedger(root=hermetic)
    asyncio.run(
        ledger.append("r1", {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "cost_usd": 3.00})
    )
    module = hermetic / ".lockstep" / "lockstep.py"
    module.write_text(
        module.read_text()
        + "\nfrom in_lockstep.core.verbs import Capability\n"
        + "class _SpendVerb: pass\n"
        + "class _Spender:\n"
        + "    capabilities = frozenset({Capability.SPENDS_BUDGET})\n"
        + "lockstep.bind(_SpendVerb, _Spender())\n"
    )
    monkeypatch.setenv("IN_LOCKSTEP_DAILY_LIMIT", "1.00")
    result = CliRunner().invoke(main, ["run", "selfcheck", "--paths", str(hermetic)])
    assert result.exit_code == 3, result.output
    assert "cost.daily_exceeded" in result.output
