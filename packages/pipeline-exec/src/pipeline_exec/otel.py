"""What a pipeline run consumed, in credits and in dollars, as OpenTelemetry metrics.

A pipeline's bill is not a thing this framework measures. gh-aw measures it — every agent run
uploads a usage artifact carrying the credits it spent, the tokens behind them and the model that
spent them — and this turns that into two things the substrate does not provide: a number in the
currency the person approving the budget actually uses, and a metric an observability backend can
hold a dashboard against.

Three things worth reading before trusting the number.

**Credits are measured, dollars are derived.** The credit figure comes from the substrate. The dollar
figure is that figure multiplied by a rate somebody wrote in a manifest, so it is a *statement about
price* rather than an observation, and it is only as current as that table. The rate used is recorded
alongside every number, because a report you cannot reproduce is a report you cannot argue with.

**A model with no rate is unpriced, never free.** The failure mode this module exists to avoid is a
cost report that says $0.00 because it did not recognise a model name. An unpriced model is carried
through to the output and named, and the total says how much of itself it could not price.

**The shape of gh-aw's usage artifact is not a stability contract.** It belongs to a tool this
repository pins but does not own. So the reader takes what it recognises rather than asserting a
schema, records where each number came from, and reports finding nothing as *nothing found* rather
than as zero. The first real run is what confirms it, and `--explain` is what you read to confirm it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The names gh-aw has used for the numbers we need. Declared rather than inferred, so that a shape
# this does not recognise produces a report that says so instead of a plausible total.
CREDIT_KEYS = ("ai_credits", "aic", "credits")
TOKEN_KEYS = ("total_tokens", "tokens", "effective_tokens")
INPUT_KEYS = ("input_tokens", "prompt_tokens")
OUTPUT_KEYS = ("output_tokens", "completion_tokens")
MODEL_KEYS = ("model", "model_name")
LABEL_KEYS = ("workflow", "workflow_id", "workflow_name", "agent", "name")

# gh-aw's own dollar estimate, when it publishes one. Read as a cross-check, never as the answer:
# the whole point of the pricing table is that an organization's rate is not the list rate.
COST_KEYS = ("cost", "cost_usd", "estimated_cost")


@dataclass
class Record:
    """One measurement: what a model spent, and where the number was found."""

    credits: float
    model: str = ""
    label: str = ""
    tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reported_cost: float | None = None
    source: str = ""
    # The file this came from, and the path to it inside that file. Kept structurally rather than
    # as text because both ancestry and per-file grouping depend on them, and substring tests over
    # a rendered path get those subtly wrong.
    file: str = ""
    path: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model or "(unnamed)",
            "label": self.label,
            "credits": self.credits,
            "tokens": self.tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reported_cost": self.reported_cost,
            "source": self.source,
        }


def read_usage(directory: Path) -> tuple[list[Record], list[Record]]:
    """Every usage record under a directory, split into measurements and roll-ups.

    Returns (records, rollups). A JSON object carrying a credit figure is a candidate; one that
    *contains* another candidate is an aggregate of it rather than a second measurement. Summing
    both would double a bill, so ancestors are separated out and kept only as a cross-check.
    """
    candidates: list[Record] = []
    for path in sorted(directory.rglob("*")):
        if path.suffix not in (".json", ".jsonl") or not path.is_file():
            continue
        # Relative to the directory, not the basename: several agents' artifacts merged into one
        # tree all carry the same file names, and grouping them together would compare one agent's
        # total against every agent's records.
        relative = str(path.relative_to(directory))
        for index, document in enumerate(_documents(path)):
            candidates.extend(_walk(document, (str(index),), relative))

    records: list[Record] = []
    rollups: list[Record] = []
    for record in candidates:
        contains_another = any(
            other is not record and other.file == record.file and _is_prefix(record.path, other.path)
            for other in candidates
        )
        (rollups if contains_another else records).append(record)
    return records, rollups


def _documents(path: Path) -> list[Any]:
    """A .json file is one document; a .jsonl file is one per line. A bad line is skipped, not fatal."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".json":
        try:
            return [json.loads(text)]
        except json.JSONDecodeError:
            return []
    documents = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            documents.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return documents


