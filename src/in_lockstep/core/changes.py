"""What an agent may write: protected paths, and one rule about shape.

Two kinds of rule, enforced through one call on purpose. Anything that has to be remembered
separately at three enforcement points will be forgotten at one of them.

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

The second kind of rule is about the *shape* of a change rather than its path, and it exists
because the first kind cannot express it. Tests must stay writable — writing tests is a core
feature, which is why no tier lists them — so "do not delete tests" is not a path rule. But an
agent asked to make CI green has an obvious shortcut, and `fix/*` strategies make it reachable:
delete the failing test, or mark it `skip`. That is refused unless the change carries a ticket,
which turns silencing a test from something an agent can decide into something a person signed.
"""

from __future__ import annotations

import fnmatch
import posixpath
from collections.abc import Callable
from dataclasses import dataclass

from .types import ChangeAuthor, ChangeSet, FileChange

# Absolute. No grant lifts these.
DENY_ALWAYS: tuple[str, ...] = (
    # Executable policy plus every artefact of a run: the lifecycle module that defines each
    # binding, middleware and tool grant; the skills that are instructions for every future run;
    # the checkpoint store that decides what `--recover` replays.
    ".lockstep/",
    # The previous layout. Still denied, because a repository mid-migration has one and an agent
    # that can edit it can rebind anything — and because a rule that stops protecting a path the
    # day it moves is a rule with a window in it.
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


# What a test file looks like, across the ecosystems a repository might mix. Deliberately broad:
# a false positive costs a ticket trailer, a false negative lets an agent delete the test that was
# failing. Extend it on `PathPolicy` for a repository whose conventions differ.
TEST_PATTERNS: tuple[str, ...] = (
    "test_*.py",
    "*_test.py",
    "*_test.go",
    "*.test.ts",
    "*.test.tsx",
    "*.test.js",
    "*.spec.ts",
    "*.spec.tsx",
    "*.spec.js",
    "*Test.java",
    "*_spec.rb",
    "*_test.rs",
)

# Directories whose Python files are tests whatever they are called.
TEST_DIRECTORIES: tuple[str, ...] = ("tests/", "test/", "spec/")

# Markers that stop a test from asserting anything. Matched as substrings rather than parsed,
# because this must work for every language above and a parser only works for one.
SILENCERS: tuple[str, ...] = (
    "@pytest.mark.skip",
    "@pytest.mark.xfail",
    "pytest.skip(",
    "pytest.xfail(",
    "@unittest.skip",
    "@mark.skip",
    "@mark.xfail",
    "it.skip(",
    "test.skip(",
    "describe.skip(",
    "xit(",
    "xdescribe(",
    "t.Skip(",
    "#[ignore]",
    "@Disabled",
    "@Ignore",
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
    test_patterns: tuple[str, ...] = TEST_PATTERNS
    test_directories: tuple[str, ...] = TEST_DIRECTORIES


@dataclass(frozen=True)
class Refusal:
    path: str
    rule: str
    tier: int


def _silencers(text: str) -> set[str]:
    """Which markers that stop a test asserting appear in this text."""
    return {marker for marker in SILENCERS if marker in text}


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

    def is_test(self, path: str) -> bool:
        """Whether a path is a test, by name or by the directory it sits in."""
        cleaned = _normalize(path)
        name = posixpath.basename(cleaned)
        if any(fnmatch.fnmatch(name, pattern) for pattern in self.policy.test_patterns):
            return True
        parts = cleaned.split("/")
        return any(f"{part}/" in self.policy.test_directories for part in parts[:-1])

    def check_test_shape(
        self,
        changeset: ChangeSet,
        *,
        read: Callable[[str], str | None] | None = None,
    ) -> list[Refusal]:
        """GATE-TESTGUARD-1 — a test may not be deleted or silenced without a ticket.

        Not a path rule, and it cannot be made into one: tests have to stay writable, because
        writing tests is a core feature and no tier lists them. What is refused is a *shape* —
        removing a test, or adding a marker that stops it asserting — and only when nothing links
        the change to a decision a person made.

        `read` returns the pre-change contents of a path, so "added a skip" can be told from "this
        file already had one". Without it the rule cannot distinguish those, and it fails closed:
        the asymmetry is that a false positive costs a ticket trailer, while a false negative lets
        an agent asked to make CI green do it by silencing the test that was failing.
        """
        if changeset.ticket.strip():
            return []

        refusals: list[Refusal] = []
        for change in changeset.by_author(ChangeAuthor.AGENT):
            if not self.is_test(change.path):
                continue
            if change.deleted:
                refusals.append(Refusal(path=change.path, rule="test-deleted-without-ticket", tier=1))
                continue
            added = _silencers(change.contents or "")
            if not added:
                continue
            if read is not None:
                added -= _silencers(read(change.path) or "")
            if added:
                refusals.append(Refusal(path=change.path, rule="test-silenced-without-ticket", tier=1))
        return refusals

    def check(
        self,
        changeset: ChangeSet,
        *,
        workflow_id: str = "",
        read: Callable[[str], str | None] | None = None,
    ) -> list[Refusal]:
        """Every agent-authored refusal in a changeset, of either kind.

        One call rather than two. A second check each enforcement point has to remember is a check
        one of them will not make.
        """
        path_refusals = [
            refusal
            for change in changeset.by_author(ChangeAuthor.AGENT)
            if (refusal := self.check_change(change, workflow_id=workflow_id)) is not None
        ]
        return path_refusals + self.check_test_shape(changeset, read=read)
