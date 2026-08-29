"""Where configuration is loaded from.

Configuration is code, discovered at the repository root. Under review, that root is the pull
request's — which would mean the change being reviewed supplies the module defining every binding,
every policy contribution, the egress mode, and the protected-path tiers that are supposed to
constrain reviewing it.

The compiler's arrangement did not have this problem, and its documentation says why in one line:
the workflow that runs is the one on the default branch, so a fork cannot modify the workflow that
reviews it. Nothing about "runnable, never rendered" requires giving that up — it only requires
being explicit about which ref configuration comes from.

So: content under review comes from head, and `lockstep.py` plus `.in-lockstep/` come from a
trusted ref. Protecting `lockstep.py` from *agent* writes does not address this at all; the change
here is human-authored by construction.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Everything loaded from the trusted ref rather than from the change under review. One directory
#: now: the lifecycle module, the skills, and anything else the framework reads as configuration.
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

    @classmethod
    def under_review(cls, ref: str) -> ConfigRef:
        return cls(ref=ref, reason="the ref under review", trusted=False)


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
            f"enough history for the base branch — `actions/checkout` with `fetch-depth: 0`."
        )
    return _show(repo_root, f"{resolved}:{path}")


def _candidates(ref: str) -> tuple[str, ...]:
    """The ref as given, then as a remote-tracking ref.

    A developer has a local `main`; a CI checkout usually does not — it is a detached HEAD with
    `origin/main` and nothing else. Trying only the bare name is what made this control silently
    inapplicable in CI, which is the only place it does any work.
    """
    # A ref that already names a remote, a tag, or a full refs/ path is taken as written:
    # `origin/origin/main` is not a spelling of anything.
    if "/" in ref:
        return (ref,)
    return (ref, f"origin/{ref}")


def _resolve_commit(repo_root: str | Path, ref: str) -> str | None:
    """The commit a trusted ref names, or None if no spelling of it resolves."""
    for candidate in _candidates(ref):
        out = _run(repo_root, ["git", "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"])
        if out is not None and out.strip():
            return out.strip()
    return None


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
