"""A review lens is a declared object, keyed on the name it already has. Discharges `GATE-LENS-1`.

`prompts/review.py` claimed that an aspect is an agent and not a data row, and it was true of the
body alone: the layer stack, the ceilings and the model route were the adapter's, shared by every
lens it ran. `Lens` makes it true of the rest (#204). What these tests hold is the shape that
decision took -- enhance without a subclass and without a new key, a stack and ceilings per lens
that only the lens carrying them sees, a route keyed `review/<aspect>` that wins over `review` --
and the one asymmetry that has a reason on each side, which is that turns tighten and tokens
replace.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from in_lockstep.adapters.ai import AiReview, Review
from in_lockstep.ai.invoker import Invocation, InvokePolicy
from in_lockstep.ai.prompt import PromptLayers
from in_lockstep.core.outcome import Cost, Status
from in_lockstep.core.verbs import Verb
from in_lockstep.prompts.review import (
    LENSES,
    IntentReviewPrompt,
    Lens,
    SecurityReviewPrompt,
    as_lens,
    review_layers,
)

DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"


class _Seen:
    """An invoker that answers a clean review and keeps what it was asked with."""

    def __init__(self) -> None:
        self.systems: list[str] = []
        self.policies: list[InvokePolicy] = []

    async def run(self, **kwargs: Any) -> Invocation:
        self.systems.append(str(kwargs["system"]))
        self.policies.append(kwargs["policy"])
        return Invocation(
            content='{"statement": "small and does what it says", "findings": []}',
            cost=Cost(usd=0.001, input_tokens=10, output_tokens=5),
        )


def _run(adapter: AiReview, aspect: str) -> Any:
    return asyncio.run(adapter.invoke(None, Review(base="a", head="b", aspect=aspect, diff=DIFF)))


# -- the object ---------------------------------------------------------------------------------


def test_gate_lens_1_a_bare_class_and_a_lens_of_it_mean_the_same_thing() -> None:
    """Every document written before `Lens` shows the bare class, and stays right."""
    bare = as_lens(SecurityReviewPrompt)
    assert bare == Lens(prompt=SecurityReviewPrompt)
    assert bare.layers is None and bare.max_turns is None and bare.max_tokens is None and not bare.emphasis
    assert as_lens(bare) is bare


def test_gate_lens_1_a_lens_adds_emphasis_to_the_shipped_body_under_the_shipped_key() -> None:
    """Enhance, not replace: the framework's prose is kept, the finding id is kept, and the
    three sentences ride after the body. No subclass and no new name."""
    seen = _Seen()
    adapter = AiReview(
        lambda ctx: seen,
        lenses={**LENSES, "security": Lens(SecurityReviewPrompt, emphasis="No bare excepts.")},
    )
    outcome = _run(adapter, "security")
    assert outcome.status is Status.SUCCEEDED
    shipped = SecurityReviewPrompt().system(review_layers())
    body_then_skills = shipped.split("<!-- skill:", 1)
    sent = seen.systems[0]
    assert sent.startswith(body_then_skills[0]), "guardrails and the shipped body lead, unchanged"
    assert "## Emphasis for this team\n\nNo bare excepts." in sent
    assert sent.index("No bare excepts.") < sent.index("<!-- skill:"), "emphasis rides after the body"
    assert "<!-- skill:" + body_then_skills[1] in sent, "and the shipped skills follow it, unchanged"
    assert outcome.value is not None and outcome.value.aspect == "security"
    assert adapter.compositions()["review/security"].emphasis == "No bare excepts."
    assert "No bare excepts." in adapter.compositions()["review/security"].text()


def test_a_lens_emphasis_and_a_subclass_emphasis_share_one_heading() -> None:
    """A model reading two "Emphasis for this team" headings would reasonably wonder which team."""

    class House(SecurityReviewPrompt):
        emphasis = "Ours first."

    text = House().system(review_layers(), emphasis="Then the lens's.")
    assert text.count("## Emphasis for this team") == 1
    assert "Ours first.\n\nThen the lens's." in text


def test_an_empty_lens_emphasis_leaves_the_composed_text_byte_identical() -> None:
    """The shipped cassette is keyed on the whole composed prompt; a changed byte is a re-record."""
    prompt = SecurityReviewPrompt()
    assert prompt.system(review_layers()) == prompt.system(review_layers(), emphasis="")


def test_gate_lens_1_a_lens_carries_its_own_stack_and_the_others_keep_the_adapters() -> None:
    house = review_layers().plus(guardrails=(("acme/house", "Never touch migrations."),))
    adapter = AiReview(
        lenses={**LENSES, "security": Lens(SecurityReviewPrompt, layers=house)},
        layers=review_layers().plus(skills=(("acme/how", "Quote the line."),)),
    )
    composed = adapter.compositions()
    assert "guardrail:acme/house" in composed["review/security"].projection()
    assert "skill:acme/how" not in composed["review/security"].projection(), "whole stack, not appended"
    assert "guardrail:acme/house" not in composed["review/intent"].projection()
    assert "skill:acme/how" in composed["review/intent"].projection(), "the others keep the adapter's"


def test_a_lens_stack_that_drops_the_baseline_is_visible_in_the_receipt(tmp_path: Path) -> None:
    """`guardrails_intact` is derived per composition, so a per-lens stack reaches it unchanged."""
    from in_lockstep.lockstep import Lockstep
    from in_lockstep.receipt import receipt_for

    step = Lockstep()
    bare = Lens(SecurityReviewPrompt, layers=PromptLayers(guardrails=(("acme/only", "x"),)))
    step.bind(Review, AiReview(lenses={**LENSES, "security": bare}))
    prompts = {p["label"]: p for p in receipt_for(step, root=tmp_path)["prompts"]}
    assert prompts["review/security"]["guardrails_intact"] is False
    assert prompts["review/intent"]["guardrails_intact"] is True


# -- ceilings -----------------------------------------------------------------------------------


def test_gate_lens_1_a_lens_ceiling_reaches_the_invoker_for_that_lens_only() -> None:
    seen = _Seen()
    adapter = AiReview(
        lambda ctx: seen,
        lenses={**LENSES, "security": Lens(SecurityReviewPrompt, max_tokens=8000)},
        policy=InvokePolicy(max_turns=1, max_tokens=4096),
    )
    _run(adapter, "security")
    _run(adapter, "intent")
    assert [p.max_tokens for p in seen.policies] == [8000, 4096]
    assert seen.policies[1] is adapter.policy, "an unchanged lens is the unchanged path, not a copy"


def test_gate_lens_1_turns_may_only_tighten_and_tokens_replace() -> None:
    """The adapter's cap is already `min(its own, the policy floor)`, and the floor is not
    recoverable from that number -- so a lens asking for more turns cannot be honoured without
    guessing at a ceiling an organisation set. No such floor exists for output tokens."""
    policy = InvokePolicy(max_turns=2, max_tokens=4096)
    assert Lens(SecurityReviewPrompt, max_turns=5).under(policy).max_turns == 2
    assert Lens(SecurityReviewPrompt, max_turns=1).under(policy).max_turns == 1
    assert Lens(SecurityReviewPrompt, max_tokens=16).under(policy).max_tokens == 16
    assert Lens(SecurityReviewPrompt, max_tokens=9000).under(policy).max_tokens == 9000
    assert Lens(SecurityReviewPrompt).under(policy) is policy


def test_a_lens_ceiling_leaves_the_policy_floor_fields_alone() -> None:
    """`deny_tools` and `scan_input` came from the contributed stack; a lens bounds consumption
    and must not touch what the organisation denied."""
    policy = InvokePolicy(max_turns=1, max_tokens=4096, deny_tools=("run_script",), scan_input="block")
    bounded = Lens(SecurityReviewPrompt, max_tokens=8000).under(policy)
    assert bounded.deny_tools == ("run_script",) and bounded.scan_input == "block"


def test_the_truncation_message_names_the_lens_field_to_raise() -> None:
    class _Cut:
        async def run(self, **kwargs: Any) -> Invocation:
            return Invocation(content='{"findings": [{"path": "a.py", "sum', truncated=True)

    adapter = AiReview(
        lambda ctx: _Cut(),
        lenses={"security": Lens(SecurityReviewPrompt, max_tokens=16)},
        policy=InvokePolicy(max_turns=1, max_tokens=4096),
    )
    outcome = _run(adapter, "security")
    assert outcome.reason == "review.truncated"
    assert "16-token output cap" in outcome.findings[0].message, "the lens's number, not the adapter's"
    assert "max_tokens=N" in outcome.findings[0].message


# -- the route ----------------------------------------------------------------------------------


def test_gate_lens_1_a_lens_route_wins_over_the_verbs_and_a_lens_without_one_takes_the_verbs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from in_lockstep.ai import bootstrap

    built: list[str] = []
    monkeypatch.setattr(
        bootstrap, "invoker_factory", lambda model_id, **_: lambda ctx: built.append(model_id)
    )
    ctx = SimpleNamespace(models={"review": "anthropic:a", "review/security": "anthropic:b"})
    bootstrap.routed_invoker(Verb.REVIEW, aspect="security")(ctx)
    bootstrap.routed_invoker(Verb.REVIEW, aspect="intent")(ctx)
    bootstrap.routed_invoker(Verb.REVIEW)(ctx)
    assert built == ["anthropic:b", "anthropic:a", "anthropic:a"]


def test_routed_model_is_the_one_rule_for_which_key_wins() -> None:
    from in_lockstep.ai.bootstrap import routed_model

    routes = {"review": "anthropic:a", "review/security": "anthropic:b"}
    assert routed_model(routes, "review", "security") == "anthropic:b"
    assert routed_model(routes, "review", "intent") == "anthropic:a"
    assert routed_model(routes, "review") == "anthropic:a"
    assert routed_model({}, "review", "security") == ""
    assert routed_model({"review/security": "anthropic:b"}, "review", "intent") == "", (
        "a lens route is not a verb route; a lens with neither is unrouted"
    )


def test_a_missing_route_names_both_keys_a_lens_could_be_routed_by() -> None:
    from in_lockstep.ai.bootstrap import MissingModelRoute, routed_invoker

    with pytest.raises(MissingModelRoute, match=r'"review/security" or "review"'):
        routed_invoker(Verb.REVIEW, aspect="security")(SimpleNamespace(models={}))


def test_the_adapter_asks_for_its_lens_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seam `AiReview._invoker` reaches, driven through `invoke`: the aspect the request
    carries is the aspect the route is resolved for, and an injected factory is used as given."""
    import in_lockstep.adapters.ai.review as review_module

    asked: list[str] = []
    seen = _Seen()

    def _resolve(factory: Any, verb: Any, ctx: Any, *, aspect: str = "") -> Any:
        asked.append(aspect)
        return seen

    monkeypatch.setattr(review_module, "resolve_invoker", _resolve, raising=False)
    # `_invoker` imports the name locally, so patch the module it imports from.
    import in_lockstep.adapters.ai.strategy as strategy_module

    monkeypatch.setattr(strategy_module, "resolve_invoker", _resolve)
    adapter = AiReview(lenses={"security": SecurityReviewPrompt, "intent": IntentReviewPrompt})
    _run(adapter, "intent")
    _run(adapter, "security")
    assert asked == ["intent", "security"]


