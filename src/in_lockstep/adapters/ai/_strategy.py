"""Shared machinery for the model-backed strategies — oneshot, tdd, fix.

Each repeats the same plumbing: run a model turn-loop and turn its failure modes into an Outcome,
parse the JSON cover note leniently, and render the staged-and-injection findings. It lives here so
the strategy files hold their *idea* — one session, or red→green — rather than the boilerplate
around it. What stays in each strategy is what actually differs: how many phases, what deterministic
verb runs between them, and the shape of the report.
"""

from __future__ import annotations

from typing import Any

from ...ai.invoker import InvocationBlocked, InvocationFailed
from ...ai.structured import SchemaError, parse
from ...core.outcome import Finding, Outcome, Severity, Status
from ...privileged.egress import EgressRefused


class PhaseError(Exception):
    """A model phase could not proceed. Carries the Outcome the strategy should return, so a caller
    wraps however many phases it runs in one `except PhaseError` rather than repeating the mapping."""

    def __init__(self, outcome: Outcome[Any]) -> None:
        super().__init__(outcome.reason or "phase failed")
        self.outcome = outcome


async def run_phase(session: Any, system: str, messages: Any, package: Any, *, prefix: str) -> Any:
    """One model turn-loop, with its three failure modes mapped to a `PhaseError`.

    A refused control raises BLOCKED; infrastructure failure or a truncated answer, ERRORED — the
    handling every strategy repeated inline. Returns the Invocation otherwise. `prefix` namespaces
    the truncation reason (`implement.truncated`, `fix.truncated`).
    """
    try:
        invocation = await session.invoker.run(
            system=system,
            messages=messages,
            context=package,
            tools=session.tools,
            run_tool=session.run_tool,
            policy=session.policy,
        )
    except (InvocationBlocked, EgressRefused) as e:
        raise PhaseError(
            Outcome(
                status=Status.BLOCKED,
                reason=e.reason,
                findings=(Finding(id=e.reason, message=str(e), severity=Severity.ERROR, blocking=True),),
            )
        ) from e
    except InvocationFailed as e:
        raise PhaseError(
            Outcome(
                status=Status.ERRORED,
                reason=e.reason,
                findings=(Finding(id=e.reason, message=str(e), severity=Severity.ERROR, blocking=True),),
            )
        ) from e
    if invocation.truncated:
        raise PhaseError(
            Outcome(
                status=Status.ERRORED,
                reason=f"{prefix}.truncated",
                cost=invocation.cost,
                findings=(
                    Finding(
                        id=f"{prefix}.truncated",
                        message=(
                            f"the model stopped at the {session.policy.max_tokens}-token output cap "
                            f"mid-answer. A write cut off there is a truncated file, so nothing staged "
                            f"in this session is returned. Raise `InvokePolicy.max_tokens` and re-run."
                        ),
                        severity=Severity.ERROR,
                        blocking=True,
                    ),
                ),
            )
        )
    return invocation


def read_reply(content: str) -> tuple[str, tuple[str, ...], tuple[str, ...], bool]:
    """The cover note, leniently: (summary, notes, unfinished, malformed). A reply that is not the
    JSON the schema asked for is not thrown away — the change already came through the tool boundary
    — so its text becomes the summary and `malformed` says so."""
    try:
        value = parse(content).value
    except SchemaError:
        return content.strip()[:1000], (), (), True
    if not isinstance(value, dict):
        return content.strip()[:1000], (), (), True
    return (
        str(value.get("summary", "")).strip(),
        _strings(value.get("notes")),
        _strings(value.get("unfinished")),
        False,
    )


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(v) for v in value if isinstance(v, (str, int, float)))


def test_findings(outcome: Any) -> tuple[Finding, ...]:
    """A Test verb's own blocking findings, carried up so a red/green failure explains itself."""
    return tuple(
        Finding(id=f.id, message=f.message, severity=Severity.NOTE)
        for f in outcome.findings
        if getattr(f, "blocking", False)
    )


def reported(
    changeset: Any,
    *,
    unfinished: tuple[str, ...] = (),
    malformed: bool = False,
    invocations: tuple[Any, ...] = (),
    prefix: str,
) -> list[Finding]:
    """The findings that travel with a change: the staged paths, the gaps it named, a note if the
    cover note was not JSON, and anything the injection scanner saw. `prefix` namespaces the ids
    (`implement.staged`, `fix.staged`)."""
    findings = [
        Finding(
            id=f"{prefix}.staged",
            message=f"{'deleted' if change.deleted else 'wrote'} {change.path}",
            severity=Severity.NOTE,
            path=change.path,
        )
        for change in changeset.changes
    ]
    findings += [
        Finding(id=f"{prefix}.unfinished", message=gap, severity=Severity.WARNING) for gap in unfinished
    ]
    if malformed:
        findings.append(
            Finding(
                id=f"{prefix}.unstructured",
                message=(
                    "the final message was not the JSON the schema asked for; its text was kept as "
                    "the summary. The staged change came through the tool boundary and is unaffected."
                ),
                severity=Severity.WARNING,
            )
        )
    findings += [
        Finding(
            id=f"injection.{f.name}",
            message=f"{f.severity}: {f.excerpt}",
            severity=Severity.ERROR if f.severity == "critical" else Severity.WARNING,
        )
        for inv in invocations
        for f in inv.findings
    ]
    return findings
