"""Posting a review's findings from a job that holds no provider key.

`review --comment` posts from inside the command, which means the process that called the model
also holds the token that writes the repository. `test_workflow_triggers.py` asserts that never
happens, so a chat-ops review cannot post from the job that ran it.

The split: the reviewing job writes the body it composed, and a second job with `pull-requests:
write` and no provider SDK installed posts it. The marker travels INSIDE the body — `review_comment`
already ends with it — so the aspect a comment asked for never has to appear in YAML, which is the
same rule `GATE-REVIEW-3` keeps for the parse.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from in_lockstep.cli import main


@pytest.fixture
def bare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty directory with no GitHub environment around it — see test_review_from_a_comment."""
    monkeypatch.chdir(tmp_path)
    for name in [k for k in os.environ if k.startswith("GITHUB_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path


def test_the_written_body_carries_the_marker_that_finds_it_again(bare: Path) -> None:
    """The property the whole split rests on. Without the marker in the file, the posting job would
    need to be told which lens ran — and that is the one thing it cannot be told, because the lens
    came out of an untrusted comment and was resolved in the other job."""
    from in_lockstep.core.outcome import Outcome, Status
    from in_lockstep.platform.report import marker, review_comment

    body = review_comment("security", Outcome(status=Status.SUCCEEDED, decided=True))
    assert marker("review:security") in body


def test_a_body_file_is_posted_under_the_marker_it_carries(bare: Path, monkeypatch) -> None:
    """The posting half, with the host stubbed. The marker is read out of the body rather than
    passed as a flag, so nothing in a workflow file ever names an aspect."""
    import in_lockstep.platform.hosted as hosted

    posted: list[tuple[int, str, str]] = []

    class _Host:
        async def upsert_comment(self, target: int, body: str, marker: str) -> None:
            posted.append((target, body, marker))

    monkeypatch.setattr(hosted, "hosted_scm", lambda *a, **k: _Host())
    body = bare / "findings.md"
    body.write_text("### Security\n\nNothing to report.\n\n<!-- in-lockstep:review:security -->")

    result = CliRunner().invoke(main, ["comment", "--pr", "199", "--body-file", str(body)])
    assert result.exit_code == 0, result.output
    assert posted == [(199, body.read_text(), "<!-- in-lockstep:review:security -->")]


def test_a_body_with_no_marker_is_refused_rather_than_posted_unanchored(bare: Path, monkeypatch) -> None:
    """An unanchored comment cannot be found again, so the next run posts a second one beside it
    instead of editing it. Two comments that disagree is worse than one that is out of date."""
    import in_lockstep.platform.hosted as hosted

    def _boom(*_a, **_k):
        raise AssertionError("a body with no marker reached the host")

    monkeypatch.setattr(hosted, "hosted_scm", _boom)
    body = bare / "findings.md"
    body.write_text("Nothing to report.\n")

    result = CliRunner().invoke(main, ["comment", "--pr", "199", "--body-file", str(body)])
    assert result.exit_code != 0
    assert "marker" in result.output.lower(), result.output


def test_the_posting_command_needs_no_provider_at_all(bare: Path, monkeypatch) -> None:
    """The reason it is a separate command in a separate job. It must be constructible and runnable
    with no provider SDK installed and no key present — a fact about the environment, not a property
    of the code path taken. `test_the_writing_job_does_not_install_a_provider_sdk` asserts the other
    half in the workflow."""
    import in_lockstep.platform.hosted as hosted

    class _Host:
        async def upsert_comment(self, target: int, body: str, marker: str) -> None:
            return None

    monkeypatch.setattr(hosted, "hosted_scm", lambda *a, **k: _Host())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    body = bare / "f.md"
    body.write_text("x\n\n<!-- in-lockstep:review:tests -->")
    assert CliRunner().invoke(main, ["comment", "--pr", "1", "--body-file", str(body)]).exit_code == 0


def test_the_reviewing_command_writes_the_body_instead_of_posting_it(bare: Path, monkeypatch) -> None:
    """The CI form. `--comment` posts from the process that called the model; `--comment-out` hands
    it to a job that holds no key. A stub host that explodes proves nothing was posted."""
    import in_lockstep.platform.hosted as hosted

    def _boom(*_a, **_k):
        raise AssertionError("--comment-out posted instead of writing")

    monkeypatch.setattr(hosted, "hosted_scm", _boom)
    # A real patch and `--dry-run`: the shipped cassette is keyed on the demo diff, so `--offline`
    # with a diff of our own is a key miss rather than a review.
    diff = bare / "change.patch"
    diff.write_text("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n")
    out = bare / "findings.md"
    result = CliRunner().invoke(
        main,
        ["review", "--dry-run", "--diff", str(diff), "--aspect", "security", "--comment-out", str(out)],
    )
    assert out.exists(), result.output
    assert "<!-- in-lockstep:review:security -->" in out.read_text()