def _walk(node: Any, path: tuple[str, ...], origin: str) -> list[Record]:
    found: list[Record] = []
    if isinstance(node, dict):
        credits = _number(node, CREDIT_KEYS)
        if credits is not None:
            found.append(_record(node, credits, path, origin))
        for key, value in node.items():
            found.extend(_walk(value, (*path, str(key)), origin))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_walk(value, (*path, str(index)), origin))
    return found


def _record(node: dict[str, Any], credits: float, path: tuple[str, ...], origin: str) -> Record:
    # A `by_model` map keys the model by the field name rather than storing it, so the enclosing key
    # is the model when the object itself does not name one.
    model = _text(node, MODEL_KEYS)
    if not model and len(path) >= 2 and path[-2] in ("by_model", "models", "per_model"):
        model = path[-1]
    return Record(
        credits=credits,
        model=model,
        label=_text(node, LABEL_KEYS),
        tokens=int(_number(node, TOKEN_KEYS) or 0),
        input_tokens=int(_number(node, INPUT_KEYS) or 0),
        output_tokens=int(_number(node, OUTPUT_KEYS) or 0),
        reported_cost=_number(node, COST_KEYS),
        source=f"{origin}:{'.'.join(path[1:]) or '(root)'}",
        file=origin,
        path=path,
    )


def _is_prefix(shorter: tuple[str, ...], longer: tuple[str, ...]) -> bool:
    return len(shorter) < len(longer) and longer[: len(shorter)] == shorter


