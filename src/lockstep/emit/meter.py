"""The job that says what a run cost.

Every agent in a compiled pipeline runs inside gh-aw, and gh-aw measures what it spent — credits,
tokens, and the model that spent them — into a `usage` artifact. Nothing in a pipeline read it.

This appends one job that does: it collects those artifacts, prices the credits against the rate
table in the manifest, writes OTLP metrics, and puts the total in the run's summary where somebody
will actually see it.

Three decisions worth stating.

**It runs when the pipeline failed.** A run that failed halfway still spent what it spent, and the
runs worth knowing the cost of are disproportionately the ones that went wrong. It does not run on
cancellation, where the workflow is being torn down and its own job would be killed alongside.

**It cannot fail the pipeline.** Metering is bookkeeping about work that is already finished. A
collector being down is not a reason to turn a green pipeline red, so the export step reports its
own failure and the job carries `continue-on-error`.

**It is off unless a rate table exists.** A cost report with no prices in it would be a column of
dollar signs in front of zeroes.
"""

from __future__ import annotations

import json
from typing import Any

from ..spec.model import Spec
from .context import EmitContext
from .profiles import resolve_value

USAGE_DIR = "outputs/usage"
METRICS_PATH = "outputs/otel/metrics.json"
PRICING_PATH = "outputs/otel/pricing.json"
JOBS_PATH = "outputs/otel/jobs.json"
HISTORY_DIR = "outputs/history"

# gh-aw names each agent's usage artifact `<prefix>usage`. One pattern collects every agent in the
# run without the compiler having to know what the prefixes came out as.
USAGE_PATTERN = "*usage*"


def meter_job(spec: Spec, ctx: EmitContext, *, needs: list[str], title: str) -> dict[str, Any] | None:
    """The metering job for one command workflow, or nothing when there is nothing to meter."""
    config = spec.manifest.otel
    history = spec.manifest.history
    # Either reason is enough. A repository may want a durable record without running a collector,
    # and a collector without keeping anything of its own.
    if not (config.enabled or history.enabled) or not needs:
        return None

    endpoint = ""
    if config.to_endpoint and config.endpoint:
        endpoint = resolve_value(
            config.endpoint,
            ctx.profile,
            location=spec.manifest.src.rel if spec.manifest.src else "pipeline.yaml",
        )

    command = [
        "pipeline-exec meter",
        f"--usage={USAGE_DIR}",
        f"--output={METRICS_PATH}",
        f"--pricing={PRICING_PATH}",
        f"--jobs={JOBS_PATH}",
        f"--service-name={config.service_name or spec.manifest.name}",
        f'--title="{title}"',
    ]
    if endpoint:
        command.append(f'--endpoint="{endpoint}"')
    if history.enabled:
        command.append(f"--history-dir={HISTORY_DIR}")

    steps: list[dict[str, Any]] = [
        {"uses": ctx.pins.external_action("actions/checkout")},
        {
            "name": "Collect what each agent reported spending",
            "uses": ctx.pins.external_action("actions/download-artifact"),
            "with": {
                "pattern": USAGE_PATTERN,
                "path": USAGE_DIR,
                "merge-multiple": False,
                # An agent that never ran uploaded nothing, and a pipeline whose conditional steps
                # all sat this one out is not a broken pipeline.
                "if-no-files-found": "ignore",
            },
            "continue-on-error": True,
        },
        {
            "name": "Ask how the run went",
            # Outcomes, durations and queue times for every job, which is where four of the five
            # questions an operator has are answered. `|| true`: this is bookkeeping, and a rate
            # limit on the reporting call is not a reason to fail a pipeline whose work is done.
            "run": (
                "mkdir -p outputs/otel && "
                "gh api --paginate "
                '"/repos/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID/jobs" '
                f"> {JOBS_PATH} || true"
            ),
            "env": {"GH_TOKEN": "${{ github.token }}"},
        },
        {
            "name": "Write the rate table",
            # Written from the spec rather than read from a file in the repository: the rates are
            # part of the compiled surface, so a change to them shows up in a diff and in the
            # semantic diff rather than in a data file nothing reviews.
            "run": _write_pricing(spec.manifest.otel.pricing),
        },
        {"name": "Price it", "id": "meter", "run": " ".join(command)},
    ]
    if history.enabled:
        steps.append(
            {
                "name": "Record the run",
                "uses": ctx.pins.action("publish-history"),
                "with": {"source": HISTORY_DIR, "branch": history.branch, "path": history.path},
            }
        )
    if config.to_artifact:
        steps.append(
            {
                "name": "Keep the metrics",
                "uses": ctx.pins.external_action("actions/upload-artifact"),
                "with": {"name": "otel-metrics", "path": METRICS_PATH, "if-no-files-found": "warn"},
            }
        )

    return {
        "name": "Meter the run",
        "needs": needs,
        # `!cancelled()` rather than `always()`, and the difference is not cosmetic. A job whose
        # condition does not mention `cancelled()` gets the skip-tolerant guard folded into it —
        # `!failure() && !cancelled() && …` — and an `always()` ANDed with `!failure()` is just
        # `!failure()`. The meter would then have skipped every run that failed, which is the exact
        # set of runs whose cost is most worth knowing.
        "if": "${{ !cancelled() }}",
        "runs-on": ctx.runs_on,
        "permissions": {
            # Writing the ledger needs a write, and it is the only one this job ever performs.
            "contents": "write" if history.enabled else "read",
            "actions": "read",
        },
        # Bookkeeping about finished work. A collector being down is not a red build.
        "continue-on-error": True,
        "container": ctx.container(),
        "steps": steps,
    }


def _write_pricing(pricing: dict[str, float]) -> str:
    """A heredoc rather than an `echo`, so a rate table is readable in the workflow it lives in."""
    body = json.dumps(pricing, indent=2, sort_keys=True)
    return f"mkdir -p outputs/otel\ncat > {PRICING_PATH} <<'PRICING'\n{body}\nPRICING"
