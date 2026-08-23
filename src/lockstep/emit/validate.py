"""Structural checks on emitted workflows.

These catch the mistakes GitHub would only report at run time — a dangling `needs:`, a matrix
reading an output nobody declares, a reusable-workflow job carrying keys Actions rejects. Every one
of them is cheaper to find here than in a scheduled run at 2am.
"""

from __future__ import annotations

import re
from typing import Any

from ..errors import EmitError

# Keys GitHub Actions permits on a job that calls a reusable workflow.
REUSABLE_JOB_KEYS = {
    "name",
    "needs",
    "if",
    "permissions",
    "uses",
    "with",
    "secrets",
    "strategy",
    "concurrency",
}
MAX_DISPATCH_INPUTS = 10
MAX_JOB_MINUTES = 360  # a single job may not exceed six hours

NEEDS_OUTPUT = re.compile(r"needs\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)")


def validate_workflow(name: str, workflow: dict[str, Any]) -> None:
    jobs: dict[str, Any] = workflow.get("jobs", {})
    _check_triggers(name, workflow)
    for job_id, job in jobs.items():
        location = f"{name}:{job_id}"
        _check_reusable_job(location, job)
        _check_needs(location, job, jobs)
        _check_timeout(location, job)
    _check_output_references(name, jobs)


def _check_triggers(name: str, workflow: dict[str, Any]) -> None:
    triggers = workflow.get("on") or {}
    dispatch = triggers.get("workflow_dispatch") or {}
    inputs = dispatch.get("inputs") or {}
    if len(inputs) > MAX_DISPATCH_INPUTS:
        raise EmitError(
            f"workflow_dispatch declares {len(inputs)} inputs; GitHub allows {MAX_DISPATCH_INPUTS}",
            location=name,
            hint="reduce the command's parameters, or split it into sub-commands",
        )


def _check_reusable_job(location: str, job: dict[str, Any]) -> None:
    if "uses" not in job:
        return
    disallowed = sorted(set(job) - REUSABLE_JOB_KEYS)
    if disallowed:
        raise EmitError(
            f"job calls a reusable workflow but also sets {disallowed}",
            location=location,
            hint="Actions rejects runs-on/container/steps/env on a `uses:` job",
        )


def _check_needs(location: str, job: dict[str, Any], jobs: dict[str, Any]) -> None:
    needs = job.get("needs")
    if needs is None:
        return
    for dependency in [needs] if isinstance(needs, str) else needs:
        if dependency not in jobs:
            raise EmitError(f"job needs {dependency!r}, which does not exist", location=location)


def _check_timeout(location: str, job: dict[str, Any]) -> None:
    timeout = job.get("timeout-minutes")
    if isinstance(timeout, int) and timeout > MAX_JOB_MINUTES:
        raise EmitError(
            f"timeout-minutes is {timeout}; a single job may not exceed {MAX_JOB_MINUTES}",
            location=location,
            hint="fan the work out across jobs instead of lengthening one",
        )


def _check_output_references(name: str, jobs: dict[str, Any]) -> None:
    """A matrix reading `needs.x.outputs.y` silently yields an empty matrix if y is never declared."""
    for job_id, job in jobs.items():
        for producer, output in NEEDS_OUTPUT.findall(str(job)):
            if producer not in jobs:
                raise EmitError(
                    f"references outputs of {producer!r}, which does not exist",
                    location=f"{name}:{job_id}",
                )
            declared = (jobs[producer].get("outputs") or {}) if isinstance(jobs[producer], dict) else {}
            if "uses" in jobs[producer]:
                continue  # a called workflow declares its own outputs; we cannot see them here
            if output not in declared:
                raise EmitError(
                    f"reads `needs.{producer}.outputs.{output}`, which {producer!r} does not declare",
                    location=f"{name}:{job_id}",
                    hint="an undeclared output resolves to empty, producing a silently empty matrix",
                )
