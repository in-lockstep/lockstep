"""What a job calling a compiled agent workflow has to grant it.

`gh aw compile` decides what permissions its generated jobs need, and a reusable workflow can never
hold more than its caller. lockstep granted nothing, so every scope but `contents` was `none` and
GitHub refused the workflow before running anything:

    The nested job 'activation' is requesting 'actions: read',
    but is only allowed 'actions: none'.

No pipeline with an agent in it had ever started. Nothing offline caught it — `lint`, `doctor`, the
drift gate, the semantic diff and GitHub's own workflow schema all pass, because the file is
structurally valid. The rule GitHub enforces is between two files, at run time.

So the constant is checked against reality here: every committed `.lock.yml` in the repository is
parsed, and the build fails if any job asks for something the caller does not grant. A gh-aw version
that needs a fourth scope becomes a red build on the commit that bumps it, rather than a startup
failure in somebody's repository at an inconvenient hour.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from lockstep.emit.agentic import AGENT_CALLER_PERMISSIONS

# Rank within one scope. A caller granting `write` satisfies a callee asking for `read`.
RANK = {"none": 0, "read": 1, "write": 2}


def _lock_files(repo_root: Path) -> list[Path]:
    return sorted(repo_root.glob(".github/workflows/aw-*.lock.yml")) + sorted(
        repo_root.glob("examples/*/.github/workflows/aw-*.lock.yml")
    )


def test_there_are_lock_files_to_check(repo_root):
    """Otherwise this whole module passes by describing nothing."""
    assert _lock_files(repo_root), "no compiled agent workflows found to check the contract against"


def test_the_caller_grants_everything_the_generated_jobs_ask_for(repo_root):
    missing: list[str] = []
    for lock in _lock_files(repo_root):
        data = yaml.safe_load(lock.read_text(encoding="utf-8")) or {}
        for job, body in (data.get("jobs") or {}).items():
            wanted = body.get("permissions")
            if not isinstance(wanted, dict):
                # `read-all` and `write-all` are the whole-workflow forms; the scope-by-scope
                # comparison below does not apply to them.
                continue
            for scope, level in wanted.items():
                granted = AGENT_CALLER_PERMISSIONS.get(scope, "none")
                if RANK.get(str(level), 0) > RANK.get(granted, 0):
                    missing.append(f"{lock.name}:{job} wants {scope}: {level}, caller grants {granted}")
    assert not missing, (
        "AGENT_CALLER_PERMISSIONS no longer covers what gh-aw generates — a run would fail at "
        "startup with 'is requesting X, but is only allowed none':\n  " + "\n  ".join(sorted(set(missing)))
    )


def test_the_caller_grants_nothing_it_was_not_asked_for(repo_root):
    """The other direction, so the grant cannot quietly become a blanket one.

    A scope nothing requests is a scope handed to every agent workflow for no reason, and the point
    of this framework is that the surface is the smallest thing that works.
    """
    asked: set[str] = set()
    for lock in _lock_files(repo_root):
        data = yaml.safe_load(lock.read_text(encoding="utf-8")) or {}
        for body in (data.get("jobs") or {}).values():
            wanted = body.get("permissions")
            if isinstance(wanted, dict):
                asked |= set(wanted)
    assert set(AGENT_CALLER_PERMISSIONS) <= asked, (
        f"granted but never requested: {sorted(set(AGENT_CALLER_PERMISSIONS) - asked)}"
    )


def test_the_agent_job_itself_still_never_writes(repo_root):
    """`issues: write` on the caller must not have leaked into the agent.

    The floor's claim is that an agent produces files and a deterministic job performs any write.
    This is that claim checked against what actually runs, rather than against the markdown it was
    compiled from.
    """
    for lock in _lock_files(repo_root):
        data = yaml.safe_load(lock.read_text(encoding="utf-8")) or {}
        agent = (data.get("jobs") or {}).get("agent")
        if agent is None:
            continue
        assert agent.get("permissions") == "read-all", f"{lock.name}: agent job is not read-only"
