"""The workflow that runs an agent against its eval cases.

`lockstep lint` refuses an agent with no cases because an agent nobody evaluates cannot be changed
safely. That argument only holds if something runs them, and until now nothing did.

Three jobs per agent, and the shape is deliberately the ordinary one. An eval is not a special way
of running an agent — it is `input_path` in, `output_path` out, the same contract every agent step
uses, with the input coming from a case file instead of an earlier step. That is what makes a green
suite evidence about the agent that ships rather than about a test harness.

    cases-<agent>   expand the case files into agent inputs, and list them
    run-<agent>     the agent itself, once per case, as a matrix
    judge-<agent>   pair each rubric with its answer, and judge  (only with `evals.judge`)
    grade-<agent>   apply the deterministic checks, fold in verdicts, gate

The trigger is the thing worth reading twice. This suite spends credits, so it never runs on every
push: it is dispatched, or it runs when the prompt layers it covers change. A change to an agent
body, a guardrail or a skill is exactly what an eval exists to gate, and nothing else in the
repository can move an agent's behaviour.
"""

from __future__ import annotations

from typing import Any

from ..spec.model import Spec
from .agentic import lock_filename
from .context import EmitContext

WORKFLOW_NAME = "evals.yml"

# The layers that can change what an agent does. A case says nothing about the rest of the tree.
PROMPT_PATHS = ("agents/**", "guardrails/**", "skills/**", "contexts/**", "evals/**")


def agents_with_cases(spec: Spec) -> list[str]:
    """Agents this repository can evaluate: its own, with cases on disk.

    An inherited agent is evaluated by whoever published it, against the prompt they wrote. A
    consumer running those cases would be re-testing somebody else's lens from the outside and
    paying for the privilege.
    """
    found = []
    for name, agent in sorted(spec.agents.items()):
        if agent.inherited_from:
            continue
        cases = spec.home / "evals" / name / "cases"
        if cases.is_dir() and any(cases.glob("*.json")):
            found.append(name)
    return found


def emit_evals(spec: Spec, ctx: EmitContext) -> dict[str, Any] | None:
    """The eval workflow, or nothing when there is no agent this repository can evaluate."""
    agents = agents_with_cases(spec)
    if not agents:
        return None

    config = spec.manifest.evals
    judge = config.judge if config.judge in spec.agents else ""
    jobs: dict[str, Any] = {}
    for agent in agents:
        jobs.update(_agent_jobs(spec, ctx, agent, judge=judge))

    triggers: dict[str, Any] = {"workflow_dispatch": {}}
    if config.on_prompt_change:
        triggers["pull_request"] = {"paths": [spec.repo_path(path) for path in PROMPT_PATHS]}

    return {
        "name": "evals",
        "on": triggers,
        "permissions": {"contents": "read"},
        "concurrency": {"group": "evals-${{ github.ref }}", "cancel-in-progress": True},
        "env": {"OUTPUT_DIR": "outputs"},
        "jobs": jobs,
    }


def _slug(name: str) -> str:
    return name.replace("/", "-")


def _agent_jobs(spec: Spec, ctx: EmitContext, agent: str, *, judge: str) -> dict[str, Any]:
    key = _slug(agent)
    cases_dir = spec.repo_path(f"evals/{agent}/cases")
    inputs = f"outputs/evals/{key}/inputs"
    answers = f"outputs/evals/{key}/answers"
    verdicts = f"outputs/evals/{key}/verdicts"
    judge_inputs = f"outputs/evals/{key}/judge"

    jobs: dict[str, Any] = {
        f"cases-{key}": _exec_job(
            ctx,
            name=f"Cases for {agent}",
            steps=[
                {
                    "name": "Expand the cases into agent inputs",
                    "id": "cases",
                    "run": f"pipeline-exec eval-cases --cases={cases_dir} --output-dir={inputs}",
                }
            ],
            outputs={"cases": "${{ steps.cases.outputs.cases }}"},
        ),
        f"run-{key}": {
            "name": f"Run {agent}",
            "needs": f"cases-{key}",
            "strategy": {
                "fail-fast": False,
                "matrix": {"case": "${{ fromJSON(needs.cases-" + key + ".outputs.cases) }}"},
            },
            "uses": f"./{ctx.out_dir}/{lock_filename(agent, ctx.profile if ctx.multi_profile else None)}",
            "with": {
                "input_path": f"{inputs}/${{{{ matrix.case }}}}.json",
                "output_path": f"{answers}/${{{{ matrix.case }}}}.json",
            },
        },
    }

    grade_needs = [f"cases-{key}", f"run-{key}"]
    grade = f"pipeline-exec eval-grade --cases={cases_dir} --outputs={answers} --agent={agent}"
    grade += f" --output=outputs/evals/{key}.json"
    if spec.manifest.evals.min_pass_rate is not None:
        grade += f" --min-pass-rate={spec.manifest.evals.min_pass_rate}"

    if judge:
        jobs[f"prep-{key}"] = _exec_job(
            ctx,
            name=f"Rubrics for {agent}",
            needs=[f"cases-{key}", f"run-{key}"],
            steps=[
                {
                    "name": "Pair each rubric with the answer it is about",
                    "id": "prep",
                    "run": (
                        f"pipeline-exec eval-judge-prep --cases={cases_dir} "
                        f"--outputs={answers} --output-dir={judge_inputs}"
                    ),
                }
            ],
            outputs={"pending": "${{ steps.prep.outputs.pending }}"},
        )
        jobs[f"judge-{key}"] = {
            "name": f"Judge {agent}",
            "needs": f"prep-{key}",
            # No rubric to judge is not a failure; it is a suite whose cases are all deterministic.
            "if": "${{ needs.prep-" + key + ".outputs.pending != '[]' }}",
            "strategy": {
                "fail-fast": False,
                "matrix": {"case": "${{ fromJSON(needs.prep-" + key + ".outputs.pending) }}"},
            },
            "uses": f"./{ctx.out_dir}/{lock_filename(judge, ctx.profile if ctx.multi_profile else None)}",
            "with": {
                "input_path": f"{judge_inputs}/${{{{ matrix.case }}}}.json",
                "output_path": f"{verdicts}/${{{{ matrix.case }}}}.json",
            },
        }
        grade_needs.append(f"judge-{key}")
        grade += f" --judgements={verdicts}"

    jobs[f"grade-{key}"] = _exec_job(
        ctx,
        name=f"Grade {agent}",
        needs=grade_needs,
        # `!cancelled()` rather than success: a case whose agent run failed is a case the suite
        # should report on, not one that takes the report down with it.
        condition="${{ !cancelled() }}",
        steps=[{"name": "Grade the answers", "id": "grade", "run": grade}],
    )
    return jobs


def _exec_job(
    ctx: EmitContext,
    *,
    name: str,
    steps: list[dict[str, Any]],
    needs: list[str] | None = None,
    outputs: dict[str, str] | None = None,
    condition: str = "",
) -> dict[str, Any]:
    job: dict[str, Any] = {"name": name, "runs-on": ctx.runs_on, "permissions": {"contents": "read"}}
    if needs:
        job["needs"] = needs
    if condition:
        job["if"] = condition
    if outputs:
        job["outputs"] = outputs
    job["container"] = ctx.pins.exec_container()
    job["steps"] = [
        {"uses": ctx.pins.external_action("actions/checkout")},
        {"uses": ctx.pins.action("restore")},
        *steps,
        {"id": "save-workspace", "uses": ctx.pins.action("save"), "if": "${{ always() }}"},
    ]
    return job
