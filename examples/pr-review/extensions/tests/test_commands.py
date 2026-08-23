"""What reaches the reviewing agent, and what deliberately does not."""

from __future__ import annotations

import json

from click.testing import CliRunner
from review_ext.commands import MAX_PATCH, assemble, is_generated, pr_diff

PULL = {
    "number": 7,
    "title": "Skip priceless items when totalling",
    "body": "Fixes APP-412.",
    "base": {"ref": "main"},
    "head": {"sha": "abc123"},
}


def file(path, patch="@@ -1 +1 @@\n-a\n+b", status="modified"):
    return {"filename": path, "patch": patch, "status": status, "additions": 1, "deletions": 1}


def test_the_diff_carries_what_a_review_needs():
    diff = assemble([file("src/app.py")], PULL)
    assert diff["title"].startswith("Skip priceless")
    assert diff["head_sha"] == "abc123"
    assert diff["files"][0]["path"] == "src/app.py"


def test_lockfiles_are_not_reviewed():
    """Large, mechanical, and never what a review is about."""
    assert is_generated("uv.lock")
    assert is_generated(".github/workflows/aw-x.lock.yml")
    assert not is_generated("src/app.py")


def test_what_was_skipped_is_named_rather_than_dropped():
    """A reviewer must know what it did not see."""
    diff = assemble([file("uv.lock"), file("src/app.py")], PULL)
    assert [f["path"] for f in diff["files"]] == ["src/app.py"]
    assert any("uv.lock" in entry for entry in diff["not_reviewed"])
    assert diff["truncated"] is True


def test_a_binary_file_is_named_not_omitted():
    diff = assemble([{"filename": "logo.png", "status": "added"}], PULL)
    assert diff["files"] == []
    assert any("logo.png" in entry for entry in diff["not_reviewed"])


def test_an_enormous_patch_is_truncated_visibly():
    """Truncating deliberately beats truncating implicitly by running out of context."""
    diff = assemble([file("src/big.py", patch="x" * (MAX_PATCH + 5000))], PULL)
    assert "patch truncated" in diff["files"][0]["patch"]


def test_the_total_budget_is_enforced_across_files():
    files = [file(f"src/f{n}.py", patch="x" * 20_000) for n in range(20)]
    diff = assemble(files, PULL)
    assert sum(len(f["patch"]) for f in diff["files"]) <= 180_000
    assert diff["not_reviewed"]


def test_a_small_pull_request_is_not_marked_truncated():
    assert assemble([file("src/app.py")], PULL)["truncated"] is False


def test_the_command_writes_what_the_reviewer_reads(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "files.json").write_text(json.dumps([file("src/app.py")]), encoding="utf-8")
    (fixtures / "pull.json").write_text(json.dumps(PULL), encoding="utf-8")
    output = tmp_path / "diff.json"

    result = CliRunner().invoke(
        pr_diff, ["--pr=7", "--repo=o/r", f"--output={output}", f"--from-dir={fixtures}"]
    )
    assert result.exit_code == 0
    assert "head_sha=abc123" in result.output
    assert json.loads(output.read_text())["files"][0]["path"] == "src/app.py"