def _number(node: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = node.get(key)
        # `bool` is an `int`; a flag read as a credit figure would be a 1 nobody spent.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return float(value)
    return None


def _text(node: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


@dataclass
class Priced:
    """The run's consumption, priced against a rate table."""

    records: list[Record] = field(default_factory=list)
    rates: dict[str, float] = field(default_factory=dict)
    # The aggregates the reader set aside. Not summed into the bill — that is what would double it —
    # but kept, because gh-aw's own total for a file is the one number that can tell us our reading
    # of its shape was wrong.
    rollups: list[Record] = field(default_factory=list)

    @property
    def credits(self) -> float:
        return sum(record.credits for record in self.records)

    @property
    def tokens(self) -> int:
        return sum(record.tokens for record in self.records)

    def rate_for(self, model: str) -> float | None:
        """The rate for a model, longest matching prefix first.

        A prefix match because `claude-sonnet-4-6-20260101` and `claude-sonnet-4-6` are the same
        price, and a table that had to name every dated snapshot would be a table that silently
        stopped pricing things the day a provider published one.
        """
        if model in self.rates:
            return self.rates[model]
        matches = [name for name in self.rates if model.startswith(name)]
        return self.rates[max(matches, key=len)] if matches else None

    @property
    def priced_credits(self) -> float:
        return sum(r.credits for r in self.records if self.rate_for(r.model) is not None)

    @property
    def dollars(self) -> float:
        total = 0.0
        for record in self.records:
            rate = self.rate_for(record.model)
            if rate is not None:
                total += record.credits * rate
        return round(total, 6)

    @property
    def unpriced_models(self) -> list[str]:
        return sorted({r.model or "(unnamed)" for r in self.records if self.rate_for(r.model) is None})

    def by_model(self) -> dict[str, dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for record in self.records:
            key = record.model or "(unnamed)"
            entry = grouped.setdefault(
                key, {"credits": 0.0, "tokens": 0, "rate": self.rate_for(record.model), "dollars": None}
            )
            entry["credits"] += record.credits
            entry["tokens"] += record.tokens
        for entry in grouped.values():
            rate = entry["rate"]
            entry["dollars"] = round(entry["credits"] * rate, 6) if rate is not None else None
        return grouped

    def crosscheck(self) -> dict[str, Any]:
        """Our sum against gh-aw's own, where it published one.

        The reader infers which objects in an artifact are measurements and which are totals of
        them. That inference is the part most likely to be wrong after an upstream change, and it
        fails *quietly* — a double count or a missed file still produces a confident number. So the
        totals gh-aw wrote are compared against the totals we computed, and a disagreement is
        reported rather than resolved: which of the two is right is not something this can know.
        """
        reported = 0.0
        computed = 0.0
        sources: list[str] = []
        for name in sorted({record.file for record in self.rollups}):
            outermost = [
                record
                for record in self.rollups
                if record.file == name
                and not any(
                    other is not record and other.file == name and _is_prefix(other.path, record.path)
                    for other in self.rollups
                )
            ]
            reported += sum(record.credits for record in outermost)
            # Only the files that published a total take part. A file with no roll-up has nothing to
            # be checked against, and counting its records against another file's total is how a
            # perfectly healthy multi-agent run starts reporting that it does not reconcile.
            computed += sum(record.credits for record in self.records if record.file == name)
            sources.extend(record.source for record in outermost)

        if not sources:
            return {"available": False}
        return {
            "available": True,
            "reported_credits": round(reported, 4),
            "computed_credits": round(computed, 4),
            # Not exact equality: these are floats, and a run whose artifact rounds its own total is
            # not a run whose accounting is broken.
            "agrees": abs(reported - computed) <= max(0.01, reported * 0.001),
            "sources": sources,
        }

    def summary(self) -> dict[str, Any]:
        """The answer, with everything needed to argue with it.

        `priced_fraction` is the part worth reading: a total that covers three quarters of the
        credits is not a total, and a report that did not say so would be worse than no report.
        """
        credits = self.credits
        return {
            "crosscheck": self.crosscheck(),
            "credits": round(credits, 4),
            "tokens": self.tokens,
            "dollars": self.dollars,
            "priced_credits": round(self.priced_credits, 4),
            "priced_fraction": round(self.priced_credits / credits, 4) if credits else 1.0,
            "unpriced_models": self.unpriced_models,
            "by_model": self.by_model(),
            "records": len(self.records),
            # gh-aw's own figure where it publishes one. Kept beside ours rather than instead of it:
            # a list rate and a negotiated rate disagreeing is information, not an error.
            "reported_cost": self._reported_cost(),
        }

    def _reported_cost(self) -> float | None:
        """gh-aw's own dollar figure, preferring the totals it wrote over the leaves."""
        for group in (self.rollups, self.records):
            costs = [record.reported_cost for record in group if record.reported_cost is not None]
            if costs:
                return round(sum(costs), 6)
        return None


def _render_shape(jobs: list[Job]) -> list[str]:
    """What the run did, before what it cost.

    Cost is the question people ask last and the only one credits answer. Whether it worked, how
    long it took and how long it waited are the ones an operator asks first.
    """
    if not jobs:
        return []
    shape = run_shape(jobs)
    failed = shape["failed"]
    verdict = "passed" if not failed else f"**{len(failed)} job(s) failed**"
    lines = [
        f"{verdict} · {shape['jobs']} job(s) · {shape['wall_seconds']:g}s wall, "
        f"{shape['busy_seconds']:g}s runner time"
        + (f" · {shape['pickup_seconds']:g}s to pick up" if shape["pickup_seconds"] else ""),
        "",
    ]
    if failed:
        lines += ["Failed: " + ", ".join(f"`{name}`" for name in failed), ""]
    return lines


def price(records: list[Record], rates: dict[str, float], rollups: list[Record] | None = None) -> Priced:
    return Priced(records=records, rates=dict(rates), rollups=list(rollups or []))


# --- OTLP ---------------------------------------------------------------------------------------
#
# OTLP/HTTP in its JSON encoding, which is what a collector accepts on /v1/metrics and what an
# artifact can hold without a protobuf toolchain on the runner.

SCOPE = {"name": "lockstep", "version": "1"}


def _attributes(pairs: dict[str, Any]) -> list[dict[str, Any]]:
    attributes = []
    for key, value in pairs.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            entry: dict[str, Any] = {"boolValue": value}
        elif isinstance(value, int):
            entry = {"intValue": str(value)}
        elif isinstance(value, float):
            entry = {"doubleValue": value}
        else:
            entry = {"stringValue": str(value)}
        attributes.append({"key": key, "value": entry})
    return attributes


def _gauge(name: str, unit: str, description: str, points: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": name, "unit": unit, "description": description, "gauge": {"dataPoints": points}}


def _point(value: float, attributes: dict[str, Any], nanos: int) -> dict[str, Any]:
    key = "asInt" if isinstance(value, int) else "asDouble"
    return {key: value, "timeUnixNano": str(nanos), "attributes": _attributes(attributes)}


def metrics_document(
    priced: Priced,
    *,
    resource: dict[str, Any],
    nanos: int,
    jobs: list[Job] | None = None,
    attempt: int = 1,
) -> dict[str, Any]:
    """One OTLP metrics document for a run.

    Gauges rather than counters. A counter carries an aggregation temporality and a start time that
    say "this is a running total the exporter has been maintaining", and this exporter runs once at
    the end of a job and has maintained nothing. Recording a per-run measurement as a gauge is the
    honest shape; the summing over a period is the backend's job, or the rollup's.
    """
    summary = priced.summary()
    points = [
        _gauge(
            "lockstep.run.credits",
            "{credit}",
            "AI credits consumed by one pipeline run, as measured by gh-aw.",
            [_point(summary["credits"], {}, nanos)],
        ),
        _gauge(
            "lockstep.run.tokens",
            "{token}",
            "Tokens consumed by one pipeline run.",
            [_point(summary["tokens"], {}, nanos)],
        ),
        _gauge(
            "lockstep.run.cost.usd",
            "USD",
            "Credits priced against the configured rate table. Derived, not measured.",
            [_point(summary["dollars"], {}, nanos)],
        ),
        _gauge(
            "lockstep.run.priced_fraction",
            "1",
            "Share of this run's credits the rate table could price. Below 1, the cost is partial.",
            [_point(summary["priced_fraction"], {}, nanos)],
        ),
    ]
    # Split by model, tagged with the GenAI semantic conventions so that a backend which already
    # understands agent workloads groups these without being taught to.
    per_model, per_model_tokens, per_model_cost = [], [], []
    for model, entry in sorted(summary["by_model"].items()):
        attributes = {
            "gen_ai.request.model": model,
            "gen_ai.system": gen_ai_system(model),
            "lockstep.priced": entry["rate"] is not None,
        }
        per_model.append(_point(entry["credits"], attributes, nanos))
        per_model_tokens.append(_point(entry["tokens"], attributes, nanos))
        if entry["dollars"] is not None:
            per_model_cost.append(_point(entry["dollars"], attributes, nanos))
    if per_model:
        points += [
            _gauge(
                "lockstep.run.credits.by_model",
                "{credit}",
                "AI credits consumed by one pipeline run, split by model.",
                per_model,
            ),
            _gauge(
                "lockstep.run.tokens.by_model",
                "{token}",
                "Tokens consumed by one pipeline run, split by model.",
                per_model_tokens,
            ),
        ]
    if per_model_cost:
        points.append(
            _gauge(
                "lockstep.run.cost.usd.by_model",
                "USD",
                "Cost of one pipeline run, split by model. Derived from the rate table.",
                per_model_cost,
            )
        )

    points += _run_metrics(jobs or [], attempt=attempt, nanos=nanos)
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": _attributes(resource)},
                "scopeMetrics": [{"scope": SCOPE, "metrics": points}],
            }
        ]
    }


def _run_metrics(jobs: list[Job], *, attempt: int, nanos: int) -> list[dict[str, Any]]:
    """Did it work, how long did it take, and how long did it wait.

    Deliberately per job as well as per run. "The pipeline is slow" and "one reviewer is slow and
    the other eleven wait for it" produce the same run duration and want different fixes.
    """
    if not jobs:
        return []
    shape = run_shape(jobs)
    metrics = [
        _gauge(
            "lockstep.run.duration",
            "s",
            "Wall clock across the run's jobs. Not their sum: a fan-out runs at once.",
            [_point(shape["wall_seconds"], {}, nanos)],
        ),
        _gauge(
            "lockstep.run.busy",
            "s",
            "Runner time the run consumed, summed across jobs. This is what it costs in minutes.",
            [_point(shape["busy_seconds"], {}, nanos)],
        ),
        _gauge(
            "lockstep.run.attempt",
            "1",
            "Which attempt this was. Above 1, somebody re-ran a pipeline that failed.",
            [_point(attempt, {}, nanos)],
        ),
        _gauge(
            "lockstep.run.jobs",
            "{job}",
            "Jobs in the run, by how they ended. The success rate a dashboard is built on.",
            [
                _point(count, {"lockstep.outcome": outcome}, nanos)
                for outcome, count in sorted(shape["outcomes"].items())
            ],
        ),
        _gauge(
            "lockstep.job.duration",
            "s",
            "How long each job took, so a slow pipeline can be attributed to a slow step.",
            [
                _point(job.duration_seconds or 0.0, _job_attributes(job), nanos)
                for job in jobs
                if job.duration_seconds is not None
            ],
        ),
        _gauge(
            "lockstep.run.pickup",
            "s",
            "Delay before the run's first job started. Work not being picked up shows up here.",
            [_point(shape["pickup_seconds"], {}, nanos)],
        ),
        _gauge(
            "lockstep.job.start_delay",
            "s",
            "Delay from run creation to each job starting. Includes waiting for upstream jobs.",
            [
                _point(job.start_delay_seconds or 0.0, _job_attributes(job), nanos)
                for job in jobs
                if job.start_delay_seconds is not None
            ],
        ),
    ]
    return [metric for metric in metrics if metric["gauge"]["dataPoints"]]


def _job_attributes(job: Job) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "cicd.pipeline.task.name": job.name,
        "lockstep.outcome": job.outcome,
        "lockstep.job.agentic": job.agentic,
    }
    if job.agentic:
        # The agent a job ran, under the convention gh-aw names its workflows by. A per-agent
        # error rate is the answer to "where should improvement effort go", and it cannot be
        # computed from a job name a dashboard has to parse.
        attributes["gen_ai.agent.name"] = job.name.split("aw-", 1)[-1].strip()
        attributes["gen_ai.operation.name"] = "invoke_agent"
    return attributes


