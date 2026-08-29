"""Materialise a staged change so it can be run.

A `ChangeSet` is a proposal: the files it describes are not on disk, so a test suite run against the
working tree measures the code as it was, not as it would be. Goal 5's own exemplar turns on this —
"nothing can test a staged ChangeSet" — and it is what a fix or TDD loop needs before it can see
whether a change works.

This builds the tree the change proposes — HEAD (or a named ref) plus the change — in a throwaway
git worktree, hands back its path, and tears it down. `ctx.do(Test, TestSpec(root=that))` then runs
the suite against the change without the change ever touching the real working tree.

A throwaway worktree, not an in-place apply, on purpose:

- The developer's own working tree keeps whatever is in it; a materialisation that failed halfway
  leaves nothing behind to reconcile.
- The ref is checked out committed, so a proposed change is tested against a clean base rather than
  against whatever else happens to be uncommitted locally.
- A suite that a model's change tells it to run — `python`, `make`, a `conftest.py` — writes into
  the copy, not into `.git/hooks` or `.lockstep/lockstep.py`. That is the write path `ChangeGuard`
  governs at propose time but cannot see once a process is executing, so the disposable tree is the
  boundary that makes running a proposed change safe to do at all.

One honest caveat, for the container execution path. A linked worktree's `.git` is a *file*
pointing at `<repo>/.git/worktrees/<id>`, a path outside the tree itself. `Sandbox`'s container
path bind-mounts only the run cwd, so inside a container that gitlink dangles: a normal suite runs
(pytest does not read `.git`), but git-dependent tooling in the suite — `setuptools_scm`, a
coverage git integration — cannot resolve the repository. The subprocess path is unaffected. When
`implement/from-issue` runs Test in a container (slice 2, alongside item 14's sandbox work), the
mount has to reach the gitlink or the tree has to be self-contained; until then this is a worktree
run against a container, not a claim that git works inside one.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from ..core.types import ChangeSet, FileChange


class WorktreeError(RuntimeError):
    """A worktree could not be created, or the source was not a git repository."""


async def _git(repo_root: str, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        repo_root,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode:
        raise WorktreeError(
            f"git {' '.join(args)} failed ({proc.returncode}): {err.decode(errors='replace').strip()}"
        )
    return out.decode(errors="replace")


def _within(root: Path, candidate: Path) -> bool:
    """Whether `candidate` is `root` itself or lives under it, compared on resolved paths.

    `resolve()` collapses `..` and follows any symlink in an existing prefix, so this holds against
    both a `../` path and a symlink planted earlier in the same tree. A path that does not exist yet
    still resolves lexically, which is what a change writing a new file needs.
    """
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _safe_target(worktree: Path, rel_path: str) -> Path:
    """The path a change writes to, refused if it escapes the worktree.

    `ChangeGuard` already refuses an out-of-root path upstream, but materialisation is a second
    write point and a rule enforced at one point is enforced at none — a change that reached here
    with a `..` in it must not land outside the disposable tree.
    """
    target = worktree / rel_path
    if not _within(worktree, target):
        raise WorktreeError(
            f"change path {rel_path!r} escapes the worktree — refusing to write outside the "
            f"materialised tree."
        )
    return target


def _apply_change(worktree: Path, change: FileChange) -> None:
    target = _safe_target(worktree, change.path)
    # Symlinks first: a symlink change carries `symlink_target` and no `contents`, so it would read
    # as a deletion under the check below.
    if change.symlink_target is not None:
        # Guard the *target*, not just the link's own path: a symlink is a write that lands where it
        # points, and one aimed at `/etc/passwd` or `../../secret` is the "out-of-root write next
        # turn" ChangeGuard exists to stop. Resolve it from the link's own directory the way the
        # filesystem will, so a relative target and an absolute one are both checked.
        destination = target.parent / change.symlink_target
        if not _within(worktree, destination):
            raise WorktreeError(
                f"symlink {change.path!r} -> {change.symlink_target!r} points outside the "
                f"worktree — refusing to plant an escaping link."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            target.unlink()
        target.symlink_to(change.symlink_target)
        # The pre-check above is lexical; this is the authoritative one. `resolve()` on the created
        # link follows it — and any in-tree symlink an earlier change planted — to its real
        # destination, so a link that reaches outside through another link is caught here even if
        # the lexical check could not see it. Undo it before refusing; nothing escaping stays.
        if not _within(worktree, target):
            target.unlink()
            raise WorktreeError(
                f"symlink {change.path!r} -> {change.symlink_target!r} resolves outside the "
                f"worktree — refusing to plant an escaping link."
            )
        return
    if change.deleted:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(change.contents or "")
    if change.mode is not None:
        os.chmod(target, change.mode)


@asynccontextmanager
async def materialize(repo_root: str, changeset: ChangeSet, *, ref: str = "HEAD") -> AsyncIterator[str]:
    """`ref` (default HEAD) plus `changeset`, in a throwaway worktree. Yields its path; removes it.

    Use as `async with materialize(root, changeset) as tree: await ctx.do(Test, TestSpec(root=tree))`.
    """
    # `ref` is an argv token to `git` (no shell), so this is option-confusion, not injection: a ref
    # like `--lock` would be read by `git worktree add` as a flag. A commit-ish never begins with a
    # dash, so refuse one that does rather than let a base ref a caller took from a ticket steer the
    # command.
    if ref.startswith("-"):
        raise WorktreeError(f"refusing a ref that looks like an option: {ref!r}")
    # Absolute, so `git -C <repo_root>` cannot read a `-`-leading path as an option, and so a
    # relative root resolves once here rather than against each subprocess's cwd. Mirrors how
    # `Sandbox` absolutises its mount source for the same reason.
    repo_root = os.path.abspath(repo_root)
    parent = Path(tempfile.mkdtemp(prefix="in-lockstep-worktree-"))
    # A child that does not exist yet: `git worktree add` creates the leaf and refuses a path that
    # is already populated, and `mkdtemp` hands back an existing directory.
    tree = parent / "tree"
    try:
        try:
            await _git(repo_root, "worktree", "add", "--detach", "--quiet", str(tree), ref)
        except WorktreeError as e:
            raise WorktreeError(
                f"could not materialise a worktree from {repo_root!r} at {ref!r}: {e}. Testing a "
                f"staged change needs {repo_root!r} to be a git repository with {ref!r} committed."
            ) from e
        for change in changeset.changes:
            _apply_change(tree, change)
        yield str(tree)
    finally:
        # Remove through git so the administrative entry under `.git/worktrees` goes too; `prune`
        # and `rmtree` then cover a git that could not (a deleted `.git`, an interrupted add), so a
        # temp tree never leaks even when the repository is in a strange state.
        try:
            await _git(repo_root, "worktree", "remove", "--force", str(tree))
        except WorktreeError:
            pass
        try:
            await _git(repo_root, "worktree", "prune")
        except WorktreeError:
            pass
        shutil.rmtree(parent, ignore_errors=True)
