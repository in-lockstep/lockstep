"""An acknowledgement outside git's trailer block is refused with a message that says so.

GATE-CFG-4. The trailer must be in the commit's final paragraph (git's trailer block). When it
is separated from the last line by a blank line — as happens naturally when `Co-Authored-By:` is
already at the end — git reads it as prose rather than a trailer, and the check refuses silently
with the wrong diagnosis. These tests assert that the two refusals differ, so a person is told
what actually went wrong.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from in_lockstep.cli import main
from in_lockstep.config_ref import ACKNOWLEDGEMENT, acknowledgement


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with `main` and a branch off it, pinned to `main`."""
    root = tmp_path / "repo"
    (root / ".lockstep").mkdir(parents=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "branch", "-M", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / ".lockstep" / "lockstep.py").write_text("lockstep = object()\n")
    (root / "src.py").write_text("x = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "change")
    return root


def _commit(root: Path, path: str, body: str, message: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


# ---------------------------------------------------------------------------
# Unit-level: near_miss_acknowledgement
# ---------------------------------------------------------------------------


def test_near_miss_returns_empty_when_no_acknowledgement_anywhere(repo: Path) -> None:
    """No trailer key in any commit message at all — nothing to report."""
    from in_lockstep.config_ref import near_miss_acknowledgement

    _commit(repo, ".lockstep/lockstep.py", "lockstep = 'rebound'\n", "feat: rebind Validate")

    assert near_miss_acknowledgement(repo, "main") == ""


def test_near_miss_returns_empty_when_acknowledgement_is_a_proper_trailer(repo: Path) -> None:
    """The trailer IS in the trailer block. No near-miss: git sees it, and `acknowledgement()`
    returns it. A near-miss here would be a false alarm."""
    from in_lockstep.config_ref import near_miss_acknowledgement

    _commit(
        repo,
        ".lockstep/lockstep.py",
        "lockstep = 'rebound'\n",
        f"feat: rebind Validate\n\n{ACKNOWLEDGEMENT}: first run is main.",
    )

    # git sees the trailer — acknowledgement() returns it
    assert acknowledgement(repo, "main") == "first run is main."
    # no near-miss because it IS a proper trailer
    assert near_miss_acknowledgement(repo, "main") == ""


def test_near_miss_detects_acknowledgement_outside_trailer_block(repo: Path) -> None:
    """The acknowledgement is separated from Co-Authored-By by a blank line, so git reads it
    as prose. `acknowledgement()` returns empty, but `near_miss_acknowledgement()` finds it."""
    from in_lockstep.config_ref import near_miss_acknowledgement

    _commit(
        repo,
        ".lockstep/lockstep.py",
        "lockstep = 'rebound'\n",
        (
            "feat: rebind Validate\n"
            "\n"
            f"{ACKNOWLEDGEMENT}: first run is main, and I will watch it.\n"
            "\n"
            "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
        ),
    )

    # git does NOT see it as a trailer
    assert acknowledgement(repo, "main") == ""
    # but the near-miss scanner does
    assert near_miss_acknowledgement(repo, "main") != ""


# ---------------------------------------------------------------------------
# CLI-level: the two refusals are distinct
# ---------------------------------------------------------------------------


def test_cli_refusal_for_no_acknowledgement_at_all(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When there is no acknowledgement anywhere in the commit message, the refusal tells the
    person to add the trailer."""
    _commit(repo, ".lockstep/lockstep.py", "lockstep = 'rebound'\n", "feat: rebind Validate")
    for name in ("GITHUB_ACTIONS", "GITLAB_CI"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(main, ["config", "--base", "main"])

    assert result.exit_code == 1
    assert f"{ACKNOWLEDGEMENT}:" in result.output
    # This refusal must NOT mention "not in the trailer block" — it is about absence.
    assert "trailer block" not in result.output.lower()


def test_cli_refusal_for_acknowledgement_outside_trailer_block(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the acknowledgement is in the message but not in git's trailer block, the refusal
    says so and says how to fix it. This is a different diagnosis from 'no acknowledgement'."""
    _commit(
        repo,
        ".lockstep/lockstep.py",
        "lockstep = 'rebound'\n",
        (
            "feat: rebind Validate\n"
            "\n"
            f"{ACKNOWLEDGEMENT}: first run is main.\n"
            "\n"
            "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
        ),
    )
    for name in ("GITHUB_ACTIONS", "GITLAB_CI"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(main, ["config", "--base", "main"])

    assert result.exit_code == 1
    # This refusal MUST mention the trailer-block problem
    assert "trailer block" in result.output.lower() or "last paragraph" in result.output.lower()


def test_the_two_refusals_differ(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: a test that only checks 'non-zero exit' would pass on the defect.
    The two failure messages are genuinely distinct, not the same text for both causes."""
    for name in ("GITHUB_ACTIONS", "GITLAB_CI"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(repo)

    # Branch A: no acknowledgement at all
    _commit(
        repo,
        ".lockstep/lockstep.py",
        "lockstep = 'rebound'\n",
        "feat: rebind Validate",
    )
    result_none = CliRunner().invoke(main, ["config", "--base", "main"])
    assert result_none.exit_code == 1

    # Reset: undo that commit so we can try a different one
    _git(repo, "reset", "--hard", "HEAD~1")

    # Branch B: acknowledgement outside trailer block
    _commit(
        repo,
        ".lockstep/lockstep.py",
        "lockstep = 'rebound'\n",
        (
            "feat: rebind Validate\n"
            "\n"
            f"{ACKNOWLEDGEMENT}: first run is main.\n"
            "\n"
            "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
        ),
    )
    result_near_miss = CliRunner().invoke(main, ["config", "--base", "main"])
    assert result_near_miss.exit_code == 1

    # The refusals must be different, because they diagnose different problems.
    assert result_none.output != result_near_miss.output