def render_summary(priced: Priced, *, title: str, jobs: list[Job] | None = None) -> str:
    """The run's bill and its shape, for the job summary — where somebody will actually see it."""
    summary = priced.summary()
    lines = [f"### {title}", ""]
    lines += _render_shape(jobs or [])
    if not priced.records:
        lines += [
            "No usage records were found for this run.",
            "",
            "This is reported as *nothing found* rather than as a cost of zero. Either no agent ran,",
            "or the usage artifact was not where the meter looked — `pipeline-exec meter --explain`",
            "prints every file it read.",
        ]
        return "\n".join(lines) + "\n"

    lines += [
        "| | |",
        "|---|---|",
        f"| Credits | {summary['credits']:g} |",
        f"| Tokens | {summary['tokens']:,} |",
        f"| Cost | ${summary['dollars']:,.4f} |",
    ]
    if summary["reported_cost"] is not None:
        lines.append(f"| Cost reported by gh-aw | ${summary['reported_cost']:,.4f} |")

    check = summary["crosscheck"]
    if check.get("available") and not check["agrees"]:
        lines += [
            "",
            f"> **These credits do not reconcile.** gh-aw reported {check['reported_credits']:g} for "
            f"the files that published a total; adding up the records in those same files gives "
            f"{check['computed_credits']:g}. The "
            "meter reads a shape it does not own, so the likeliest explanation is that the shape "
            "changed and this reading of it is now wrong. `pipeline-exec meter --explain` prints "
            "every number it matched and where it came from.",
        ]
    lines += ["", "| Model | Credits | Rate | Cost |", "|---|---|---|---|"]
    for model, entry in sorted(summary["by_model"].items()):
        rate = "—" if entry["rate"] is None else f"${entry['rate']:g}/credit"
        cost = "unpriced" if entry["dollars"] is None else f"${entry['dollars']:,.4f}"
        lines.append(f"| `{model}` | {entry['credits']:g} | {rate} | {cost} |")

    if summary["unpriced_models"]:
        share = 1 - summary["priced_fraction"]
        lines += [
            "",
            f"**${summary['dollars']:,.4f} covers {summary['priced_fraction']:.0%} of this run's "
            f"credits.** {share:.0%} went to models with no rate in the table: "
            + ", ".join(f"`{name}`" for name in summary["unpriced_models"])
            + ". The cost above is a floor, not a total.",
        ]
    return "\n".join(lines) + "\n"


