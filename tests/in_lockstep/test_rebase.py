"""Bringing a stale change forward, deterministically.

Real git repositories throughout. A rebase that is faked tests nothing: what these protect is
exactly the behaviour git has and the wrapper must not obscure — that a clean replay moves the
commits, that a conflict is left where somebody can act on it, and that the retreat works.

The wider goal is a `/rebase` verb. This is its deterministic half, which is most of it: the same
claim `adapters/backport.py` makes about cherry-picking, that replaying an existing change is not an
AI problem and a model is needed at exactly one point.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from in_lockstep.platform.scm.base import GitLocal


def _run(root: Path, *args: str) -> None:
    subprocess.run(args, cwd=root, capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with `main` at one commit, and nothing else."""
    root = tmp_path / "r"
    root.mkdir()
    _run(root, "git", "init", "-q")
    _run(root, "git", "config", "user.email", "t@example.test")
    _run(root, "git", "config", "user.name", "t")
    (root / "shared.txt").write_text("one\n")
    _run(root, "git", "add", "-A")
    _run(root, "git", "commit", "-q", "-m", "base")
    _run(root, "git", "branch", "-M", "main")
    return root


def _branch_from(root: Path, name: str, at: str = "main") -> None:
    _run(root, "git", "checkout", "-q", "-b", name, at)


def _commit(root: Path, path: str, text: str, message: str) -> None:
    (root / path).write_text(text)
    _run(root, "git", "add", "-A")
    _run(root, "git", "commit", "-q", "-m", message)


def _advance_main(root: Path, path: str, text: str, message: str) -> None:
    """Move `main` on, then return to whatever branch was checked out."""
    was = GitLocal(root).current_branch()
    _run(root, "git", "checkout", "-q", "main")
    _commit(root, path, text, message)
    _run(root, "git", "checkout", "-q", was)


def _subjects(root: Path, ref: str = "HEAD") -> list[str]:
    out = subprocess.run(
        ["git", "log", "--format=%s", ref], cwd=root, capture_output=True, text=True, check=True
    )
    return out.stdout.split()


# -- the clean path --------------------------------------------------------------------------


def test_a_branch_that_does_not_overlap_replays_onto_the_base(repo: Path) -> None:
    """The ordinary case, and the one that costs nothing: git does all the work."""
    _branch_from(repo, "feature")
    _commit(repo, "mine.txt", "mine\n", "feat: mine")
    _advance_main(repo, "theirs.txt", "theirs\n", "feat: theirs")

    scm = GitLocal(repo)
    before = scm.head()
    after = scm.rebase_onto("main")

    assert after != before, "a replayed commit is a new commit"
    assert scm.head() == after, "the returned sha is the tree's actual head"
    assert (repo / "theirs.txt").exists(), "the base's work is now underneath"
    assert (repo / "mine.txt").exists(), "and so is ours"


def test_the_rebased_commits_sit_on_top_of_the_base(repo: Path) -> None:
    """ "Brought forward" means the base is an ancestor. A rebase that left the branch beside its
    target would still change the sha and still be wrong."""
    _branch_from(repo, "feature")
    _commit(repo, "mine.txt", "mine\n", "feat:mine")
    _advance_main(repo, "theirs.txt", "theirs\n", "feat:theirs")

    GitLocal(repo).rebase_onto("main")

    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "main", "HEAD"], cwd=repo, capture_output=True
    )
    assert ancestry.returncode == 0, "main is not an ancestor of the rebased branch"
    assert _subjects(repo)[0] == "feat:mine", "our commit is on top"


def test_a_clean_tree_reports_no_conflicts(repo: Path) -> None:
    assert GitLocal(repo).unmerged_paths() == ()


# -- the conflict, which is the interesting one -----------------------------------------------


def _conflicting(repo: Path) -> GitLocal:
    """A branch and a base that edited the same line of the same file."""
    _branch_from(repo, "feature")
    _commit(repo, "shared.txt", "ours\n", "feat: ours")
    _advance_main(repo, "shared.txt", "theirs\n", "feat: theirs")
    return GitLocal(repo)


def test_a_conflict_raises_and_carries_gits_own_message(repo: Path) -> None:
    """Not a return value. A caller that ignored a boolean would carry on with a half-rebased tree,
    and the message is what tells a person which file to look at."""
    scm = _conflicting(repo)
    with pytest.raises(RuntimeError) as raised:
        scm.rebase_onto("main")
    assert "rebase" in str(raised.value)
    assert "shared.txt" in str(raised.value) or "conflict" in str(raised.value).lower()


def test_the_conflicted_tree_is_left_for_somebody_to_act_on(repo: Path) -> None:
    """The deliberate part, and the reason this does not clean up after itself: `cherry_pick` gives
    it directly above — resolving a conflict is a decision, and tidying it away here would discard
    exactly what the decision needs. It is also what a `ConflictResolver` will later read."""
    scm = _conflicting(repo)
    with pytest.raises(RuntimeError):
        scm.rebase_onto("main")

    assert scm.unmerged_paths() == ("shared.txt",)


def test_the_retreat_returns_the_tree_to_where_it_was(repo: Path) -> None:
    scm = _conflicting(repo)
    before = scm.head()
    with pytest.raises(RuntimeError):
        scm.rebase_onto("main")

    scm.abort_rebase()

    assert scm.unmerged_paths() == (), "no conflict is outstanding"
    assert scm.head() == before, "and the branch is where it started"
    assert (repo / "shared.txt").read_text() == "ours\n"


def test_aborting_when_nothing_is_in_progress_is_not_an_error(repo: Path) -> None:
    """Being careful is not being wrong. Raising here would make the safe spelling the awkward one,
    and a caller unsure whether a rebase started would have to guess instead of just retreating."""
    GitLocal(repo).abort_rebase()


def test_conflicted_paths_come_back_sorted(repo: Path) -> None:
    """Two reads of one tree should not differ by however the index happened to be walked — this is
    read to describe a state to a person, and to a resolver that will act on it."""
    _branch_from(repo, "feature")
    _commit(repo, "shared.txt", "ours\n", "feat: ours a")
    (repo / "b.txt").write_text("ours\n")
    (repo / "a.txt").write_text("ours\n")
    _run(repo, "git", "add", "-A")
    _run(repo, "git", "commit", "-q", "-m", "feat: ours b")

    _run(repo, "git", "checkout", "-q", "main")
    for name in ("shared.txt", "b.txt", "a.txt"):
        (repo / name).write_text("theirs\n")
    _run(repo, "git", "add", "-A")
    _run(repo, "git", "commit", "-q", "-m", "feat: theirs")
    _run(repo, "git", "checkout", "-q", "feature")

    scm = GitLocal(repo)
    with pytest.raises(RuntimeError):
        scm.rebase_onto("main")

    paths = scm.unmerged_paths()
    assert list(paths) == sorted(paths)
    assert len(paths) >= 1


# -- the base ref -----------------------------------------------------------------------------


def test_a_base_that_looks_like_an_option_is_refused(repo: Path) -> None:
    """`start_point`'s guard, reached through this path too. A ref never legitimately begins with a
    dash, and this one becomes a git argument."""
    _branch_from(repo, "feature")
    _commit(repo, "mine.txt", "mine\n", "feat: mine")

    with pytest.raises(RuntimeError, match="looks like an option"):
        GitLocal(repo).rebase_onto("--exec=touch /tmp/pwned")
