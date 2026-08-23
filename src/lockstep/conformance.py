"""Executing a compiled workflow on paper.

Golden tests prove the compiler emits the same text twice. They cannot tell you whether that text
*behaves* like the spec it came from — whether a condition reaches the jobs it should, whether the
job order still follows step order, whether some job can never run at all.

So this walks the emitted graph: it resolves `needs`, evaluates the `if:` expressions the compiler
emits, and reports which jobs would run in which order. The evaluator understands exactly the
expression grammar the compiler produces and refuses anything else, which keeps it honest — an
expression it cannot read is a bug in this module or a new emission it has not been taught.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .errors import LockstepError

EXPRESSION = re.compile(r"^\$\{\{\s*(?P<body>.+?)\s*\}\}$", re.DOTALL)
INPUT_TEST = re.compile(r"^inputs\.(?P<name>[A-Za-z0-9_]+)\s*(?P<op>==|!=)\s*true$")
OUTPUT_TEST = re.compile(
    r"^needs\.(?P<job>[A-Za-z0-9_-]+)\.outputs\.(?P<output>[A-Za-z0-9_-]+)\s*(?P<op>==|!=)\s*'(?P<value>[^']*)'$"
)
NON_EMPTY_TEST = re.compile(
    r"^needs\.(?P<job>[A-Za-z0-9_-]+)\.outputs\.(?P<output>[A-Za-z0-9_-]+)\s*!=\s*'\[\]'$"
)


class UnreadableExpression(LockstepError):
    """The simulator met an expression the compiler is not known to emit."""

    code = "LS600"


@dataclass
class Simulation:
    order: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def ran(self) -> set[str]:
        return set(self.order)


def evaluate(expression: str, inputs: dict[str, Any], outputs: dict[str, dict[str, str]]) -> bool:
    """Evaluate one `if:` expression against declared inputs and prior job outputs."""
    match = EXPRESSION.match(expression.strip())
    body = match.group("body") if match else expression.strip()

    # `&&` is the only combinator the compiler emits, and only between whole clauses.
    if "&&" in body:
        return all(evaluate(clause.strip(), inputs, outputs) for clause in body.split("&&"))

    if body in ("!cancelled()", "always()", "success()", "!failure()"):
        return True
    if body in ("failure()", "cancelled()"):
        return False

    input_match = INPUT_TEST.match(body)
    if input_match:
        value = inputs.get(input_match.group("name"), False)
        truthy = value is True or str(value).lower() == "true"
        return truthy if input_match.group("op") == "==" else not truthy

    empty_match = NON_EMPTY_TEST.match(body)
    if empty_match:
        produced = outputs.get(empty_match.group("job"), {}).get(empty_match.group("output"), "")
        return produced != "[]"

    output_match = OUTPUT_TEST.match(body)
    if output_match:
        produced = outputs.get(output_match.group("job"), {}).get(output_match.group("output"), "")
        expected = output_match.group("value")
        return produced == expected if output_match.group("op") == "==" else produced != expected

    raise UnreadableExpression(
        f"cannot evaluate {body!r}",
        hint="the simulator is taught the grammar the compiler emits; teach it this one, or stop emitting it",
    )


def topological_order(jobs: dict[str, Any]) -> list[str]:
    """Order jobs so every job follows everything it needs. Raises on a cycle."""
    pending = {name: _needs_of(job) for name, job in jobs.items()}
    order: list[str] = []
    while pending:
        ready = sorted(name for name, needs in pending.items() if not (set(needs) & set(pending)))
        if not ready:
            raise LockstepError(f"dependency cycle among jobs: {', '.join(sorted(pending))}")
        # Preserve the compiler's declaration order among jobs that are equally ready, so the
        # simulation reflects the sequence a reader sees in the file.
        declared = [name for name in jobs if name in ready]
        order.extend(declared)
        for name in declared:
            pending.pop(name)
    return order


def _needs_of(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs")
    if needs is None:
        return []
    return [needs] if isinstance(needs, str) else list(needs)


def simulate(
    workflow: dict[str, Any],
    inputs: dict[str, Any] | None = None,
    job_outputs: dict[str, dict[str, str]] | None = None,
) -> Simulation:
    """Walk the compiled graph and report which jobs run, in order.

    A job with no `if:` carries an implicit `success()`, which is what makes Actions propagate skips
    down a dependency chain. An explicit `if:` replaces that default, so such a job is evaluated on
    its own terms — which is precisely how a conditional step avoids skipping everything after it.
    """
    jobs: dict[str, Any] = workflow.get("jobs", {})
    resolved = dict(inputs or {})
    outputs = dict(job_outputs or {})

    # Unset inputs take the declared default, exactly as a dispatch or schedule trigger would.
    # YAML 1.1 readers parse the `on` key as the boolean True; both spellings mean the same key.
    raw: dict[Any, Any] = workflow
    triggers: dict[str, Any] = raw.get("on") or raw.get(True) or {}
    declared: dict[str, Any] = (triggers.get("workflow_dispatch") or {}).get("inputs") or {}
    for name, declaration in declared.items():
        resolved.setdefault(name, declaration.get("default", False))

    simulation = Simulation()
    for name in topological_order(jobs):
        job = jobs[name]
        condition = job.get("if")
        if condition is None:
            blocked = any(dep in simulation.skipped for dep in _needs_of(job))
            runs = not blocked
        else:
            runs = evaluate(condition, resolved, outputs)
        if runs:
            simulation.order.append(name)
        else:
            simulation.skipped.append(name)
    return simulation
