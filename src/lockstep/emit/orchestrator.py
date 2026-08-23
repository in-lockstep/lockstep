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
from .builtins import AVAILABLE, MATRIX_CAP
from .caching import cache_spec_for, emit_fingerprint, emit_probe, emit_save, render_step_def, step_def_path
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
    step_defs: dict[str, str] = field(default_factory=dict)
    cached_steps: int = 0


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


def _bare_condition(condition: Condition) -> str:
    operator = "!=" if condition.negated else "=="
    return f"inputs.{condition.input_name} {operator} true"


def _if_expression(condition: Condition) -> str:
    """`(if not --skip-repair)` -> an expression that also reads correctly on schedule triggers."""
    return "${{ " + _bare_condition(condition) + " }}"


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
    _validate_builtins(command, steps, spec)

    # A step that publishes a report needs a write token. Fusing it with test execution would hand
    # that token to whatever the tests do, so it gets its own job.
    for step in steps:
        if _publishes_report(step, ctx):
            step.job_boundary = True

    upstream = _upstream_outputs(steps, ctx, command)
    groups = group_steps(steps, fuse=spec.manifest.target.fuse_script_steps)
    state_group = _resolve_state_scope(command, groups, ctx, result)
    jobs: dict[str, dict[str, Any]] = {}
    previous_id: str | None = None
    previous_job: dict[str, Any] | None = None

    step_to_job: dict[str, str] = {}

    for group in groups:
        fanout_ref: str | None = None
        items_expr: str | None = None
        producer_id: str | None = None
        if group.head.foreach:
            producer_id, producer_job = _ensure_producer(jobs, previous_id, previous_job, group, ctx)
            output_name = f"items_{group.id.replace('-', '_')}"
            _inject_fanout(producer_job, group, ctx, command)
            producer_job.setdefault("outputs", {})[output_name] = (
                "${{ steps." + f"fanout-{group.id}" + ".outputs.items }}"
            )
            items_expr = "${{ needs." + producer_id + ".outputs." + output_name + " }}"
            fanout_ref = "${{ fromJSON(needs." + producer_id + ".outputs." + output_name + ") }}"
            previous_id = producer_id

        emitted: list[tuple[str, dict[str, Any]]]
        if group.kind == "steps":
            emitted = [
                (
                    group.id,
                    _steps_job(
                        group,
                        ctx,
                        previous_id,
                        fanout_ref,
                        command,
                        upstream,
                        result,
                        stateful=group is state_group,
                    ),
                )
            ]
        elif group.kind == "agent":
            emitted = [
                (
                    group.id,
                    _agent_job(group, ctx, previous_id, fanout_ref, agent_lock, agent_secrets, command),
                )
            ]
            result.agents_used.append(group.head.target)
        else:
            emitted = _command_jobs(group, ctx, previous_id, sub_workflow, command)

        for job_id, job in emitted:
            jobs[job_id] = job
        for step in group.steps:
            step_to_job[step.id] = emitted[0][0]

        previous_id = emitted[-1][0]
        previous_job = emitted[-1][1] if group.kind == "steps" else None

        if group.head.foreach and group.head.min_success_rate is not None:
            verify_id, verify_job = _verify_job(group, ctx, previous_id, producer_id, items_expr, command)
            jobs[verify_id] = verify_job
            previous_id, previous_job = verify_id, verify_job

    result.step_count = len(steps)
    result.agentic_steps = sum(1 for s in steps if s.kind is StepKind.AGENT)
    result.deterministic_steps = sum(1 for s in steps if s.kind in (StepKind.SCRIPT, StepKind.BUILTIN))
    # Order matters: the proposal job must exist before the gate authorizes jobs, or the one job
    # holding write permissions would be the only one left unauthorized.
    _emit_proposal(command, ctx, jobs, previous_id)
    _emit_command_gate(command, ctx, jobs)
    # Counted here, not before: the proposal job is a job, and a summary that undercounts is a
    # summary nobody can check against the file.
    result.job_count = len(jobs)
    _guard_against_skipped_dependencies(jobs)
    _expose_convergence(command, jobs, step_to_job, result)

    result.data = {
        "name": command.name if not ctx.multi_profile else f"{command.name} ({ctx.profile.name})",
        "on": _on_block(command, ctx, step_to_job),
        "permissions": {"contents": "read"},
        "concurrency": _concurrency(command, ctx),
        "env": {"OUTPUT_DIR": ctx.output_dir_env},
        "jobs": jobs,
    }
    return result


