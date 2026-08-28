"""Protected paths.

The rule lives where the writing happens, because a prompt telling a model not to edit CI
configuration is a request and a check here is the thing that holds.

Two tiers. Tier 1 is absolute: these paths change what CI runs, or what a *later* run is allowed
to do, so a grant cannot lift them. Tier 2 is deny-by-default with a named grant.

`lockstep.py` leads Tier 1, and that is new. When configuration was YAML the compiler validated,
editing it was bounded by what the schema allowed. Now configuration is executable Python: an
agent that can edit it can rebind any adapter, drop middleware, and grant itself tools. So can
anything under `.in-lockstep/` — skills are instructions for every future run, the ledger is the
audit record, and the checkpoint store decides what `--recover` replays.

Matching is done on the post-change tree. A symlink written this turn is an out-of-root write next
turn, and evaluating the rule against the pre-change tree would miss it.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

from .types import ChangeAuthor, ChangeSet, FileChange

# Absolute. No grant lifts these.
DENY_ALWAYS: tuple[str, ...] = (
    # Executable policy: the file that defines every binding, middleware and tool grant.
    "lockstep.py",
    "lockstep/",
    ".in-lockstep/",
    # What runs in CI.
    ".github/",
    ".gitlab-ci.yml",
    ".gitlab/",
    ".circleci/",
    "Jenkinsfile",
    ".azure-pipelines.yml",
    # Git's own execution surface.
    ".git/",
    # Executes at install or collection time.
    "setup.py",
    "setup.cfg",
    "pyproject.toml",
    "conftest.py",
    "sitecustomize.py",
    "Makefile",
    "noxfile.py",
    "tox.ini",
    ".envrc",
    ".pre-commit-config.yaml",
    "uv.lock",
    "poetry.lock",
    "requirements.txt",
    # Who reviews what.
    "CODEOWNERS",
    # Secrets.
    ".env",
)

DENY_ALWAYS_SUFFIXES: tuple[str, ...] = (".pem", ".pth", "id_rsa", "id_ed25519")
DENY_ALWAYS_BASENAMES: tuple[str, ...] = ("conftest.py", "CODEOWNERS", "sitecustomize.py")

# Deny by default; a named workflow may be granted.
DENY_UNLESS_GRANTED: tuple[str, ...] = (
    "prompts/",
    ".in-lockstep/skills/",
    # The compiler-era surface, retained at zero cost while repositories are mid-migration.
    "guardrails/",
    "agents/",
    "commands/",
    "profiles/",
    ".pipeline/",
)


@dataclass(frozen=True)
class PathPolicy:
    deny_always: tuple[str, ...] = DENY_ALWAYS
    deny_unless_granted: tuple[str, ...] = DENY_UNLESS_GRANTED
    # Keyed on a workflow id, never a verb or strategy id: strategy selection may be driven by
    # ticket labels, which are attacker-influenceable, and a grant reachable that way is a grant
    # on the instructions for every future run.
    grants: frozenset[str] = frozenset()
    granted_to_workflow: str = ""


@dataclass(frozen=True)
class Refusal:
    path: str
    rule: str
    tier: int


def _normalize(path: str) -> str:
    """Normalize without eating leading dots.

    `lstrip("./")` strips any leading '.' or '/' character, which turns `.github/` into `github/`
    and quietly unprotects every dotfile in the deny set.
    """
    cleaned = path.replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return posixpath.normpath(cleaned)


def escapes_root(path: str) -> bool:
    """Whether a path resolves outside the repository root."""
    raw = path.replace("\\", "/")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        return True
    return posixpath.normpath(raw).startswith("..")


def _matches(path: str, prefixes: tuple[str, ...]) -> str | None:
    normalized = _normalize(path)
    base = posixpath.basename(normalized)
    for prefix in prefixes:
        if prefix.endswith("/"):
            if normalized == prefix.rstrip("/") or normalized.startswith(prefix):
                return prefix
        elif normalized == prefix:
            return prefix
    if base in DENY_ALWAYS_BASENAMES and prefixes is DENY_ALWAYS:
        return base
    for suffix in DENY_ALWAYS_SUFFIXES:
        if prefixes is DENY_ALWAYS and (normalized.endswith(suffix) or base.startswith(".env")):
            return suffix
    return None


class ChangeGuard:
    """Refuses writes to protected paths. Consulted at every point that writes."""

    def __init__(self, policy: PathPolicy | None = None) -> None:
        self.policy = policy or PathPolicy()

    def check_path(self, path: str, *, workflow_id: str = "") -> Refusal | None:
        if escapes_root(path):
            return Refusal(path=path, rule="outside-repo-root", tier=1)

        hit = _matches(path, self.policy.deny_always)
        if hit:
            return Refusal(path=path, rule=hit, tier=1)

        hit = _matches(path, self.policy.deny_unless_granted)
        if hit:
            granted = (
                hit in self.policy.grants
                and bool(self.policy.granted_to_workflow)
                and workflow_id == self.policy.granted_to_workflow
            )
            if not granted:
                return Refusal(path=path, rule=hit, tier=2)
        return None

    def check_change(self, change: FileChange, *, workflow_id: str = "") -> Refusal | None:
        # Evaluated on the post-change tree: a symlink pointing out of the repository is an
        # out-of-root write, whatever the declared path says.
        if change.symlink_target and escapes_root(change.symlink_target):
            return Refusal(path=change.path, rule="symlink-outside-repo-root", tier=1)
        return self.check_path(change.path, workflow_id=workflow_id)

    def check(self, changeset: ChangeSet, *, workflow_id: str = "") -> list[Refusal]:
        """Every agent-authored refusal in a changeset."""
        return [
            refusal
            for change in changeset.by_author(ChangeAuthor.AGENT)
            if (refusal := self.check_change(change, workflow_id=workflow_id)) is not None
        ]
