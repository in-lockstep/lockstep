"""Putting a review where a person reads it: the sticky PR comment.

The rendering has to be legible markdown, the injection signals have to be separated from code
findings, and the upsert has to edit its own prior comment rather than pile a new one on the
thread each run.
"""

from __future__ import annotations

import asyncio

from in_lockstep.core.outcome import Cost, Finding, Outcome, Severity, Status
from in_lockstep.core.types import ChangeSet, TestReport, TestVerdict
from in_lockstep.platform.report import implement_body, marker, review_comment


def _outcome(findings=(), *, decided=True, status=Status.SUCCEEDED, usd=0.02, billed=1.0) -> Outcome:
    return Outcome(
        status=status,
        findings=tuple(findings),
        decided=decided,
        cost=Cost(input_tokens=1000, output_tokens=120, usd=usd, billed_tokens=1120),
    )


def test_a_clean_review_says_so() -> None:
    body = review_comment("security", _outcome())
    assert "in-lockstep review — security" in body
    assert "succeeded" in body
    assert "No findings." in body
    assert marker("review:security") in body


def test_findings_render_as_a_table_with_location() -> None:
    findings = [
        Finding(
            id="review.security",
            message="SQL built by string concat",
            severity=Severity.WARNING,
            path="a.py",
            line=42,
        ),
    ]
    body = review_comment("security", _outcome(findings))
    assert "| location | finding |" in body
    assert "`a.py:42`" in body
    assert "SQL built by string concat" in body


def test_injection_signals_are_split_from_code_findings() -> None:
    findings = [
        Finding(id="review.security", message="a real code finding", path="a.py", line=1),
        Finding(id="injection.exfil_token_names", message="high: ANTHROPIC_API_KEY"),
    ]
    body = review_comment("security", _outcome(findings))
    assert "Prompt-injection signals in the diff" in body
    assert "ANTHROPIC_API_KEY" in body
    # The injection line must not be in the code-findings table.
    table = body.split("Prompt-injection")[0]
    assert "ANTHROPIC_API_KEY" not in table


def test_a_replayed_review_says_nothing_was_billed() -> None:
    body = review_comment("security", _outcome(usd=0.0, billed=0.0))
    # billed_fraction is derived; a replay has real tokens and no cost.
    assert "replayed, nothing billed" in body or "$0.0000" in body


def test_decided_nothing_is_surfaced() -> None:
    body = review_comment("security", _outcome(decided=False))
    assert "decided nothing" in body
    # A review that decided nothing must not print the reassuring "No findings."
    assert "No findings." not in body


def test_a_pipe_in_a_finding_does_not_break_the_table() -> None:
    findings = [Finding(id="review.security", message="a || b is always true", path="a.py", line=3)]
    body = review_comment("security", _outcome(findings))
    assert "a \\|\\| b" in body


def test_a_pipe_or_backtick_in_the_path_is_escaped_too() -> None:
    """The path is untrusted model output like the message; a raw pipe would split the table row
    and a backtick would tear the code span open."""
    findings = [Finding(id="review.security", message="msg", path="weird|`name.py", line=7)]
    body = review_comment("security", _outcome(findings))
    row = next(line for line in body.splitlines() if "name.py" in line)
    assert "weird\\|name.py" in row, "the path's pipe must be escaped"
    # The only backticks in the row are the two that open/close the location code span.
    assert row.count("`") == 2, "a backtick from the path would open a stray span"


# -- the implement PR body ----------------------------------------------------------------------


def _cs() -> ChangeSet:
    return ChangeSet(summary="do the thing")


def test_implement_body_always_carries_the_untrusted_warning() -> None:
    body = implement_body(_cs(), None)
    assert "untrusted input to a model that held write tools" in body
    assert marker("implement") in body


def test_implement_body_reports_a_green_verdict() -> None:
    verdict = TestVerdict.of("succeeded", True, TestReport(total=42, passed=42))
    body = implement_body(_cs(), verdict)
    assert "✅" in body and "42 passed" in body


def test_implement_body_reports_a_red_verdict() -> None:
    verdict = TestVerdict.of("failed", True, TestReport(total=42, passed=39, failed=3))
    body = implement_body(_cs(), verdict)
    assert "🛑" in body and "3 of 42 failed" in body


def test_implement_body_calls_an_unbound_test_unverified_not_green() -> None:
    body = implement_body(_cs(), None)
    assert "not run" in body and "unverified" in body
    assert "✅" not in body, "a change nobody tested must not read as passing"


def test_implement_body_marks_a_collected_nothing_run_as_undecided() -> None:
    verdict = TestVerdict.of("succeeded", False, TestReport())
    assert verdict.green is False
    body = implement_body(_cs(), verdict)
    assert "collected nothing" in body
    assert "✅" not in body


# -- the sticky upsert --------------------------------------------------------------------------


class _FakeGh:
    """Records gh invocations and answers the comment-list call, so the upsert logic is tested
    without a network or a real repository."""

    def __init__(self, existing: list | None = None) -> None:
        self.existing = existing or []
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str):
        self.calls.append(args)
        if args and args[0] == "api" and "comments" in args[-1] and "-X" not in args:
            import json

            return 0, json.dumps(self.existing), ""
        return 0, "", ""

    @property
    def list_calls(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c and c[0] == "api" and "comments" in c[-1] and "-X" not in c]


def _scm(fake: _FakeGh):
    from in_lockstep.platform.scm import GitHubScm

    scm = GitHubScm(root=".")
    scm._gh = fake  # type: ignore[method-assign]
    return scm


def test_upsert_creates_a_comment_when_none_exists() -> None:
    fake = _FakeGh(existing=[])
    scm = _scm(fake)
    asyncio.run(scm.upsert_comment(42, "the review body", marker("review:security")))
    # The last call is a POST to the issue's comments, carrying the marker.
    create = fake.calls[-1]
    assert create[0] == "api" and create[1].endswith("/issues/42/comments")
    assert any("in-lockstep:review:security" in a for a in create)


def test_upsert_edits_its_own_prior_comment_in_place() -> None:
    fake = _FakeGh(existing=[{"id": 7, "body": "old review\n\n<!-- in-lockstep:review:security -->"}])
    scm = _scm(fake)
    asyncio.run(scm.upsert_comment(42, "new review body", marker("review:security")))
    patch = fake.calls[-1]
    assert "PATCH" in patch and any("/issues/comments/7" in a for a in patch)
    assert any("new review body" in a for a in patch)


def test_upsert_ignores_someone_elses_comment() -> None:
    fake = _FakeGh(existing=[{"id": 9, "body": "a human comment, no marker"}])
    scm = _scm(fake)
    asyncio.run(scm.upsert_comment(42, "body", marker("review:security")))
    # No marker match → it creates rather than editing the human's comment.
    assert "PATCH" not in fake.calls[-1]
    assert fake.calls[-1][1].endswith("/issues/42/comments")


def test_upsert_paginates_the_comment_list() -> None:
    """The framework's comment is the newest; without --paginate a busy PR's first page never
    holds it and every run posts a duplicate."""
    fake = _FakeGh(existing=[])
    scm = _scm(fake)
    asyncio.run(scm.upsert_comment(42, "body", marker("review:security")))
    assert any("--paginate" in call for call in fake.list_calls), "the list must page through all comments"
