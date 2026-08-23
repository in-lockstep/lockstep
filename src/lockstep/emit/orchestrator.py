"""Commands become plain GitHub Actions orchestrators.

Orchestration is deliberately *not* an agentic workflow: fan-out, ordering, conditions and caching
are deterministic mechanics, and gh-aw frontmatter has no matrix. So a command lowers to ordinary
Actions YAML whose only agentic content is `uses:` calls into compiled agent workflows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import EmitError, SpecError
from ..spec.model import Command, Condition, Spec, Step, StepKind
from ..util.text import slug
from .context import EmitContext
from .profiles import env_block, secret_ref

RUNNERS = {
    ".py": "uv run python3",
    ".sh": "bash",
    ".ts": "npx tsx",
    ".js": "node",
    ".rb": "ruby",
    ".go": "go run",
}


@dataclass
class JobGroup:
    """One emitted job: either a run of fused deterministic steps, or a single `uses:` call."""

    kind: str  # "steps" | "agent" | "command"
    steps: list[Step]
    id: str = ""

    @property
    def head(self) -> Step:
        return self.steps[0]

    @property
    def condition(self) -> Condition | None:
        return self.head.condition


@dataclass
class WorkflowResult:
    filename: str
    data: dict[str, Any]
    agents_used: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    step_count: int = 0
    job_count: int = 0
    agentic_steps: int = 0
    deterministic_steps: int = 0


def runner_for(script: str) -> str:
    for suffix, runner in RUNNERS.items():
        if script.endswith(suffix):
            return runner
    raise EmitError(
        f"no runner for script {script!r}",
        hint=f"known extensions: {', '.join(sorted(RUNNERS))}",
    )


def group_steps(steps: list[Step], *, fuse: bool) -> list[JobGroup]:
    """Fuse consecutive deterministic steps into one job; split at every boundary that matters."""
    groups: list[JobGroup] = []
    current: JobGroup | None = None

    for step in steps:
        if step.kind is StepKind.AGENT:
            groups.append(JobGroup(kind="agent", steps=[step]))
            current = None
            continue
        if step.kind is StepKind.COMMAND:
            groups.append(JobGroup(kind="command", steps=[step]))
            current = None
            continue

        breaks = (
            current is None
            or not fuse
            or not step.fusible
            or not current.steps[-1].fusible
            # a condition change would otherwise become per-step `if:` spaghetti
            or _condition_key(step.condition) != _condition_key(current.condition)
        )
        if breaks:
            current = JobGroup(kind="steps", steps=[step])
            groups.append(current)
        else:
            assert current is not None
            current.steps.append(step)

    taken: set[str] = set()
    for group in groups:
        base = group.head.id
        name = base
        n = 2
        while name in taken:
            name = f"{base}-{n}"
            n += 1
        taken.add(name)
        group.id = name
    return groups


def _condition_key(condition: Condition | None) -> str:
    return condition.key() if condition else ""


def _if_expression(condition: Condition) -> str:
    """`(if not --skip-repair)` -> an expression that also reads correctly on schedule triggers."""
    operator = "!=" if condition.negated else "=="
    return "${{ inputs." + condition.input_name + " " + operator + " true }}"


def emit_command(
    command: Command,
    spec: Spec,
    ctx: EmitContext,
    *,
    filename: str,
    agent_lock: dict[str, str],
    agent_secrets: dict[str, list[str]],
    sub_workflow: dict[str, str],
) -> WorkflowResult:
    """Lower one command into a workflow mapping."""
    result = WorkflowResult(filename=filename, data={})
    steps = [s for s in command.steps if s.applies_to("github")]
    for skipped in [s for s in command.steps if not s.applies_to("github")]:
        result.notes.append(
            f"{command.name}: step {skipped.number} ({skipped.label!r}) skipped — targets={skipped.targets}"
        )
    if command.state:
        result.notes.append(
            f"{command.name}: `state: {command.state}` is not lowered yet (Phase 2); "
            "steps needing shared state should be fused into one job"
        )
    if command.github.max_iterations > 1:
        result.notes.append(
            f"{command.name}: `max-iterations` convergence unrolling is not implemented yet (Phase 2)"
        )

    if any(parameter.name == "profile" for parameter in command.parameters):
        result.notes.append(
            f"{command.name}: `profile` is compiled in, not selected at run time — its workflow input "
            "is accepted for compatibility but does not choose configuration"
        )

    _validate_conditions(command, steps)

    groups = group_steps(steps, fuse=spec.manifest.target.fuse_script_steps)
    jobs: dict[str, dict[str, Any]] = {}
    previous_id: str | None = None
    previous_job: dict[str, Any] | None = None

    for group in groups:
        fanout_ref: str | None = None
        if group.head.foreach:
            producer_id, producer_job = _ensure_producer(jobs, previous_id, previous_job, group, ctx)
            output_name = f"items_{group.id.replace('-', '_')}"
            _inject_fanout(producer_job, group, ctx, command)
            producer_job.setdefault("outputs", {})[output_name] = (
                "${{ steps." + f"fanout-{group.id}" + ".outputs.items }}"
            )
            fanout_ref = "${{ fromJSON(needs." + producer_id + ".outputs." + output_name + ") }}"
            previous_id = producer_id

        if group.kind == "steps":
            job = _steps_job(group, ctx, previous_id, fanout_ref, command)
        elif group.kind == "agent":
            job = _agent_job(group, ctx, previous_id, fanout_ref, agent_lock, agent_secrets, command)
            result.agents_used.append(group.head.target)
        else:
            job = _command_job(group, ctx, previous_id, sub_workflow, command)

        jobs[group.id] = job
        previous_id = group.id
        previous_job = job if group.kind == "steps" else None

    result.step_count = len(steps)
    result.job_count = len(jobs)
    result.agentic_steps = sum(1 for s in steps if s.kind is StepKind.AGENT)
    result.deterministic_steps = sum(1 for s in steps if s.kind in (StepKind.SCRIPT, StepKind.BUILTIN))
    result.data = {
        "name": command.name if not ctx.multi_profile else f"{command.name} ({ctx.profile.name})",
        "on": _on_block(command, ctx),
        "permissions": {"contents": "read"},
        "concurrency": _concurrency(command, ctx),
        "env": {"OUTPUT_DIR": ctx.output_dir_env},
        "jobs": jobs,
    }
    return result


def _validate_conditions(command: Command, steps: list[Step]) -> None:
    declared = {p.input_name for p in command.parameters} | {"force"}
    for step in steps:
        if step.condition and step.condition.input_name not in declared:
            raise SpecError(
                f"step condition references undeclared parameter {step.condition.flag!r}",
                location=f"{command.src.rel if command.src else command.name} step {step.number}",
                hint=f"declared parameters: {', '.join(sorted(declared))}",
            )


def _ensure_producer(
    jobs: dict[str, dict[str, Any]],
    previous_id: str | None,
    previous_job: dict[str, Any] | None,
    group: JobGroup,
    ctx: EmitContext,
) -> tuple[str, dict[str, Any]]:
    """Fan-out needs a steps job to emit the item list from. Reuse the previous one or insert one."""
    if previous_job is not None and previous_id is not None:
        return previous_id, previous_job
    job_id = f"fanout-{group.id}"
    job = _base_steps_job(ctx, previous_id, condition=group.condition)
    jobs[job_id] = job
    return job_id, job


def _inject_fanout(job: dict[str, Any], group: JobGroup, ctx: EmitContext, command: Command) -> None:
    foreach = group.head.foreach
    assert foreach is not None
    source = ctx.expand(foreach.source, command)
    step = {
        "id": f"fanout-{group.id}",
        "name": f"Fan out {foreach.var}s",
        "run": (
            f"pipeline-exec fanout --input={source} --key={foreach.key_field} "
            '--only-missing --max=256 >> "$GITHUB_OUTPUT"'
        ),
    }
    # Insert before the trailing save step so the item list reflects this job's own output.
    steps: list[dict[str, Any]] = job["steps"]
    insert_at = len(steps)
    for index, existing in enumerate(steps):
        if existing.get("id") == "save-workspace":
            insert_at = index
            break
    steps.insert(insert_at, step)


def _base_steps_job(ctx: EmitContext, needs: str | None, *, condition: Condition | None) -> dict[str, Any]:
    job: dict[str, Any] = {}
    if needs:
        job["needs"] = needs
    if condition:
        job["if"] = _if_expression(condition)
    job["runs-on"] = ctx.runs_on
    job["container"] = ctx.pins.exec_container()
    job["steps"] = [
        {"uses": ctx.pins.external_action("actions/checkout")},
        {"uses": ctx.pins.action("restore")},
        {
            "id": "save-workspace",
            "uses": ctx.pins.action("save"),
            "if": "${{ always() }}",
        },
    ]
    return job


def _steps_job(
    group: JobGroup,
    ctx: EmitContext,
    needs: str | None,
    fanout_ref: str | None,
    command: Command,
) -> dict[str, Any]:
    job = _base_steps_job(ctx, needs, condition=group.condition)
    env = env_block(ctx.profile)
    if any(value.startswith("${{ secrets.") for value in env.values()) and ctx.profile.github.environment:
        job["environment"] = ctx.profile.github.environment

    if fanout_ref:
        job["strategy"] = _strategy(group, fanout_ref)
    if env:
        job["env"] = dict(env)

    body: list[dict[str, Any]] = []
    for step in group.steps:
        if step.pre:
            body.append({"name": f"pre: {step.label}", "run": step.pre})
        body.append(_run_step(step, ctx, command))
        if step.post:
            body.append({"name": f"post: {step.label}", "run": step.post})
        if step.on_failure:
            body.append(
                {"name": f"on-failure: {step.label}", "if": "${{ failure() }}", "run": step.on_failure}
            )

    steps: list[dict[str, Any]] = job["steps"]
    steps[2:2] = body
    return job


def _run_step(step: Step, ctx: EmitContext, command: Command) -> dict[str, Any]:
    """One spec step becomes one `run:` step; the runner is chosen by file extension."""
    args = ctx.expand(step.args.get("args", ""), command)
    invocation = (
        f"{runner_for(step.target)} {step.target}"
        if step.kind is StepKind.SCRIPT
        else f"pipeline-exec {step.target}"
    )
    return {"name": step.label, "run": f"{invocation} {args}".strip()}


def _strategy(group: JobGroup, fanout_ref: str) -> dict[str, Any]:
    strategy: dict[str, Any] = {"fail-fast": False}
    if group.head.parallel:
        strategy["max-parallel"] = group.head.parallel
    strategy["matrix"] = {"item": fanout_ref}
    return strategy


def _agent_job(
    group: JobGroup,
    ctx: EmitContext,
    needs: str | None,
    fanout_ref: str | None,
    agent_lock: dict[str, str],
    agent_secrets: dict[str, list[str]],
    command: Command,
) -> dict[str, Any]:
    step = group.head
    job: dict[str, Any] = {}
    if needs:
        job["needs"] = needs
    if step.condition:
        job["if"] = _if_expression(step.condition)
    if fanout_ref:
        job["strategy"] = _strategy(group, fanout_ref)

    lock = agent_lock.get(step.target)
    if not lock:
        raise EmitError(f"no compiled workflow for agent {step.target!r}")
    job["uses"] = f"./.github/workflows/{lock}"

    with_block: dict[str, Any] = {}
    if step.foreach:
        with_block["item"] = "${{ toJSON(matrix.item) }}"
        key = "${{ matrix.item." + step.foreach.key_field + " }}"
        output = ctx.expand(step.output, command) or f"{ctx.output_dir_env}/{step.id}"
        with_block["output_path"] = f"{output}/{key}.json"
    else:
        if step.input:
            with_block["input_path"] = ctx.expand(step.input, command)
        with_block["output_path"] = ctx.expand(step.output, command) or f"{ctx.output_dir_env}/{step.id}.json"
    if step.context_files:
        with_block["context_files"] = ",".join(ctx.expand(p, command) for p in step.context_files)
    job["with"] = with_block

    # Pass only what the callee declares. Handing a reusable workflow an undeclared secret is an
    # error, and `secrets: inherit` would hand every agent the whole repository.
    required = agent_secrets.get(step.target, [])
    if required:
        job["secrets"] = {name: secret_ref(name) for name in required}
    return job


def _command_job(
    group: JobGroup,
    ctx: EmitContext,
    needs: str | None,
    sub_workflow: dict[str, str],
    command: Command,
) -> dict[str, Any]:
    step = group.head
    job: dict[str, Any] = {}
    if needs:
        job["needs"] = needs
    if step.condition:
        job["if"] = _if_expression(step.condition)
    target = sub_workflow.get(step.target)
    if not target:
        raise EmitError(f"no compiled workflow for command {step.target!r}")
    job["uses"] = f"./.github/workflows/{target}"

    with_block = {
        slug(k).replace("-", "_"): ctx.expand(v, command) for k, v in step.args.items() if k != "args"
    }
    with_block.setdefault("force", "${{ inputs.force }}")
    job["with"] = with_block

    secrets = {name: secret_ref(name) for name in ctx.profile.github.secrets}
    if secrets:
        job["secrets"] = secrets
    return job


def _inputs_for(command: Command) -> dict[str, Any]:
    inputs: dict[str, Any] = {}
    for parameter in command.parameters:
        entry: dict[str, Any] = {}
        if parameter.description:
            entry["description"] = parameter.description
        if parameter.is_flag:
            entry["type"] = "boolean"
            entry["default"] = str(parameter.default).lower() == "true"
        else:
            entry["type"] = "string"
            entry["default"] = parameter.default or ""
        inputs[parameter.input_name] = entry
    inputs.setdefault(
        "force",
        {"description": "Re-run steps even when a cached output exists", "type": "boolean", "default": False},
    )
    inputs.setdefault(
        "force_steps",
        {"description": "Comma-separated step ids to force", "type": "string", "default": ""},
    )
    return inputs


def _on_block(command: Command, ctx: EmitContext) -> dict[str, Any]:
    inputs = _inputs_for(command)
    on: dict[str, Any] = {"workflow_dispatch": {"inputs": inputs}}

    triggers = command.github.triggers or {}
    schedule = triggers.get("schedule")
    if schedule:
        crons = schedule if isinstance(schedule, list) else [schedule]
        on["schedule"] = [{"cron": str(c)} for c in crons]
    for name, value in triggers.items():
        if name in ("schedule", "workflow_dispatch"):
            continue
        on[name] = value

    call_inputs = {
        name: {k: v for k, v in entry.items() if k != "description"} for name, entry in inputs.items()
    }
    call: dict[str, Any] = {"inputs": call_inputs}
    if ctx.profile.github.secrets:
        call["secrets"] = {name: {"required": False} for name in ctx.profile.github.secrets}
    on["workflow_call"] = call
    return on


def _concurrency(command: Command, ctx: EmitContext) -> dict[str, Any]:
    if command.github.concurrency:
        return command.github.concurrency
    return {"group": f"{command.name}-{ctx.profile.name}", "cancel-in-progress": False}


# GitHub does not care about key order, but reviewers and diffs do. Overlay-inserted keys land in
# canonical position too, so an overlay never reorders the file it patches.
TOP_LEVEL_ORDER = ("name", "on", "permissions", "concurrency", "env", "defaults", "jobs")
JOB_ORDER = (
    "name",
    "needs",
    "if",
    "permissions",
    "environment",
    "concurrency",
    "strategy",
    "runs-on",
    "container",
    "services",
    "outputs",
    "timeout-minutes",
    "defaults",
    "env",
    "uses",
    "with",
    "secrets",
    "steps",
)


def _reorder(data: dict[str, Any], order: tuple[str, ...]) -> dict[str, Any]:
    known = [key for key in order if key in data]
    rest = [key for key in data if key not in order]
    return {key: data[key] for key in [*known, *rest]}


def normalize(workflow: dict[str, Any]) -> dict[str, Any]:
    """Put a compiled workflow into canonical key order."""
    jobs = workflow.get("jobs", {})
    workflow["jobs"] = {name: _reorder(job, JOB_ORDER) for name, job in jobs.items()}
    return _reorder(workflow, TOP_LEVEL_ORDER)
