"""Human boundaries: what a run parks on, what resumes it, and the barrier record between.

Design §13. A run is one machine-driven episode; the moment a person must weigh in it parks --
state externalises to the ledger and the system of record, the process exits `PARKED`, and a
continuation starts as a fresh run when the person's event arrives. Long lifecycles are chains of
short runs stitched by human events, which buys waits of days with no durable-execution runtime.

This module is data and pure functions, and it imports nothing of ours on purpose: the record a
park writes and the transition a tick applies are read by `core` (which writes them) and by
`platform.barrier` (which reads them back from a shared store), and one definition of the shape
is how the two cannot disagree. The I/O -- the compare-and-set, the launch -- lives in
`platform.barrier`; what a tick MEANS is decided here, where a test can reach it without a git
remote.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: The label a parked change request carries on the host, and the marker its fenced JSON rides
#: under. A resume trampoline filters on the label so unrelated events cost nothing (§13.3).
PARKED_LABEL = "lockstep:parked"

#: Human verdicts that count as the boundary being met in the affirmative. Anything else a
#: person says (`changes_requested`, `rejected`, a transition elsewhere) resumes the run too --
#: the continuation decides what a "no" means -- but resumes it as `failed` rather than as
#: `succeeded`, so a join reads the way the person meant it.
AFFIRMATIVE_VERDICTS = frozenset({"approved", "transitioned", "chosen", "guided", "granted"})


@dataclass(frozen=True)
class HumanBoundary:
    """What a person is being asked to do, and where (§13.2).

    `kind` names the boundary type; `target` is the thing on the host the act happens on (a pull
    request number, a ticket key, an environment name); `detail` carries what the type needs (a
    transition's destination, a choice's options, a free-text ask). Constructed through the class
    methods, which are the five boundary types the design lists, so a boundary is one of a closed
    set a resume router can dispatch on.
    """

    kind: str
    target: str
    detail: tuple[str, ...] = ()
    reviewer: str = ""

    @classmethod
    def pr_review(cls, pr: int | str, *, reviewer: str = "") -> HumanBoundary:
        return cls(kind="pr_review", target=str(pr), reviewer=reviewer)

    @classmethod
    def ticket_transition(cls, ticket: str, *, to: str) -> HumanBoundary:
        return cls(kind="ticket_transition", target=ticket, detail=(to,))

    @classmethod
    def host_approval(cls, environment: str) -> HumanBoundary:
        return cls(kind="host_approval", target=environment)

    @classmethod
    def choice(cls, options: tuple[str, ...] | list[str], *, on: str = "") -> HumanBoundary:
        return cls(kind="choice", target=on, detail=tuple(options))

    @classmethod
    def free_text(cls, ask: str, *, on: str = "") -> HumanBoundary:
        return cls(kind="free_text", target=on, detail=(ask,))

    def as_record(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "detail": list(self.detail),
            "reviewer": self.reviewer,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> HumanBoundary:
        return cls(
            kind=str(record.get("kind", "")),
            target=str(record.get("target", "")),
            detail=tuple(str(d) for d in record.get("detail", ()) or ()),
            reviewer=str(record.get("reviewer", "")),
        )

    def describe(self) -> str:
        """One line for a person: what is being waited for."""
        if self.kind == "pr_review":
            who = f" by {self.reviewer}" if self.reviewer else ""
            return f"a review of #{self.target}{who}"
        if self.kind == "ticket_transition":
            return f"{self.target} moved to {self.detail[0] if self.detail else '?'}"
        if self.kind == "host_approval":
            return f"approval of environment {self.target}"
        if self.kind == "choice":
            return f"a choice among {', '.join(self.detail)}" + (f" on {self.target}" if self.target else "")
        if self.kind == "free_text":
            return f"guidance: {self.detail[0] if self.detail else '?'}" + (
                f" on {self.target}" if self.target else ""
            )
        return f"{self.kind} on {self.target}"


@dataclass(frozen=True)
class HumanBranch:
    """A fan-out branch that a person completes rather than a machine (`ctx.human(...)`)."""

    boundary: HumanBoundary
    expires_seconds: float | None = None


@dataclass(frozen=True)
class Resumption:
    """What a continuation is handed: the event that met the boundary, who caused it, and what
    the parked run left for it (§13.3, §13.5).

    `join` is the barrier's view of every branch -- status, reason, decided -- and not the
    branches' full outcomes: findings and values do not survive a process boundary as anything
    but a record, and a continuation that needs them reads the ledger. `stale` is None until
    something measures it: absent is not zero.
    """

    parent_run_id: str
    branch: str
    event_id: str
    actor: str
    verdict: str
    text: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    join: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    stale: bool | None = None

    @property
    def affirmative(self) -> bool:
        return self.verdict.split(":", 1)[0] in AFFIRMATIVE_VERDICTS


@dataclass(frozen=True)
class Parked:
    """What a `PARKED` outcome carries as its value: enough to print the resume command and to
    place the marker on the host, and the key the barrier record lives under."""

    run_id: str
    resume: str
    boundaries: tuple[tuple[str, HumanBoundary], ...]
    key: str

    @property
    def first(self) -> HumanBoundary:
        return self.boundaries[0][1]


class LocalStoreCannotPark(Exception):
    """A human branch was declared on a store only one machine can see (`GATE-OUT-5`)."""


# -- the barrier record ---------------------------------------------------------------------


def barrier_key(run_id: str) -> str:
    """Where a run's barrier record lives in the shared store's state space."""
    return f"barrier/{run_id}"


def barrier_record(
    run_id: str,
    *,
    resume: str,
    head: str,
    payload: Mapping[str, Any] | None = None,
    machine: Mapping[str, Mapping[str, Any]] | None = None,
    human: Mapping[str, HumanBranch] | None = None,
) -> dict[str, Any]:
    """The record a park or a parking fan-out writes: every branch's state, the continuation id,
    the head SHA the decision was made against, and what the continuation needs.

    A plain `park` is the one-branch case, its branch named `""`, so the tick is one machine
    for both shapes.
    """
    branches: dict[str, Any] = {}
    for name, outcome in (machine or {}).items():
        branches[name] = {"kind": "machine", "state": "terminal", **dict(outcome)}
    for name, branch in (human or {}).items():
        branches[name] = {
            "kind": "human",
            "state": "parked",
            "boundary": branch.boundary.as_record(),
            "expires_seconds": branch.expires_seconds,
        }
    return {
        "run_id": run_id,
        "resume": resume,
        "head": head,
        "payload": dict(payload or {}),
        "branches": branches,
        "events": [],
        "complete": False,
    }


def dumps(record: Mapping[str, Any]) -> str:
    """One serialisation, sorted keys, so the same record is the same blob and a compare-and-set
    against the text a reader was handed compares what the store holds."""
    return json.dumps(record, indent=2, sort_keys=True, default=str) + "\n"


def apply_event(
    record: Mapping[str, Any], *, branch: str, event: Resumption
) -> tuple[dict[str, Any], bool, bool]:
    """One tick's transition, pure: `(new record, duplicate, completed now)`.

    Dedupe on `(run_id, branch, event_id)`: a webhook redelivered and a review submitted twice
    collapse into one event, and the second is told so rather than applied (§13.3 step 3). A
    human branch the event names moves `parked -> resumed` carrying the verdict as its status;
    `completed now` is true exactly when this transition is the one that makes every branch
    terminal, which is the write that launches the continuation -- and the store's
    compare-and-set is what makes "exactly one write does" true across machines.
    """
    branches = record.get("branches") or {}
    if branch not in branches:
        raise KeyError(f"barrier for {record.get('run_id')!r} has no branch {branch!r}")
    if branches[branch].get("kind") != "human":
        raise ValueError(
            f"branch {branch!r} of {record.get('run_id')!r} is a machine branch; nothing resumes it"
        )
    events = list(record.get("events") or [])
    if any(e.get("branch") == branch and e.get("event_id") == event.event_id for e in events):
        return dict(record), True, False
    new = json.loads(json.dumps(record, default=str))
    new["branches"][branch] = {
        **new["branches"][branch],
        "state": "resumed",
        "status": "succeeded" if event.affirmative else "failed",
        "reason": f"human.{event.verdict}",
        "decided": True,
        "actor": event.actor,
        "event_id": event.event_id,
    }
    new["events"] = [
        *events,
        {"branch": branch, "event_id": event.event_id, "actor": event.actor, "verdict": event.verdict},
    ]
    every_done = all(b.get("state") in ("terminal", "resumed", "expired") for b in new["branches"].values())
    completed_now = every_done and not record.get("complete", False)
    new["complete"] = bool(record.get("complete", False) or every_done)
    return new, False, completed_now


def join_view(record: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The branches as a continuation sees them: status, reason, decided per branch."""
    out: dict[str, dict[str, Any]] = {}
    for name, branch in (record.get("branches") or {}).items():
        out[name] = {
            "status": branch.get("status", "parked"),
            "reason": branch.get("reason"),
            "decided": bool(branch.get("decided", False)),
            "kind": branch.get("kind"),
            **({"boundary": branch["boundary"]} if branch.get("boundary") else {}),
        }
    return out


__all__ = [
    "AFFIRMATIVE_VERDICTS",
    "PARKED_LABEL",
    "HumanBoundary",
    "HumanBranch",
    "LocalStoreCannotPark",
    "Parked",
    "Resumption",
    "apply_event",
    "barrier_key",
    "barrier_record",
    "dumps",
    "join_view",
]
