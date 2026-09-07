"""A verb that needs a model capability is refused a model that does not have it. Discharges
`GATE-MODEL-1`.

`ModelCaps.structured_output` was declared for five providers, one of them `False`, and read by
nothing but a docstring promising this check (#274). So an adopter who routed a verb at a model
that could not answer with a schema found out from `*.unparseable` after two paid calls, when the
registration had known all along. The check lives in `AiInvoker.run`, which is the thing that
knows the model, and every shipped verb tells it the shape it needs.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from in_lockstep.adapters.ai import AiReview, Review
from in_lockstep.ai.invoker import AiInvoker, InvocationBlocked, InvokePolicy
from in_lockstep.ai.pricing import CostTable, Rate
from in_lockstep.ai.tools import Tool, ToolSet
from in_lockstep.core.outcome import Status
from in_lockstep.core.spend import Budget, Spend
from in_lockstep.llm.interface import LLMProvider
from in_lockstep.llm.registry import Model, ModelCaps
from in_lockstep.llm.types import LLMInput, LLMOutput, Message
from in_lockstep.privileged.egress import UnsandboxedEgress

SCHEMA = {"type": "object", "required": ["findings"], "properties": {"findings": {"type": "array"}}}


class _Counting(LLMProvider):
    """A provider that answers cleanly and counts how often it was asked to."""

    def __init__(self) -> None:
        self.calls: list[LLMInput] = []

    def name(self) -> str:
        return "stub"

    async def generate(self, input: LLMInput) -> LLMOutput:
        self.calls.append(input)
        return LLMOutput(content='{"findings": []}')


def _invoker(caps: ModelCaps | None, provider: LLMProvider | None = None) -> AiInvoker:
    table = CostTable()
    table.add("m", Rate(3.0, 15.0))
    return AiInvoker(
        provider or _Counting(),
        model="m",
        cost_table=table,
        spend=Spend(budget=Budget(usd=5.0)),
        egress=UnsandboxedEgress(),
        caps=caps,
    )


def _run(invoker: AiInvoker, **kwargs: Any) -> Any:
    return asyncio.run(
        invoker.run(
            system="s",
            messages=[Message(role="user", content="go")],
            policy=InvokePolicy(max_turns=1),
            **kwargs,
        )
    )


def _tool() -> Tool:
    return Tool(server="workspace", name="read_file", description="", parameters={}, capabilities=frozenset())


# -- the refusal ------------------------------------------------------------------------------------


def test_gate_model_1_a_schema_call_is_refused_by_name_on_a_model_registered_without_structured_output():
    provider = _Counting()
    invoker = _invoker(ModelCaps(structured_output=False), provider)
    with pytest.raises(InvocationBlocked) as refused:
        _run(invoker, schema=SCHEMA)
    assert refused.value.reason == "model.no_structured_output"
    assert "'m'" in str(refused.value) and "structured_output" in str(refused.value)
    assert provider.calls == [], "nothing was sent and nothing was charged"


def test_a_call_that_needs_no_schema_is_not_refused_for_want_of_one():
    provider = _Counting()
    invocation = _run(_invoker(ModelCaps(structured_output=False), provider))
    assert invocation.content == '{"findings": []}'
    assert len(provider.calls) == 1


def test_gate_model_1_a_session_handing_tools_is_refused_on_a_registration_without_tool_use():
    provider = _Counting()
    invoker = _invoker(ModelCaps(tool_use=False), provider)
    with pytest.raises(InvocationBlocked) as refused:
        _run(invoker, tools=ToolSet.of(_tool()), schema=SCHEMA)
    assert refused.value.reason == "model.no_tool_use"
    assert "1 tool(s)" in str(refused.value) and "tool_use" in str(refused.value)
    assert provider.calls == []


def test_a_toolless_call_on_a_registration_without_tool_use_proceeds():
    provider = _Counting()
    _run(_invoker(ModelCaps(tool_use=False), provider), tools=ToolSet.none(), schema=SCHEMA)
    assert len(provider.calls) == 1


def test_an_invoker_that_declared_no_capabilities_is_not_refused():
    """A capability nobody declared is not a capability nobody has. Unlike residency, this is not a
    control where silence must fail closed: a wrong refusal costs a run somebody wanted, and a
    wrong pass costs one call the parser then refuses."""
    provider = _Counting()
    _run(_invoker(None, provider), tools=ToolSet.of(_tool()), schema=SCHEMA)
    assert len(provider.calls) == 1


def test_the_capability_refusal_comes_before_pricing():
    """Whether a model can do the job is a prior question to what it would cost, and the message a
    person reads should be the one about the line they can change."""
    invoker = _invoker(ModelCaps(structured_output=False))
    invoker.cost_table = CostTable()  # nothing priced, so pricing would refuse too
    with pytest.raises(InvocationBlocked) as refused:
        _run(invoker, schema=SCHEMA)
    assert refused.value.reason == "model.no_structured_output"


# -- the live path ----------------------------------------------------------------------------------


def test_gate_model_1_a_review_run_reports_the_refusal_as_blocked_and_pays_nothing():
    """Through the adapter, because the gate is about a verb: `AiReview` passes its schema, the
    invoker refuses, and the outcome is `BLOCKED` naming the reason -- the adopter's choice
    declined, not a failed review."""
    provider = _Counting()
    adapter = AiReview(lambda ctx: _invoker(ModelCaps(structured_output=False), provider))
    outcome = asyncio.run(adapter.invoke(None, Review(base="a", head="b", diff="- a\n+ b\n")))
    assert outcome.status is Status.BLOCKED
    assert outcome.reason == "model.no_structured_output"
    assert outcome.findings[0].id == "model.no_structured_output" and "'m'" in outcome.findings[0].message
    assert provider.calls == []
    assert outcome.cost.usd == 0


def test_every_shipped_verb_tells_the_invoker_the_shape_it_needs():
    """The refusal is only as live as the callers that ask for it. Read from the source: every
    `invoker.run(` in the AI adapters and the strategies' shared phase carries `schema=`, and the
    one that does not is the measuring call that grades a recorded answer against a case rather
    than demanding a shape of it."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "in_lockstep" / "adapters" / "ai"
    without: list[str] = []
    for path in sorted(root.glob("*.py")):
        for match in re.finditer(r"invoker\.run\((.*?)\n\s*\)", path.read_text(), re.S):
            if "schema=" not in match.group(1):
                without.append(path.name)
    assert without == ["improve.py"], without