# --- what the run did, as distinct from what it spent ---------------------------------------------
#
# Cost is one of five questions an operator has, and the only one credits answer. The others — is it
# working, what just happened, is it getting better, where should effort go — are questions about
# outcomes, timings and rates. Those are all observable here without any new instrumentation: the
# Actions jobs API knows when every job was created, started and finished, and how it ended.
#
# What is deliberately absent is content. No prompt, completion, diff or source reaches these
# metrics, which is what makes exporting them to a shared or vendor backend a decision nobody has to
# think hard about. Reasoning lives in gh-aw's own artifacts, under that repository's access
# controls, and this does not copy it anywhere.

# The provider behind a model name, for `gen_ai.system`. A GenAI-aware backend groups by it, and
# guessing wrong is worse than leaving it off — so an unrecognised family produces no attribute.
GEN_AI_SYSTEMS = (
    ("claude", "anthropic"),
    ("gpt", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("codex", "openai"),
    ("gemini", "gcp.gemini"),
    ("copilot", "github.copilot"),
)


def gen_ai_system(model: str) -> str:
    name = model.lower()
    for prefix, system in GEN_AI_SYSTEMS:
        if name.startswith(prefix):
            return system
    return ""


@dataclass
class Job:
    """One job of the run, as the Actions API reports it."""

    name: str
    conclusion: str = ""
    status: str = ""
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""

    @property
    def start_delay_seconds(self) -> float | None:
        """How long after the run was created this job started.

        Deliberately *not* called queue time. The Actions API stamps every job's `created_at` when
        the run is created, so for a job with dependencies this figure is mostly the time its
        upstreams took — calling that a queue would report a healthy pipeline as starved of runners.
        Only the earliest job's figure is a runner-availability signal, and `run_shape` reports that
        one separately as `pickup_seconds`.
        """
        return _elapsed(self.created_at, self.started_at)

    @property
    def duration_seconds(self) -> float | None:
        return _elapsed(self.started_at, self.completed_at)

    @property
    def agentic(self) -> bool:
        """gh-aw's compiled workflows are named for the agent they run."""
        return self.name.startswith("aw-") or "aw-" in self.name

    @property
    def outcome(self) -> str:
        return self.conclusion or self.status or "unknown"


def _elapsed(start: str, end: str) -> float | None:
    from datetime import datetime

    if not start or not end:
        return None
    try:
        began = datetime.fromisoformat(start.replace("Z", "+00:00"))
        ended = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    seconds = (ended - began).total_seconds()
    return round(seconds, 3) if seconds >= 0 else None


def read_jobs(path: Path) -> list[Job]:
    """The run's jobs, from a saved `/actions/runs/{id}/jobs` response.

    A paginated fetch may have concatenated several pages, so both a bare list and the API's
    `{"jobs": [...]}` envelope are accepted. The metering job's own entry is dropped: it is still
    running while it reads this, and a job cannot honestly report its own duration.
    """
    jobs: list[Job] = []
    for document in _documents(path) if path.is_file() else []:
        entries = document.get("jobs", []) if isinstance(document, dict) else document
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            if not name or entry.get("status") == "in_progress":
                continue
            jobs.append(
                Job(
                    name=name,
                    conclusion=str(entry.get("conclusion") or ""),
                    status=str(entry.get("status") or ""),
                    created_at=str(entry.get("created_at") or ""),
                    started_at=str(entry.get("started_at") or ""),
                    completed_at=str(entry.get("completed_at") or ""),
                )
            )
    return jobs


def run_shape(jobs: list[Job]) -> dict[str, Any]:
    """What happened, in the terms an operator asks about it."""
    outcomes: dict[str, int] = {}
    for job in jobs:
        outcomes[job.outcome] = outcomes.get(job.outcome, 0) + 1
    durations = [job.duration_seconds for job in jobs if job.duration_seconds is not None]
    delays = [job.start_delay_seconds for job in jobs if job.start_delay_seconds is not None]
    agent_jobs = [job for job in jobs if job.agentic]
    return {
        "jobs": len(jobs),
        "outcomes": outcomes,
        "failed": sorted(job.name for job in jobs if job.outcome == "failure"),
        "agent_jobs": len(agent_jobs),
        "agents_failed": sorted(job.name for job in agent_jobs if job.outcome == "failure"),
        # Wall clock across the whole run rather than the sum of its jobs: a fan-out of twelve
        # reviewers that finishes in four minutes took four minutes, and reporting forty-eight would
        # describe a pipeline nobody ran.
        "wall_seconds": round(max(durations) if durations else 0.0, 3),
        "busy_seconds": round(sum(durations), 3),
        # The first job to start is the only one whose delay is about runner availability rather
        # than about waiting for the job before it. That one answers "is work being picked up".
        "pickup_seconds": round(min(delays) if delays else 0.0, 3),
    }


# --- what is kept -------------------------------------------------------------------------------
#
# Metrics go to a collector, which is where trends belong. But a collector is a thing an
# organization has to run, and a repository without one still needs to answer "did that prompt
# change help" three months later — which artifacts expiring and job logs rotating make impossible.
#
# So one line per run is written to a branch. Small enough that ten thousand runs are a few
# megabytes, plain enough to read with `grep`, and durable in the only place a repository always has.
#
# What it deliberately does not carry is content. No prompt, completion, diff or source — the same
# line the metrics draw, for the same reason: this file is as readable as the repository, and a
# transcript in it is a transcript in everybody's clone forever. The reasoning stays in gh-aw's own
# artifacts under that repository's access controls, and the record points at the run that holds it.


def run_record(
    priced: Priced,
    jobs: list[Job],
    *,
    identity: dict[str, Any],
    attempt: int = 1,
) -> dict[str, Any]:
    """One run, as the line that outlives it."""
    summary = priced.summary()
    shape = run_shape(jobs) if jobs else {}
    return {
        "run_id": identity.get("run_id", ""),
        "run_url": identity.get("run_url", ""),
        "workflow": identity.get("workflow", ""),
        "event": identity.get("event", ""),
        "ref": identity.get("ref", ""),
        "sha": identity.get("sha", ""),
        "finished": identity.get("finished", ""),
        "attempt": attempt,
        "credits": summary["credits"],
        "tokens": summary["tokens"],
        "cost_usd": summary["dollars"],
        # Recorded beside the cost, because a total covering three quarters of a run is not a total
        # and a reader three months from now has no other way to know which they are looking at.
        "priced_fraction": summary["priced_fraction"],
        "models": {model: entry["credits"] for model, entry in summary["by_model"].items()},
        "wall_seconds": shape.get("wall_seconds", 0.0),
        "busy_seconds": shape.get("busy_seconds", 0.0),
        "jobs": shape.get("jobs", 0),
        "outcomes": shape.get("outcomes", {}),
        "failed": shape.get("failed", []),
        # Per agent, because "which lens is failing" is the question a retro asks first.
        "agents": {
            job.name.split("aw-", 1)[-1]: {
                "outcome": job.outcome,
                "seconds": job.duration_seconds or 0.0,
            }
            for job in jobs
            if job.agentic
        },
    }


def history_line(record: dict[str, Any]) -> str:
    """JSON on one line, keys sorted, so a diff of the ledger reads as one run added."""
    return json.dumps(record, sort_keys=True) + "\n"


def history_file(finished: str, *, path: str = "history") -> str:
    """Sharded by month.

    One file per run makes a directory git walks slowly; one file forever makes a diff nobody can
    read. A month is small enough to append to and large enough that the count stays bounded.
    """
    month = (finished or "")[:7] or "unknown"
    return f"{path}/{month}.jsonl"
