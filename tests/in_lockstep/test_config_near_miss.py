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
from in_lockstep.config_ref import ACKNOWLEDGEMENT, acknowledgement, near_miss_acknowledgement
from in_lockstep.loader import unexercised


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
    # This refusal must not claim one was FOUND -- that is the other diagnosis, and telling a
    # person their trailer is misplaced when they wrote none sends them looking for something that
    # is not there.
    #
    # It DOES carry the syntax rule, and asserting its absence was the wrong property to pin: the
    # two refusals are distinguished by their remedy -- add one, versus move the one you wrote --
    # not by which of them is allowed to explain where a trailer goes. Pinning it the other way
    # locked in the half of #444 that says the clause belongs in the refusal a person reads first.
    assert "was found in the commit message" not in result.output


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


def test_the_scan_agrees_with_git_about_a_folded_value(repo: Path) -> None:
    """A trailer whose reason sits on a continuation line is one git reads, so it has to be one
    this scan reads too.

    The two readers disagreeing about what a VALUE is has the same consequence as disagreeing about
    what a trailer is: correctly placed, this shape clears the check, because git's `unfold` joins
    the continuation; misplaced, reading only the first line found an empty value and reported that
    nothing had been written. A person who wrote an acknowledgement being told they did not is the
    one sentence this whole function exists to stop printing.
    """
    _commit(
        repo,
        ".lockstep/lockstep.py",
        "lockstep = 'rebound'\n",
        (
            "feat: rebind Validate\n"
            "\n"
            f"{ACKNOWLEDGEMENT}:\n"
            " the whole reason lives on the continuation line\n"
            "\n"
            "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
        ),
    )

    assert near_miss_acknowledgement(repo, "main") == "the whole reason lives on the continuation line"


def test_a_folded_value_placed_correctly_still_clears_the_check(repo: Path) -> None:
    """The other half of the pair, and the reason the one above matters: git accepts this shape, so
    the scan reporting it absent was the scan being wrong rather than the commit being wrong."""
    _commit(
        repo,
        ".lockstep/lockstep.py",
        "lockstep = 'rebound'\n",
        (
            "feat: rebind Validate\n"
            "\n"
            f"{ACKNOWLEDGEMENT}:\n"
            " the whole reason lives on the continuation line\n"
            "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
        ),
    )

    found = unexercised(repo, base="main")

    assert found is not None and found.cleared
    assert found.acknowledged == "the whole reason lives on the continuation line"


def test_the_scan_stops_at_the_end_of_the_folded_value(repo: Path) -> None:
    """The negative control for the fold: an unindented line ends the value, so the scan does not
    swallow the rest of the message and report a paragraph as the reason."""
    _commit(
        repo,
        ".lockstep/lockstep.py",
        "lockstep = 'rebound'\n",
        (
            "feat: rebind Validate\n"
            "\n"
            f"{ACKNOWLEDGEMENT}: the reason\n"
            " and its continuation\n"
            "NOT part of the value\n"
            "\n"
            "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
        ),
    )

    assert near_miss_acknowledgement(repo, "main") == "the reason and its continuation"


def test_the_first_refusal_says_where_the_trailer_has_to_go(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal a person meets BEFORE they have written anything carries the syntax rule.

    Saying it only in the near-miss teaches it one round trip late, and the trip is not hypothetical:
    this repository requires `Co-Authored-By:` on every commit, so the natural place to write the
    acknowledgement -- after the prose, above the sign-off, in a paragraph of its own -- is exactly
    the place git does not read it. Two people hit that on #445 and #446 before the check could say
    so (#444).
    """
    for name in ("GITHUB_ACTIONS", "GITLAB_CI"):
        monkeypatch.delenv(name, raising=False)
    _commit(repo, ".lockstep/lockstep.py", "lockstep = 'rebound'\n", "feat: rebind Validate")
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(main, ["config", "--base", "main"])

    assert result.exit_code == 1
    said = result.output.lower()
    assert "trailer block" in said or "last paragraph" in said, (
        "the first refusal does not say where the trailer has to go, so a person learns it only "
        "after getting it wrong"
    )
    assert "co-authored-by" in said, "it names the line the trailer has to sit beside"
