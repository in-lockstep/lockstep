"""Materialise a staged change so it can be run.

A `ChangeSet` is a proposal: the files it describes are not on disk, so a test suite run against the
working tree measures the code as it was, not as it would be. Goal 5's own exemplar turns on this —
"nothing can test a staged ChangeSet" — and it is what a fix or TDD loop needs before it can see
whether a change works.

This builds the tree the change proposes — HEAD (or a named ref) plus the change — in a throwaway
git worktree, hands back its path, and tears it down. `ctx.do(Test(root=that))` then runs
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
`implement/from-ticket` runs Test in a container (slice 2, alongside item 14's sandbox work), the
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
from typing import Any

from ..core.types import ChangeSet, FileChange, Test, TestReport, TestVerdict


class WorktreeError(RuntimeError):
    """A worktree could not be created, or the source was not a git repository."""


class WorktreeRunner:
    """A `CommandRunner` that runs each command in a throwaway worktree of the repository.

    `run_script` lets a model run python/make/npm/… against the tree, and `Sandbox` drops the
    credentials from the child — but it bind-mounts the working tree **read-write**, so the command
    can still write `.git/hooks` or `.lockstep/lockstep.py` on the real repository, straight past the
    `Workspace` and `ChangeGuard` that govern the `write_file` tool. That is goal 8's one confirmed
    non-bypassability hole: what a *later* run executes, written by a command in *this* one.

    This closes it. Each command runs in a disposable worktree of HEAD, so its writes land in a copy
    that is discarded — the real `.git`, `.lockstep` and CI config are never in reach, and the copy
    is what the inner runner bind-mounts read-write. The inner runner's credential-drop and
    no-network still apply inside it; this only changes *where* it runs, not *how*.

    HEAD, not the staged change, on purpose: `run_script`'s documented job is to tell the model what
    the existing behaviour is, not whether its unstaged change works — `ctx.do(Test)` over a
    materialised change is for that. Each call gets a fresh copy, so nothing a command writes
    survives to the next; a step that needs its predecessor's output on disk is not what this is for.
    """

    def __init__(self, inner: Any, repo_root: str) -> None:
        self.inner = inner
        # The trusted repository root, decided at the binding site (`lockstep.repo.root`), not
        # anything a model can influence — this is what the throwaway worktree is a copy *of*, so a
        # value under an attacker's control would defeat the point. A non-git path fails loudly in
        # `materialize` rather than silently running somewhere else.
        self.repo_root = repo_root

    async def run(self, command: list[str], *, cwd: str | None = None, timeout: float = 900.0) -> Any:
        # `cwd` (the workspace root) is ignored deliberately: the whole point is to run somewhere
        # other than the live tree. The worktree is materialised from `repo_root` and thrown away.
        async with materialize(self.repo_root, ChangeSet()) as tree:
            return await self.inner.run(command, cwd=tree, timeout=timeout)


async def head_state(repo_root: str, paths: list[str], *, ref: str = "HEAD") -> dict[str, FileChange | None]:
    """The pre-image of each path at `ref`: a `FileChange` reproducing its committed contents, or
    None if it does not exist there. This is what `ChangeSet.inverse` needs to revert a change that
    was applied over `ref` — the "before" state lives in the tree, not in the change.

    Text-oriented, like the `FileChange.contents` it fills: contents are decoded with
    `errors="replace"`, the same choice `_git` and `Sandbox` make, because a `ChangeSet` cannot
    carry bytes in the first place. A repository whose *source* is non-UTF-8 is not something a
    change staged through the tool boundary could have produced.
    """
    # Option-confusion guard, as in `materialize`: `{ref}:{path}` reaches `git show` as one argv
    # token, so a `-`-leading ref could be read as a flag. Not injection (no shell), but refused.
    if ref.startswith("-"):
        raise WorktreeError(f"refusing a ref that looks like an option: {ref!r}")
    repo_root = os.path.abspath(repo_root)
    state: dict[str, FileChange | None] = {}
    for path in paths:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            repo_root,
            "show",
            f"{ref}:{path}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        # A non-zero exit means the path is absent at `ref` (a file the change created), which
        # inverts to a deletion — represented as None rather than an empty file.
        state[path] = (
            FileChange(path=path, contents=out.decode(errors="replace")) if not proc.returncode else None
        )
    return state


def staged_runner(ctx: Any, verb: type = Test) -> Any:
    """Where a model's staged work executes for `verb` (#419).

    **The binding's own if it names an image, else the workshop's.** A binding's `sandbox=` is an
    override for this path, not the only answer to it, because one binding serves callers with
    different requirements: `selfcheck` validates the working tree on the host, where this
    repository's own `make lint` works, and a model's staged change must be executed in a
    container. A `sandbox=` declared once cannot be both, and the workshop is what already means
    "where a model's work runs" -- `run_script` has been going through it all along.

    ONE function, and that is the point rather than tidiness. `staged_refusal` asks about a runner
    and the dispatch has to USE it; a call site free to resolve those separately is a control
    measuring something it does not govern, which this repository produced twice in a week
    (`GATE-READ-1` asserted one layer below delivery, #412; a pull request's container job verified
    the base branch's configuration rather than the change, #421).
    """
    container = getattr(ctx, "container", None)
    own = None
    if container is not None and container.has(verb):
        own = getattr(container.resolve(verb), "sandbox", None)
        if str(getattr(own, "image", "") or ""):
            return own
    # UNWRAPPED, and that is load-bearing rather than tidy. `Lockstep.use` wraps the workshop's
    # sandbox in a `WorktreeRunner`, whose documented job is to run each command in a throwaway
    # worktree of HEAD -- right for `run_script`, and wrong here: `prepared` has already built the
    # tree, environment and staged change included, and a second worktree of HEAD would check the
    # code as it was before the change. The sandbox that owns the image is `inner`. Unwrapped by
    # attribute rather than by type, so an adopter's own wrapper is treated the same way.
    workshop = getattr(ctx, "workshop_runner", None)
    inner = getattr(workshop, "inner", workshop)
    # And the binding's own where there is no workshop either -- not because it would be allowed,
    # but because it is what WOULD execute, and the refusal describes the runner it was handed. A
    # `None` here reads as "exposes no sandbox" about a binding that has one and simply names no
    # image, which sends somebody to fix the wrong line.
    return inner if inner is not None else own


def staged_refusal(ctx: Any, verb: type = Test) -> str | None:
    """Why the runner bound to `verb` may not be handed a MODEL-staged tree, or None when it may.

    One question, asked at every place a staged change is materialised for something that
    EXECUTES it -- `run_tests`, a strategy's red and green runs, the verdict below, and the build
    a strategy runs over what it staged -- and asked BEFORE the worktree exists. A staged file is
    code the model wrote on a ticket that is untrusted by construction, and `Sandbox()` with no
    image runs it as a subprocess on this host with `HOME` and an open socket (#308): `~/.ssh`
    readable, the persisted CI token in a linked worktree's `.git` file one `git config` away. The
    repository's OWN suite run by a person is a different situation and keeps the fallback; this is
    scoped to what a model staged. The reason is spelled so a refusal names the line to add,
    because a control that says "no" without saying what would make it "yes" is one somebody
    switches off.

    `verb` is a parameter rather than `Test` by name because the question was never about tests:
    it is about executing what a model wrote. A build runs the repository's own build scripts over
    model-authored source -- a `setup.py`, a `build.rs`, a postinstall -- which is the same
    exposure with a different file extension.

    Since #410 the check recognises adapters that declare they do NOT execute. A binding whose
    declared capabilities do not include `EXECUTES_CODE` has told the framework it only reads the
    tree -- `RuffValidate` is the canonical case: ruff is a binary that parses, its config is
    TOML rules not code, and refusing it would regress every repository that binds it.
    """
    from ..core.verbs import Capability, capabilities_of
    from .sandbox import host_fallback

    container = getattr(ctx, "container", None)
    if container is None or not container.has(verb):
        return None
    # `container.resolve` directly, not through a `getattr` that returns None when it is absent.
    # That spelling was added here so a test double implementing only `has` would not raise, and
    # it turned a crash into a silent pass on a security control: a container this code cannot
    # inspect is one it cannot clear, and the honest failure is loud. The real `Container` always
    # resolves, and a double that cannot has not modelled the thing under test (#410).
    adapter = container.resolve(verb)
    # A binding that does not declare EXECUTES_CODE needs no container: ruff is a binary that
    # parses, its config is TOML rules not code, and a model-staged `ruff.toml` changes which
    # rules run and nothing else.  Refusing it would break every repository that binds
    # `RuffValidate`.  `CommandValidate` defaults to EXECUTES_CODE since #410, because a
    # repository's own `make lint` runs recipes and a model can author the files those recipes
    # read -- `mypy.ini`, `eslint.config.js` -- so the deny list is not the fix; the container
    # is the control that does not depend on enumerating files.
    if Capability.EXECUTES_CODE not in capabilities_of(adapter):
        return None
    # The runner this asks about is the runner the dispatch uses, because both call `staged_runner`
    # (#419). Asking the binding here while the workshop executed would be the defect this control
    # exists to prevent, wearing the control's own clothes.
    why = host_fallback(staged_runner(ctx, verb), named=verb.__name__)
    if why is None:
        return None
    named = verb.__name__
    return (
        f"{why}. What a model staged is executed only in a container: bind {named} with "
        f'Sandbox(image="...", require_container=True), with `mounts=` for the environment it '
        f"needs (see docs/extending.md), or run this verb where a container runtime is."
    )


async def verdict_over_staged(ctx: Any, repo_root: str, changeset: ChangeSet) -> TestVerdict | None:
    """Run the bound suite against HEAD-plus-`changeset`, and report how it came out.

    None when no Test verb is bound — an honest "unverified", never a silent green. This is what
    lets an implement run see whether its own change works before proposing it: the change is not on
    disk, so materialising it into a throwaway worktree is the only way to run a suite over it, and
    the worktree is discarded so nothing the suite does reaches the real tree.

    A runner that would put the staged files on this host is a `blocked` verdict rather than a
    run: the worktree is never made, and the verdict says `sandbox.host_fallback` so the
    proposal it rides into reads as a control working rather than a suite that failed.
    """
    if not ctx.container.has(Test):
        return None
    if staged_refusal(ctx) is not None:
        # The reason is not carried: `TestVerdict` is counts and a status so it serialises on the
        # redacted side of the artifact, and `implement_body` renders `blocked` as the sentence.
        return TestVerdict.of("blocked", False, TestReport())
    async with materialize(repo_root, changeset) as tree:
        outcome = await ctx.do(Test(root=tree, runner=staged_runner(ctx, Test)))
    report = outcome.value if outcome.value is not None else TestReport()
    # The paths this change staged, so the verdict can tell a change that fails its own tests from
    # a suite that is red somewhere else. The two are one number apart and mean opposite things.
    return TestVerdict.of(
        outcome.status.value, outcome.decided, report, changed=tuple(c.path for c in changeset.changes)
    )


async def staged_diff(repo_root: str, changeset: ChangeSet, *, ref: str = "HEAD") -> str:
    """A unified diff of `changeset` against `ref`, as a reader would see it on a pull request.

    The change is a set of whole file contents, which is what a model wrote and not what a reviewer
    reads. `Describe` is given this instead, because a description written from whole files
    describes the files; one written from a diff describes the change.

    `git add -A` first, in the throwaway worktree and nowhere else: a file the change CREATES is
    untracked, and `git diff` alone would leave the new files -- usually the most interesting part
    -- out of the document the description is written from.
    """
    async with materialize(repo_root, changeset, ref=ref) as tree:
        await _git(tree, "add", "-A")
        return await _git(tree, "diff", "--cached")


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
        # Permission bits only. A test run needs the execute bit and nothing above it, so mask off
        # setuid/setgid/sticky: harmless in a user-owned throwaway tree, but a change has no reason
        # to ask for them and a materialiser has no reason to grant a bit it will never need.
        os.chmod(target, change.mode & 0o777)


#: Files whose change could move the environment the checks run in. NOT the question
#: `detected_bindings` asks -- that one picks a provisioner from a lockfile that exists -- but the
#: wider "might this staged change have altered what `Provision` installs". Deliberately a superset:
#: a false positive costs one sentence in a report, and a false negative is a check that fails on a
#: missing import while the model is told its code is wrong (#422).
DEPENDENCY_MANIFESTS = frozenset(
    {
        "uv.lock",
        "poetry.lock",
        "pdm.lock",
        "Pipfile.lock",
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Gemfile",
        "Gemfile.lock",
        "go.mod",
        "go.sum",
        "Cargo.toml",
        "Cargo.lock",
    }
)


async def _install(ctx: Any, tree: str, for_verb: type) -> str:
    """Build the repository's own environment in `tree`. Returns a note, empty when all is well.

    **Where it builds is the point (#424).** One `Provision` binding serves two callers that need
    different environments: `in-lockstep provision` needs one on the host, at the repository root,
    built by the runner's interpreter, because every later `uv run in-lockstep …` step uses it;
    a check needs one inside its own image, in the worktree, because `uv run mypy` executes a
    console script whose shebang carries an absolute path. A `sandbox=` is declared once and cannot
    be both, and giving the binding an image containerises the CI step instead -- which is #419's
    mechanism arriving from the other side.

    So the steps are the binding's and the PLACE is the check's. Where the verb being prepared for
    names an image, its own sandbox runs the install, with the network open for that step alone:
    same image, same mounts, so what is installed is what the checks will find. `steps` is read the
    way `fixes` and `sandbox` already are, by asking rather than by knowing the class -- and an
    adapter that exposes none, or a check that names no image, falls back to the bound `Provision`
    through `ctx.do`, which is the host path and says so.
    """
    from ..core.outcome import Status
    from ..core.types import Provision

    container = getattr(ctx, "container", None)
    if container is None or not container.has(Provision):
        # O1: nothing is invented where the repository declared nothing. The checks still run,
        # against whatever the image carries, and the caller is told which it was.
        return "no Provision is bound, so the checks ran against whatever the image carries"

    steps = tuple(getattr(container.resolve(Provision), "steps", ()) or ())
    # The same resolution the refusal and the dispatch use, so the environment is built where the
    # checks will read it (#419).
    where = staged_runner(ctx, for_verb)
    if steps and str(getattr(where, "image", "") or ""):
        return await _install_where_the_checks_run(where, steps, tree)

    outcome = await ctx.do(Provision(root=tree))
    if getattr(outcome, "status", Status.SUCCEEDED) is not Status.SUCCEEDED:
        return (
            "the repository's own environment was not installed "
            f"({getattr(outcome, 'reason', '') or outcome.status.value}), so a failure below may "
            "be about the environment rather than about the change"
        )
    return ""


async def _install_where_the_checks_run(sandbox: Any, steps: tuple[Any, ...], tree: str) -> str:
    """The repository's install steps, in the check's own sandbox, over `tree`.

    `replace(..., allow_network=True)` rather than a sandbox of our own: the image, the mounts and
    the environment are the ones the checks will run under, and only the network differs. It is
    open here and closed for the checks, and the tree holds only `ref` while it is open -- which is
    the ordering `prepared` exists for, and the reason this is not an allowlist.
    """
    from dataclasses import replace

    networked = replace(sandbox, allow_network=True)
    for step in steps:
        result = await networked.run(list(step), cwd=tree)
        if getattr(result, "exit_code", 0) != 0:
            said = (getattr(result, "stderr", "") or getattr(result, "stdout", "")).strip()
            return (
                f"the repository's own environment was not installed: {' '.join(step)} exited "
                f"{result.exit_code}{f' ({said[-300:]})' if said else ''}, so a failure below may "
                "be about the environment rather than about the change"
            )
    return ""


@asynccontextmanager
async def prepared(
    ctx: Any, repo_root: str, changeset: ChangeSet, *, for_verb: type, ref: str = "HEAD"
) -> AsyncIterator[tuple[str, str]]:
    """A worktree of `ref` with the repository's own environment installed, and THEN the staged
    change applied. Yields the tree and a note about the environment, empty when there is nothing
    to say.

    **The order is the security property** (#422). `Provision` reaches a registry, so it runs with
    the network open -- and it runs over a tree holding only `ref`, which is reviewed code. By the
    time anything a model wrote is on disk the provisioning run has ended and the checks run under
    the validator's own sandbox, which denies the network. Nothing here depends on an allowlist of
    hosts, which is as well: `Sandbox.allow_network` is one boolean that removes `--network=none`,
    and per-host rules are a proxy neither runtime offers as a flag.

    Provisioning the STAGED tree instead would be arbitrary code execution with a supply chain
    attached, and not hypothetically: `uv.lock`, `poetry.lock`, `requirements.txt`, `pyproject.toml`
    and `Makefile` are tier-1 denied to a model, and `package.json` and `package-lock.json` are NOT.
    A Node repository provisioning what a model staged would run `npm ci` over a manifest the model
    wrote, with the network open, executing whatever postinstall scripts it named. The `ref`
    argument is the whole control, which is why a test drives the staged case rather than reading
    this paragraph.

    One tree, not two, and no volume: the environment `Provision` builds lands INSIDE the worktree
    (`.venv`, `node_modules`), so mounting the same tree again in the check run finds it with the
    paths it was built with. That is also what #419 was missing -- a venv built on the host carries
    the host's absolute paths in its console-script shebangs, and nothing mounted makes those exist
    inside an image.
    """
    async with materialize(repo_root, ChangeSet(), ref=ref) as tree:
        note = await _install(ctx, tree, for_verb)
        # AFTER provisioning, never before. See the docstring.
        for change in changeset.changes:
            _apply_change(Path(tree), change)
        if moved := tuple(c.path for c in changeset.changes if Path(c.path).name in DEPENDENCY_MANIFESTS):
            # Named rather than silently misleading: the environment came from `ref`, so a
            # dependency this change ADDS is not in it, and the check fails on a missing import
            # while looking like the change is wrong.
            note = (
                f"{note}. " if note else ""
            ) + f"this change touches {', '.join(sorted(moved))}, and the environment came from {ref}"
        yield str(tree), note


@asynccontextmanager
async def materialize(repo_root: str, changeset: ChangeSet, *, ref: str = "HEAD") -> AsyncIterator[str]:
    """`ref` (default HEAD) plus `changeset`, in a throwaway worktree. Yields its path; removes it.

    Use as `async with materialize(root, changeset) as tree: await ctx.do(Test(root=tree))`.
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
