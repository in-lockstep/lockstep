"""Posting the reviews.

The script is bash because it needs `gh` and the job's token, but what it constructs is the thing a
human sees on their pull request — so it is exercised for real against a stubbed `gh` that captures
what would have been sent.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "post-reviews.sh"

FINDINGS = {
    "title": "Security",
    "summary": "One traversal, in the upload handler.",
    "findings": [
        {"path": "src/files.py", "line": 84, "comment": "A `name` of `../../etc/passwd` reaches open()."}
    ],
}


@pytest.fixture
def workspace(tmp_path):
    """A reviews directory, a diff, and a `gh` that records what it was asked to send."""
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    (tmp_path / "diff.json").write_text(json.dumps({"head_sha": "abc123"}), encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    (bin_dir / "gh").write_text(
        f'#!/usr/bin/env bash\n{{ echo "ARGS $*"; cat; echo; }} >> "{log}"\n', encoding="utf-8"
    )
    (bin_dir / "gh").chmod(0o755)
    return tmp_path, reviews, log, bin_dir


def run(workspace, pr="7"):
    tmp_path, reviews, log, bin_dir = workspace
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_REPOSITORY": "o/r",
    }
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            f"--pr={pr}",
            f"--reviews={reviews}",
            f"--diff={tmp_path / 'diff.json'}",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result, (log.read_text() if log.is_file() else "")


def test_each_aspect_becomes_its_own_review(workspace):
    """`/review security intent` produces two reviews, not one mentioning both."""
    _, reviews, _, _ = workspace
    (reviews / "security.json").write_text(json.dumps(FINDINGS), encoding="utf-8")
    (reviews / "intent.json").write_text(json.dumps({"title": "Intent", "summary": "Matches."}), encoding="utf-8")

    result, calls = run(workspace)
    assert result.returncode == 0
    assert calls.count("pulls/7/reviews") == 2
    assert "posted or revised 2 review(s)" in result.stdout


def test_a_review_carries_a_marker_naming_its_aspect_and_commit(workspace):
    """This is the only durable record of what was reviewed when."""
    _, reviews, _, _ = workspace
    (reviews / "security.json").write_text(json.dumps(FINDINGS), encoding="utf-8")
    _, calls = run(workspace)
    assert "lockstep:review aspect=security sha=abc123" in calls


def test_findings_are_posted_as_inline_comments_on_the_diff(workspace):
    _, reviews, _, _ = workspace
    (reviews / "security.json").write_text(json.dumps(FINDINGS), encoding="utf-8")
    _, calls = run(workspace)
    payload = json.loads(calls.split("\n", 1)[1].strip())
    assert payload["comments"][0]["path"] == "src/files.py"
    assert payload["comments"][0]["line"] == 84
    assert payload["commit_id"] == "abc123"


def test_a_review_with_nothing_to_say_is_still_posted(workspace):
    """Silence is ambiguous; "nothing found" is a result somebody can act on."""
    _, reviews, _, _ = workspace
    (reviews / "tests.json").write_text(json.dumps({"title": "Tests", "summary": "Well covered."}), encoding="utf-8")
    result, calls = run(workspace)
    assert result.returncode == 0
    assert "Well covered." in calls


def test_a_previously_reviewed_aspect_is_revised_in_place(workspace):
    """A reviewer who addressed a finding wants it resolved, not repeated below itself."""
    _, reviews, _, _ = workspace
    (reviews / "security.json").write_text(
        json.dumps({**FINDINGS, "previous_review_id": 42}), encoding="utf-8"
    )
    result, calls = run(workspace)
    assert "-X PUT" in calls
    assert "pulls/7/reviews/42" in calls
    assert "revised the security review" in result.stdout


def test_a_first_review_is_posted_not_revised(workspace):
    _, reviews, _, _ = workspace
    (reviews / "security.json").write_text(json.dumps(FINDINGS), encoding="utf-8")
    result, calls = run(workspace)
    assert "-X POST" in calls
    assert "posted the security review" in result.stdout


def test_nothing_to_post_is_not_a_failure(workspace):
    """Every aspect was already reviewed at this commit, so the agent never ran."""
    result, calls = run(workspace)
    assert result.returncode == 0
    assert "no aspect needed reviewing" in result.stdout
    assert calls == ""


def test_no_pull_request_is_not_a_failure(workspace):
    _, reviews, _, _ = workspace
    (reviews / "security.json").write_text(json.dumps(FINDINGS), encoding="utf-8")
    result, _ = run(workspace, pr="")
    assert result.returncode == 0
    assert "no pull request" in result.stderr
