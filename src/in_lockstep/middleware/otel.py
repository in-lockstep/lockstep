"""Spans and metrics for every call.

Default-on, and degrades to a no-op recorder when the OTel SDK is absent so that observability is
never the reason a run cannot start. Metric dimensions are bounded to verb, adapter, status and
`decided` — run ids live on spans, not on metrics, because a run id as a metric dimension is an
unbounded cardinality explosion.

`decided` is a dimension because without it a nightly eval suite that judged nothing emits
SUCCEEDED, pages nobody, and reads green forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.middleware import ActionCall, Next
from ..core.outcome import Outcome
from ..privileged import sink


@dataclass
class SpanRecord:
    name: str
    attributes: dict[str, object] = field(default_factory=dict)


@dataclass
class MetricRecord:
    name: str
    value: float
    dimensions: dict[str, str] = field(default_factory=dict)


class Recorder:
    """Collects spans and metrics in-process. Real exporters wrap this in phase 4."""

    def __init__(self) -> None:
        self.spans: list[SpanRecord] = []
        self.metrics: list[MetricRecord] = []

    def span(self, name: str, **attributes: object) -> SpanRecord:
        record = SpanRecord(name=name, attributes=sink.attributes(attributes))
        self.spans.append(record)
        return record

    def metric(self, name: str, value: float, **dimensions: str) -> None:
        self.metrics.append(MetricRecord(name=name, value=value, dimensions=dict(dimensions)))


def otel(recorder: Recorder | None = None) -> OtelMiddleware:
    return OtelMiddleware(recorder or Recorder())


class OtelMiddleware:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

    async def __call__(self, ctx: object, call: ActionCall, next: Next) -> Outcome[object]:
        verb = call.verb.value if call.verb else call.iface.__name__.lower()
        run_id = getattr(ctx, "run_id", "")
        span = self.recorder.span(
            f"action {verb}",
            **{"in_lockstep.verb": verb, "in_lockstep.run_id": run_id},
        )

        outcome = await next()

        span.attributes["in_lockstep.status"] = outcome.status.value
        span.attributes["in_lockstep.decided"] = outcome.decided
        if outcome.reason:
            span.attributes["in_lockstep.reason"] = outcome.reason

        # A call-scoped adapter (`via=`) is the one that actually served, so it is the one the
        # metric names; only a container-resolved call falls back to the binding.
        if call.via is not None:
            adapter = type(call.via).__name__
        elif getattr(ctx, "container", None) is not None and ctx.container.has(call.iface):  # type: ignore[attr-defined]
            adapter = type(ctx.container.resolve(call.iface)).__name__  # type: ignore[attr-defined]
        else:
            adapter = "unknown"
        dimensions = {
            "verb": verb,
            "adapter": adapter,
            "status": outcome.status.value,
            "decided": str(outcome.decided).lower(),
        }
        self.recorder.metric("in_lockstep.action.duration", outcome.cost.wall_seconds, **dimensions)
        self.recorder.metric("in_lockstep.action.outcome", 1.0, **dimensions)
        if outcome.cost.usd:
            self.recorder.metric("in_lockstep.cost.usd", outcome.cost.usd, **dimensions)
        if outcome.cost.total_tokens:
            self.recorder.metric("gen_ai.client.token.usage", float(outcome.cost.total_tokens), **dimensions)
        # Omitted when there is nothing to report, never defaulted. A gauge that reads 1.0 for a
        # run that spent no tokens is a dashboard saying pricing coverage is perfect because
        # nothing happened — the same shape as a suite reporting 100% having judged nothing.
        priced = outcome.cost.priced_fraction
        if priced is not None:
            self.recorder.metric("in_lockstep.cost.priced_fraction", priced, **dimensions)
        return outcome