def _validate_builtins(command: Command, steps: list[Step], spec: Spec) -> None:
    declared = spec.manifest.extensions.builtins
    known = AVAILABLE | set(declared)
    for step in steps:
        if step.kind is StepKind.BUILTIN and step.target not in known:
            raise EmitError(
                f"builtin {step.target!r} is not provided by pipeline-exec",
                location=f"{command.src.rel if command.src else command.name} step {step.number}",
                hint=(
                    f"available: {', '.join(sorted(AVAILABLE))}. "
                    "If an extension provides it, list it under `extensions.builtins` in "
                    "pipeline.yaml — the compiler cannot discover a command it does not install"
                ),
            )


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
    parts = [
        "pipeline-exec fanout",
        f"--input={source}",
        f"--key={foreach.key_field}",
        f"--max={MATRIX_CAP}",
    ]
    output = ctx.expand(group.head.output, command)
    if output:
        # Git and the restored workspace are both caches: an item whose output already exists is
        # dropped here, so a resumed run fans out only what is still missing.
        parts.extend(["--only-missing", f"--output-dir={output}"])
    if group.kind == "agent":
        # An agent leg is a whole gh-aw run and cannot host more than one item.
        parts.append("--no-shard")
    else:
        parts.append(f"--shard-threshold={ctx.spec.manifest.target.shard_threshold}")
    step = {
        "id": f"fanout-{group.id}",
        "name": f"Fan out {foreach.var}s",
        "run": " ".join(parts),
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


STATE_TOKEN = "{state_db}"


def _uses_state(step: Step) -> bool:
    haystack = " ".join([*step.args.values(), step.input, step.output, step.pre, step.post])
    return STATE_TOKEN in haystack


def _resolve_state_scope(
    command: Command, groups: list[JobGroup], ctx: EmitContext, result: WorkflowResult
) -> JobGroup | None:
    """Find the single job that may carry the state database.

    The state file travels between jobs as an artifact, which is last-writer-wins. That is fine
    within one job and wrong across parallel ones, so the compiler is total about it: state must
    live in exactly one job group, and never inside a matrix.
    """
    if not command.state:
        return None

    holders = [(group, step) for group in groups for step in group.steps if _uses_state(step)]
    if not holders:
        result.notes.append(
            f"{command.name}: `state: {command.state}` is declared but no step references "
            f"`{STATE_TOKEN}`; no state database is emitted"
        )
        return None

    distinct = {id(group): group for group, _ in holders}
    if len(distinct) > 1:
        steps = ", ".join(f"{step.number} ({step.label!r})" for _, step in holders)
        raise EmitError(
            f"`{STATE_TOKEN}` is used by steps in {len(distinct)} different jobs: {steps}",
            location=command.src.rel if command.src else command.name,
            hint=(
                "state travels between jobs as a last-writer-wins artifact; merge these steps into "
                "one job (remove the boundary between them) or pass values through step outputs"
            ),
        )

    group = next(iter(distinct.values()))
    if group.head.foreach:
        raise EmitError(
            f"`{STATE_TOKEN}` is used inside a foreach step ({group.head.label!r})",
            location=command.src.rel if command.src else command.name,
            hint="matrix legs run in parallel; concurrent writes to one state artifact would be lost",
        )
    _ = ctx
    return group


def _upstream_outputs(steps: list[Step], ctx: EmitContext, command: Command) -> dict[str, dict[str, str]]:
    """For each step, the output paths earlier steps declared — the cache-invalidation cascade."""
    from .caching import declared_outputs

    seen: dict[str, str] = {}
    per_step: dict[str, dict[str, str]] = {}
    for step in steps:
        per_step[step.id] = dict(seen)
        for path in declared_outputs(step, ctx, command):
            seen[path] = step.id
    return per_step


def _steps_job(
    group: JobGroup,
    ctx: EmitContext,
    needs: str | None,
    fanout_ref: str | None,
    command: Command,
    upstream: dict[str, dict[str, str]],
    result: WorkflowResult,
    stateful: bool = False,
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
        cache = cache_spec_for(step, command, ctx, upstream.get(step.id, {}))
        gate = cache.hit_condition if cache else None
        if cache:
            result.cached_steps += 1
            # The durable cache layer looks up artifacts from earlier runs via the API.
            job["permissions"] = {"contents": "read", "actions": "read"}
            # The definition file exists to be hashed; only a cached step has anything to hash it.
            result.step_defs[step_def_path(command, step)] = render_step_def(step, command)
            if cache.fingerprint:
                body.append(emit_fingerprint(cache, step))
            body.append(emit_probe(cache, ctx))

        if step.pre:
            body.append(_gated({"name": f"pre: {step.label}", "run": step.pre}, gate))
        body.append(_gated(_run_step(step, ctx, command), gate))
        if step.post:
            body.append(_gated({"name": f"post: {step.label}", "run": step.post}, gate))
        if step.on_failure:
            body.append(
                {"name": f"on-failure: {step.label}", "if": "${{ failure() }}", "run": step.on_failure}
            )
        if cache:
            body.append(emit_save(cache, ctx))
        if _publishes_report(step, ctx):
            body.append(_publish_report_step(step, ctx, command))
            # The only write this pipeline performs, and it is confined to the reports branch.
            job["permissions"] = {**job.get("permissions", {"contents": "read"}), "contents": "write"}

    steps: list[dict[str, Any]] = job["steps"]
    if stateful:
        body.insert(
            0,
            {
                "name": "Load state",
                "uses": ctx.pins.action("state/load"),
                "with": {"path": ctx.state_db_path},
            },
        )
        body.append(
            {
                "name": "Save state",
                "if": "${{ always() }}",
                "uses": ctx.pins.action("state/save"),
                "with": {"path": ctx.state_db_path, "retain": command.state == "keep"},
            }
        )
    steps[2:2] = body
    return job


def _publishes_report(step: Step, ctx: EmitContext) -> bool:
    """A `report` builtin publishes when the profile says where to."""
    return (
        step.kind is StepKind.BUILTIN and step.target == "report" and bool(ctx.profile.github.reports.branch)
    )


def _publish_report_step(step: Step, ctx: EmitContext, command: Command) -> dict[str, Any]:
    """Move the rendered report somewhere it will still exist in three months.

    Artifacts expire, so a dashboard that only ever lived in one is useless for a trend. Publishing
    to an orphan branch keeps the history without putting it in anyone's clone.
    """
    reports = ctx.profile.github.reports
    source = _report_source(step, ctx, command)
    return {
        "name": "Publish the report",
        "id": f"publish-{step.id}",
        "if": "${{ always() }}",
        "uses": ctx.pins.action("publish-report"),
        "with": {
            "branch": reports.branch,
            "source": source,
            "path": reports.path,
            "retain": str(reports.retain),
        },
    }


def _report_source(step: Step, ctx: EmitContext, command: Command) -> str:
    """Where the report builtin wrote its output: its `--run-dir`, or the current run."""
    args = ctx.expand(step.args.get("args", ""), command)
    for token in args.split():
        if token.startswith("--run-dir="):
            return token.split("=", 1)[1]
    return f"{ctx.output_dir_env}/runs/current"


def _gated(step: dict[str, Any], condition: str | None) -> dict[str, Any]:
    """A cache hit skips the work and its hooks alike — the hooks are part of the step."""
    if condition:
        step["if"] = condition
    return step


def _run_step(step: Step, ctx: EmitContext, command: Command) -> dict[str, Any]:
    """One spec step becomes one `run:` step; the runner is chosen by file extension.

    A deterministic `foreach` step is wrapped in `shard-run`, which accepts either shape the matrix
    can carry — one item, or a shard covering many. That keeps the emitted workflow identical
    whether or not the item count crosses the sharding threshold, because the count is a runtime
    fact the compiler cannot see. `{item}` and `{item.field}` survive expansion untouched and are
    substituted per item at run time.
    """
    args = ctx.expand(step.args.get("args", ""), command)
    invocation = (
        f"{runner_for(step.target)} {step.target}"
        if step.kind is StepKind.SCRIPT
        else f"pipeline-exec {step.target}"
    )
    run = f"{invocation} {args}".strip()
    if step.foreach:
        source = ctx.expand(step.foreach.source, command)
        run = (
            "pipeline-exec shard-run --slice='${{ toJSON(matrix.item) }}' "
            f"--input={source} --key={step.foreach.key_field} -- {run}"
        )
    return {"name": step.label, "id": step.id, "run": run}


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


def _command_jobs(
    group: JobGroup,
    ctx: EmitContext,
    needs: str | None,
    sub_workflow: dict[str, str],
    command: Command,
) -> list[tuple[str, dict[str, Any]]]:
    """Emit a nested command call, unrolled when it is a convergence loop.

    Actions has no `while`, so a loop that runs "until converged" becomes a fixed chain of jobs, each
    skipped once the previous one reported convergence. The bound is a compile-time decision, which
    is a better habit than an unbounded local loop anyway.
    """
    step = group.head
    target = sub_workflow.get(step.target)
    if not target:
        raise EmitError(f"no compiled workflow for command {step.target!r}")

    iterations = step.max_iterations or command.github.max_iterations or 1
    with_block = {
        slug(k).replace("-", "_"): ctx.expand(v, command) for k, v in step.args.items() if k != "args"
    }
    with_block.setdefault("force", "${{ inputs.force }}")
    secrets = {name: secret_ref(name) for name in ctx.profile.github.secrets}

    emitted: list[tuple[str, dict[str, Any]]] = []
    for index in range(1, iterations + 1):
        job_id = group.id if iterations == 1 else f"{group.id}-{index}"
        job: dict[str, Any] = {}
        previous_ids = [name for name, _ in emitted]
        if previous_ids:
            # Every prior iteration, not just the last: a skipped job's output is empty, and an
            # empty output reads as "not converged", so checking only the predecessor would let a
            # later iteration run after an earlier one had already converged.
            job["needs"] = previous_ids if len(previous_ids) > 1 else previous_ids[0]
        elif needs:
            job["needs"] = needs

        conditions = []
        if step.condition:
            conditions.append(_bare_condition(step.condition))
        conditions.extend(f"needs.{name}.outputs.converged != 'true'" for name in previous_ids)
        if conditions:
            job["if"] = "${{ " + " && ".join(conditions) + " }}"
        job["uses"] = f"./.github/workflows/{target}"
        job["with"] = dict(with_block)
        if secrets:
            job["secrets"] = dict(secrets)
        emitted.append((job_id, job))
    return emitted


def _verify_job(
    group: JobGroup,
    ctx: EmitContext,
    matrix_job: str,
    producer_id: str | None,
    items_expr: str | None,
    command: Command,
) -> tuple[str, dict[str, Any]]:
    """Decide explicitly what a partially-failed fan-out means.

    The local runtime saves each item as it completes and keeps going; plain `needs:` would instead
    fail the whole pipeline on one bad leg. This restores the local semantics as an inspectable
    policy rather than an accident of how Actions treats dependencies.
    """
    step = group.head
    output = ctx.expand(step.output, command) or f"{ctx.output_dir_env}/{step.id}"
    needs = [matrix_job] + ([producer_id] if producer_id and producer_id != matrix_job else [])
    verify = f"pipeline-exec fanout-verify --dir={output} --min-success-rate={step.min_success_rate}"
    if items_expr:
        verify += f" --expected='{items_expr}'"
    # `!cancelled()` lets the gate judge a partially-failed matrix instead of being skipped by it —
    # but if the fan-out itself was conditional and did not run, there is nothing to judge.
    conditions = ([_bare_condition(step.condition)] if step.condition else []) + ["!cancelled()"]
    job: dict[str, Any] = {
        "needs": needs,
        "if": "${{ " + " && ".join(conditions) + " }}",
        "runs-on": ctx.runs_on,
        "container": ctx.pins.exec_container(),
        "steps": [
            {"uses": ctx.pins.external_action("actions/checkout")},
            {"uses": ctx.pins.action("restore")},
            {"name": f"Verify {step.label} coverage", "id": f"verify-{step.id}", "run": verify},
            {"id": "save-workspace", "if": "${{ always() }}", "uses": ctx.pins.action("save")},
        ],
    }
    return f"verify-{group.id}", job


# Actions skips a job whose dependency was skipped. That is the opposite of what a conditional step
# means in the spec — `(if not --skip-discovery)` skips discovery, not everything after it. This is
# the documented idiom for "run if everything upstream succeeded or was skipped".
SKIP_TOLERANT = "!failure() && !cancelled()"


COMMAND_GATE = "command-gate"


def _expand_arguments(names: list[str]) -> str:
    """Turn the gate's JSON argument blob into one step output per declared argument."""
    lines = [
        "set -euo pipefail",
        "payload='${{ steps.gate.outputs.arguments }}'",
        '[ -n "$payload" ] || payload="{}"',
    ]
    lines += [
        f'echo "{name}=$(echo "$payload" | jq -r \'.{name} // empty\')" >> "$GITHUB_OUTPUT"' for name in names
    ]
    return "\n".join(lines) + "\n"


def _emit_command_gate(command: Command, ctx: EmitContext, jobs: dict[str, dict[str, Any]]) -> None:
    """Gate a comment-triggered pipeline on who asked for it.

    A workflow triggered by a comment runs with the repository's token, and anyone who can comment
    can trigger it. So every job is made to depend on an explicit authorization output rather than
    on skip propagation — a job that is merely *downstream* of a skipped gate would still run under
    the tolerant condition that lets conditional steps work.
    """
    chat = command.github.command
    if not chat or not chat.name:
        return

    gate: dict[str, Any] = {
        "name": f"Authorize {chat.name}",
        "runs-on": ctx.runs_on,
        # Reading collaborator permission and reacting to the comment; nothing else.
        "permissions": {"contents": "read"},
        "outputs": {
            "authorized": "${{ steps.gate.outputs.authorized }}",
            # Read from the expansion step, not from the action: a composite action exposes only the
            # outputs it declares, and these names are chosen per pipeline.
            **{name: "${{ steps.arguments.outputs." + name + " }}" for name in chat.arguments},
            "instruction": "${{ steps.gate.outputs.instruction }}",
            "pull_request": "${{ steps.gate.outputs.pull_request }}",
        },
        "steps": [
            {"uses": ctx.pins.external_action("actions/checkout")},
            {
                "name": f"Authorize {chat.name}",
                "id": "gate",
                "uses": ctx.pins.action("command-gate"),
                "with": {
                    "command": chat.name,
                    "roles": ",".join(chat.roles),
                    "arguments": ",".join(chat.arguments),
                    "reaction": chat.reaction,
                },
            },
            {
                "name": "Expand the parsed arguments",
                "id": "arguments",
                "shell": "bash",
                "run": _expand_arguments(chat.arguments),
            },
        ],
    }
    jobs[COMMAND_GATE] = gate

    authorized = f"needs.{COMMAND_GATE}.outputs.authorized == 'true'"
    for name, job in jobs.items():
        if name == COMMAND_GATE:
            continue
        needs = _needs_list(job)
        if COMMAND_GATE not in needs:
            needs.insert(0, COMMAND_GATE)
        job["needs"] = needs if len(needs) > 1 else needs[0]
        existing = job.get("if")
        inner = (existing or "").strip().removeprefix("${{").removesuffix("}}").strip()
        job["if"] = "${{ " + (f"{authorized} && {inner}" if inner else authorized) + " }}"

    # Move the gate to the front so the file reads in the order it runs.
    ordered = {COMMAND_GATE: jobs.pop(COMMAND_GATE), **jobs}
    jobs.clear()
    jobs.update(ordered)


def _emit_proposal(
    command: Command,
    ctx: EmitContext,
    jobs: dict[str, dict[str, Any]],
    needs: str | None,
) -> None:
    """Route generated artifacts into the repository through review rather than straight in.

    This is where a pipeline stops paying for what it already knows. The agent writes the artifacts
    once, a human reviews them once, and every run afterwards executes committed files for nothing.
    """
    propose = command.github.propose
    if not propose or not propose.source or not propose.destination:
        return

    jobs["propose-generated-artifacts"] = {
        "needs": needs,
        "if": "${{ !cancelled() }}",
        "runs-on": ctx.runs_on,
        "container": ctx.pins.exec_container(),
        # The narrowest write a pipeline can need: a branch nobody merges without reading it.
        "permissions": {"contents": "write", "pull-requests": "write"},
        "steps": [
            {"uses": ctx.pins.external_action("actions/checkout")},
            {"uses": ctx.pins.action("restore")},
            {
                "name": "Propose the generated artifacts",
                "id": "propose",
                "uses": ctx.pins.action("propose-pr"),
                # Every field is expanded: a title or branch naming a parameter is the normal case,
                # and a literal `{issue}` reaching GitHub is the kind of thing nobody notices until
                # they are looking at a branch called `{branch}`.
                "with": {
                    "source": ctx.expand(propose.source, command),
                    "destination": propose.destination,
                    "branch": ctx.expand(propose.branch, command),
                    "title": ctx.expand(propose.title, command),
                    "labels": propose.labels,
                    **({"base": ctx.expand(propose.base, command)} if propose.base else {}),
                },
            },
        ],
    }


def _guard_against_skipped_dependencies(jobs: dict[str, dict[str, Any]]) -> None:
    """Let work downstream of a skipped conditional step still run."""
    # The command gate is excluded: every job checks its output directly, and treating it as merely
    # skippable would let work proceed on an unauthorized comment.
    skippable = {name for name, job in jobs.items() if job.get("if") and name != COMMAND_GATE}
    changed = True
    while changed:
        changed = False
        for name, job in jobs.items():
            if name in skippable:
                continue
            if any(dep in skippable for dep in _needs_list(job)):
                skippable.add(name)
                changed = True

    for job in jobs.values():
        if not any(dep in skippable for dep in _needs_list(job)):
            continue
        condition = job.get("if")
        if condition is None:
            job["if"] = "${{ " + SKIP_TOLERANT + " }}"
        elif "cancelled()" not in condition:
            inner = condition.strip().removeprefix("${{").removesuffix("}}").strip()
            job["if"] = "${{ " + f"{SKIP_TOLERANT} && {inner}" + " }}"


def _needs_list(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs")
    if needs is None:
        return []
    return [needs] if isinstance(needs, str) else list(needs)


def _expose_convergence(
    command: Command,
    jobs: dict[str, dict[str, Any]],
    step_to_job: dict[str, str],
    result: WorkflowResult,
) -> None:
    """Publish a `converged` job output so a caller can unroll this command as a loop."""
    source = command.github.converged_from
    if not source:
        return
    job_id = step_to_job.get(source)
    if job_id is None:
        raise EmitError(
            f"`converged-from: {source}` names a step this command does not compile",
            location=command.src.rel if command.src else command.name,
            hint=f"known step ids: {', '.join(sorted(step_to_job)) or '(none)'}",
        )
    jobs[job_id].setdefault("outputs", {})["converged"] = "${{ steps." + source + ".outputs.converged }}"


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


def _on_block(command: Command, ctx: EmitContext, step_to_job: dict[str, str]) -> dict[str, Any]:
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

    chat = command.github.command
    if chat and chat.name:
        for event in chat.events:
            # `created` only: an edited comment re-firing a pipeline would let somebody rewrite what
            # a run was asked to do after it was authorized.
            on.setdefault(event, {"types": ["created"]})

    call_inputs = {
        name: {k: v for k, v in entry.items() if k != "description"} for name, entry in inputs.items()
    }
    call: dict[str, Any] = {"inputs": call_inputs}
    if ctx.profile.github.secrets:
        call["secrets"] = {name: {"required": False} for name in ctx.profile.github.secrets}
    if command.github.converged_from:
        job_id = step_to_job[command.github.converged_from]
        call["outputs"] = {
            "converged": {
                "description": "Whether this run reached convergence",
                "value": "${{ jobs." + job_id + ".outputs.converged }}",
            }
        }
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


STEP_ORDER = ("name", "id", "if", "uses", "run", "with", "env", "working-directory", "continue-on-error")


def _reorder(data: dict[str, Any], order: tuple[str, ...]) -> dict[str, Any]:
    known = [key for key in order if key in data]
    rest = [key for key in data if key not in order]
    return {key: data[key] for key in [*known, *rest]}


def normalize(workflow: dict[str, Any]) -> dict[str, Any]:
    """Put a compiled workflow into canonical key order."""
    jobs: dict[str, Any] = workflow.get("jobs", {})
    normalized: dict[str, Any] = {}
    for name, job in jobs.items():
        if isinstance(job.get("steps"), list):
            job["steps"] = [_reorder(step, STEP_ORDER) for step in job["steps"]]
        normalized[name] = _reorder(job, JOB_ORDER)
    workflow["jobs"] = normalized
    return _reorder(workflow, TOP_LEVEL_ORDER)