# -- the declarations ---------------------------------------------------------------------------------


def test_the_shipped_registrations_declare_what_the_shipped_verbs_need():
    """Every shipped verb asks for a schema, and this repository routes `triage` to `local`, so a
    shipped registration declaring `structured_output=False` would refuse a documented route. The
    default is capable for the same reason, spelled at `ModelCaps`."""
    from in_lockstep.ai.bootstrap import default_registry

    registry = default_registry()
    for name in registry.names():
        caps = registry.registration_for(Model(f"{name}:x")).caps
        assert caps.structured_output and caps.tool_use, f"{name} would refuse a shipped verb"
    assert ModelCaps().structured_output and ModelCaps().tool_use


def test_the_registrations_capabilities_reach_the_invoker_the_factory_builds():
    """The seam: what a registration declares is what the invoker refuses on."""
    from in_lockstep.ai.bootstrap import default_registry, invoker_factory
    from in_lockstep.llm.interface import DataPolicy, ProviderSettings

    registry = default_registry()
    registry.register(
        "tiny",
        lambda settings, creds: _Counting(),
        settings=ProviderSettings(base_url="http://localhost:8080"),
        data_policy=DataPolicy.INTERNAL,
        endpoint="http://localhost:8080",
        caps=ModelCaps(structured_output=False),
    )
    table = CostTable()
    table.add("t", Rate(1.0, 1.0))
    # `provider=` so no credential is resolved for a provider that has none; the caps still come
    # from the registration, which is the seam under test.
    build = invoker_factory(
        "tiny:t", registry=registry, cost_table=table, egress=UnsandboxedEgress(), provider=_Counting()
    )
    ctx = SimpleNamespace(spend=Spend(budget=Budget(usd=1.0)), container=None, recording=None, run_id="")
    invoker = build(ctx)
    assert invoker.caps == ModelCaps(structured_output=False)
    with pytest.raises(InvocationBlocked, match="no_structured_output|structured_output"):
        _run(invoker, schema=SCHEMA)