# -- the key ------------------------------------------------------------------------------------


def test_gate_lens_1_the_key_is_what_the_finding_id_the_marker_and_the_label_are_built_from() -> None:
    """The reason a lens is keyed on the name it already has: everything joins on it."""
    from in_lockstep.platform.report import marker, review_comment

    class _Found:
        async def run(self, **kwargs: Any) -> Invocation:
            return Invocation(
                content=(
                    '{"statement": "small and does what it says", '
                    '"findings": [{"path": "a.py", "line": 1, "summary": "s"}]}'
                ),
                cost=Cost(usd=0.001, input_tokens=10, output_tokens=5),
            )

    adapter = AiReview(
        lambda ctx: _Found(), lenses={"security": Lens(SecurityReviewPrompt, emphasis="Enhanced.")}
    )
    outcome = _run(adapter, "security")
    # The statement is excluded rather than accommodated: this claim is about how a LENS is
    # keyed, and `review.statement` is one finding per review regardless of which lens ran
    # (#451). Folding it into the expected list would make this drift every time the review
    # verb grows another note.
    keyed = [f.id for f in outcome.findings if f.id != "review.statement"]
    assert keyed == ["review.security"], "enhanced, not re-keyed"
    assert marker("review:security") in review_comment("security", outcome)
    assert list(adapter.compositions()) == ["review/security"]


def test_the_map_an_adapter_holds_is_what_the_module_bound_in_either_spelling() -> None:
    """`adapter.lenses["security"] is OurSecurity` stayed true for the bare class; a `Lens` is held
    as given too. Normalisation happens where a lens is read, not where it is stored."""
    declared = Lens(SecurityReviewPrompt, emphasis="x")
    adapter = AiReview(lenses={"security": declared, "intent": IntentReviewPrompt})
    assert adapter.lenses["security"] is declared
    assert adapter.lenses["intent"] is IntentReviewPrompt
