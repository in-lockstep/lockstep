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

CONFIG_PATHS = ("lockstep.py", ".in-lockstep")


class UntrustedConfig(Exception):
    """Configuration would resolve from the ref under review."""


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
    try:
        result = subprocess.run(
            ["git", "show", f"{ref.ref}:{path}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
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
