"""Where configuration is loaded from.

Configuration is code, discovered at the repository root. Under review, that root is the pull
request's — which would mean the change being reviewed supplies the module defining every binding,
every policy contribution, the egress mode, and the protected-path tiers that are supposed to
constrain reviewing it.

The compiler's arrangement did not have this problem, and its documentation says why in one line:
the workflow that runs is the one on the default branch, so a fork cannot modify the workflow that
reviews it. Nothing about "runnable, never rendered" requires giving that up — it only requires
being explicit about which ref configuration comes from.

So: content under review comes from head, and the lifecycle module (`lockstep.py` under either
spelling) comes from a trusted ref. Protecting `lockstep.py` from *agent* writes does not address
this at all; the change here is human-authored by construction.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

#: The directory this control is ABOUT. Not the same thing as the set of files it actually reads
#: from the trusted ref, which is `loader.TRUSTED_REF_FILES` -- two files, both of them the
#: lifecycle module under one spelling or the other.
#:
#: The difference is written down rather than papered over. `.lockstep/market.toml` says which
#: catalogs this repository pulls packs from and `.lockstep/packs/<name>.json` records what a pack
#: was allowed to do, and both are read from the working tree, which under review is the change.
#: Whether they should move behind this control or the comment should narrow is #431; what must
#: not happen meanwhile is a reader taking the old wording -- "everything loaded from the trusted
#: ref" -- at face value.
CONFIG_PATHS = (".lockstep",)


class UntrustedConfig(Exception):
    """Configuration would resolve from the ref under review."""


class UnresolvableConfigRef(Exception):
    """The trusted ref does not name a commit here.

    Loud, and that is the point. This used to be indistinguishable from "the repository has no
    configuration": both produced `None`, both fell through to detected defaults, and a review
    then ran with none of the repository's bindings, policy or egress opt-out. A provenance
    control that degrades to *no configuration* in the one environment it exists for has failed
    open — which is worse than never having been wired, because the crosswalk records it as a
    replacement for gh-aw's workflow-file provenance.

    The case that produced it: `GITHUB_BASE_REF` is `main`, and an `actions/checkout` working
    directory is a detached HEAD with `origin/main` but no local `main` branch, so
    `git show main:lockstep.py` fails for a reason that has nothing to do with the file.
    """


@dataclass(frozen=True)
class ConfigRef:
    """The ref configuration is read from, and why."""

    ref: str
    reason: str
    trusted: bool = True

    @classmethod
    def local(cls) -> ConfigRef:
        """Working tree. Correct for a developer running against their own checkout."""
        return cls(ref="", reason="local working tree", trusted=True)

    @classmethod
    def base(cls, ref: str) -> ConfigRef:
        return cls(ref=ref, reason="base branch (not the ref under review)", trusted=True)


def read_config(repo_root: str | Path, path: str, ref: ConfigRef) -> str | None:
    """Read one configuration file from the trusted ref rather than the working tree."""
    if not ref.trusted:
        raise UntrustedConfig(
            f"refusing to load {path} from {ref.ref!r}: {ref.reason}. Configuration defines the "
            "bindings, policy and path tiers that constrain this run; loading it from the change "
            "under review would let that change rewrite its own constraints."
        )
    if not ref.ref:
        candidate = Path(repo_root) / path
        return candidate.read_text() if candidate.exists() else None

    resolved = _resolve_commit(repo_root, ref.ref)
    if resolved is None:
        raise UnresolvableConfigRef(
            f"the trusted ref {ref.ref!r} does not name a commit in this checkout, so "
            f"configuration cannot be read from it. Nothing was loaded, and continuing would run "
            f"with none of this repository's bindings, policy or egress decisions. In CI, fetch "
            f"enough history for the base branch — `actions/checkout` with `fetch-depth: 0` on "
            f"GitHub, `GIT_DEPTH: 0` (or `git fetch origin {ref.ref}`) on GitLab."
        )
    return _show(repo_root, f"{resolved}:{path}")


def _candidates(ref: str) -> tuple[str, ...]:
    """Full ref paths first, then the ref as written.

    A developer has a local `main`; a CI checkout usually does not — it is a detached HEAD with
    `origin/main` and nothing else. Trying only the bare name is what made this control silently
    inapplicable in CI, which is the only place it does any work.

    The fix for that was `(ref, f"origin/{ref}")` guarded by `"/" not in ref`, and the guard was
    the bug. It was there for a real reason — `origin/origin/main` is not a spelling of anything —
    but a slash does not mean *already qualified*. It also means *a branch with a slash in its
    name*, which is what every stacked pull request has: `GITHUB_BASE_REF` is `feat/first`, the
    candidate list collapsed to `("feat/first",)`, no local branch of that name exists in a
    detached checkout, and the run died claiming the base was not fetched. It was fetched. Nothing
    ever looked at `refs/remotes/origin/feat/first`.

    Naming the full paths says what each candidate means instead of inferring it from punctuation,
    so a slash carries no meaning here at all:

    - `refs/heads/<ref>` — a local branch, which is a developer's checkout.
    - `refs/remotes/origin/<ref>` — the remote-tracking branch, which is CI's. `origin` because
      that is what `actions/checkout` and GitLab's clone both name their remote; a repository
      whose remote is called something else passes a ref that resolves as written.
    - `<ref>` — as written, last: a SHA, a tag, an already-qualified `refs/...` path, or the
      `origin/main` spelling somebody typed by hand.

    Last rather than first, so a branch beats a same-named tag. A base ref means the branch.
    """
    return (f"refs/heads/{ref}", f"refs/remotes/origin/{ref}", ref)


def _resolve_commit(repo_root: str | Path, ref: str) -> str | None:
    """The commit a trusted ref names, or None if no spelling of it resolves."""
    for candidate in _candidates(ref):
        out = _run(repo_root, ["git", "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"])
        if out is not None and out.strip():
            return out.strip()
    return None


#: The trailer that acknowledges a configuration change going in without CI having run it.
#:
#: A commit trailer rather than a line in the change request's body, and the reason is what happens
#: after the merge. A body is edited by anyone with write access and disappears entirely into a
#: squash-merge; a trailer travels with the commit into the default branch's history, which is
#: where somebody asking "why did this go in unexercised" is looking. It also re-runs the check on
#: the next push without the workflow having to subscribe to `edited` events.
ACKNOWLEDGEMENT = "Unexercised-Config"


def changed_paths(repo_root: str | Path, base: str) -> tuple[str, ...]:
    """Every path this checkout changes against `base`, or nothing when the range is unreadable.

    Three dots, not two. `base..HEAD` names every commit reachable from HEAD and not from base,
    which after the base branch moves on includes nobody's work but its own -- so a file somebody
    else changed on `main` would be reported as touched by this change. `base...HEAD` diffs from
    the merge base, which is the change's own files and the question being asked.

    Empty rather than an exception when the ref does not resolve: this answers "what did this
    change touch", and a caller that needs the ref to be readable is `read_config`, which refuses
    loudly about it already. Two refusals for one unfetched base would be one of them noise.
    """
    resolved = _resolve_commit(repo_root, base)
    if resolved is None:
        return ()
    out = _run(repo_root, ["git", "diff", "--name-only", f"{resolved}...HEAD"])
    return tuple(line.strip() for line in (out or "").splitlines() if line.strip())


def acknowledgement(repo_root: str | Path, base: str, key: str = ACKNOWLEDGEMENT) -> str:
    """The reason a commit in this range gives for going in unexercised, or empty for none.

    Git does the parsing, deliberately. `%(trailers:key=...)` knows where a trailer block begins,
    which lines continue the one above them, and that `Key:value` and `Key: value` are the same
    trailer -- and `platform/scm/base.py` already reads trailers this way. A regex here would be a
    second spelling of a format neither of us owns, which is the drift the ledger module documents
    at length.

    The first non-empty one wins rather than the last, because a range with two of them is a
    person having said it once and then rebased; either sentence is the sentence.
    """
    resolved = _resolve_commit(repo_root, base)
    if resolved is None:
        return ""
    out = _run(
        repo_root,
        ["git", "log", f"{resolved}..HEAD", f"--format=%(trailers:key={key},valueonly,unfold)%x1e"],
    )
    for record in (out or "").split("\x1e"):
        # Whitespace-collapsed: an unfolded trailer arrives with its continuation lines still
        # newline-separated, and this is rendered on one line and compared against emptiness.
        reason = " ".join(record.split())
        if reason:
            return reason
    return ""


def near_miss_acknowledgement(repo_root: str | Path, base: str, key: str = ACKNOWLEDGEMENT) -> str:
    """A line that looks like an acknowledgement but is not in git's trailer block.

    When `acknowledgement()` returns empty, this scans the raw commit messages for a line
    beginning with `key:`. If one is found, it was written but placed outside the final
    paragraph — typically separated from `Co-Authored-By:` by a blank line — so git reads it
    as prose rather than a trailer. The return is the value part -- everything after `key:`, plus
    any indented continuation lines, which is how git's `unfold` reads one -- or empty for none.

    This is a diagnostic aid, not a second trailer parser. Git's `%(trailers:key=...)` remains
    the only thing that decides whether a trailer IS a trailer; this just tells a person "you
    wrote it, but git cannot see it" instead of "you did not write it".
    """
    resolved = _resolve_commit(repo_root, base)
    if resolved is None:
        return ""
    # If git already sees a proper trailer, there is no near-miss.
    if acknowledgement(repo_root, base, key):
        return ""
    # Read every raw commit body in the range.
    out = _run(
        repo_root,
        ["git", "log", f"{resolved}..HEAD", "--format=%B%x1e"],
    )
    prefix = f"{key}:"
    for record in (out or "").split("\x1e"):
        lines = record.splitlines()
        for index, line in enumerate(lines):
            if not line.strip().startswith(prefix):
                continue
            # The value CONTINUES onto following indented lines, the way git's `unfold` reads one.
            # Reading the first line alone disagreed with git about what an acknowledgement is: a
            # trailer whose whole reason sits on a continuation line clears the check when it is
            # placed correctly, and was invisible to this scan when it was not -- so a person who
            # wrote one in that shape was told they had written nothing, which is the sentence this
            # whole function exists to stop printing.
            #
            # Still not a trailer parser, and the distinction is the point: git decides WHETHER a
            # trailer is one, and this only has to agree with it about WHERE the value ends, or the
            # two readers disagree about whether the person wrote anything at all.
            folded = [line.strip()[len(prefix) :].strip()]
            for following in lines[index + 1 :]:
                if not following[:1].isspace() or not following.strip():
                    break
                folded.append(following.strip())
            if value := " ".join(part for part in folded if part):
                return value
    return ""


def _show(repo_root: str | Path, spec: str) -> str | None:
    return _run(repo_root, ["git", "show", spec])


def _run(repo_root: str | Path, argv: list[str]) -> str | None:
    try:
        result = subprocess.run(argv, cwd=repo_root, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
        return None
    return result.stdout if result.returncode == 0 else None


def resolve(*, base: str = "", head: str = "", reviewing: bool = False) -> ConfigRef:
    """Pick the ref configuration should come from.

    Reviewing something means head is untrusted, so configuration comes from base. Not reviewing
    means the working tree is the subject and there is nothing to protect against.
    """
    if reviewing:
        if not base:
            raise UntrustedConfig(
                "a review needs a base ref to load configuration from; without one, the only "
                "available source is the change under review"
            )
        return ConfigRef.base(base)
    return ConfigRef.local()
