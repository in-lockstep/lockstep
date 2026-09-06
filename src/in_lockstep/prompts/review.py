"""The four review lenses, and what a lens is.

An aspect is an agent, not a data row — which is what makes each lens independently testable,
budgetable and evaluable, and is why these are four classes rather than one prompt with a
parameter. For a long time that sentence was true of the body and nothing else: the layer stack,
the ceilings and the model route were all the adapter's, shared by every lens it ran. `Lens` is
what makes it true of the rest (#204): a declared object carrying the prompt, an emphasis, its own
stack and its own ceilings, keyed on the name the lens already has.

Keyed on the name it already has, and that is the design rather than a convenience. `review/
<aspect>` is the finding id, the sticky-comment marker, the `Improvable` label and the census key
all at once, and a lens is what it SAYS -- a verb is what it MAY DO. Making each lens a verb would
have re-keyed twenty-six ledger records and orphaned every one of those joins to buy one span name,
which is why #204's design panel refused it. A lens differs in prose and in bounds; the moment a
review must execute the suite, hold write tools or take an artifact instead of a diff, it has
crossed a capability line and genuinely is a verb, and `docs/extending.md` draws that line.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from importlib import resources
from typing import ClassVar

from ..ai.invoker import InvokePolicy
from ..ai.prompt import Body, Prompt, PromptLayers, parse_frontmatter

REVIEW_SCHEMA = {
    "type": "object",
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "summary"],
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                    "summary": {"type": "string"},
                    "detail": {"type": "string"},
                    "severity": {"type": "string"},
                },
            },
        },
        "verdict": {"type": "string"},
    },
}


@dataclass(frozen=True)
class ReviewParams:
    base: str
    head: str
    aspect: str = ""


def _text(resource: str) -> str:
    """Read a shipped fragment, stripped of its frontmatter."""
    raw = (resources.files("in_lockstep.prompts") / resource).read_text()
    _, body = parse_frontmatter(raw)
    return body


def review_layers() -> PromptLayers:
    """Guardrails before the body, skills after — the framework invariant.

    The baseline reaches every agent and cannot be excluded: it holds what is true of every
    invocation in every pipeline, and a switch to turn it off would make the floor a suggestion.
    """
    return PromptLayers(
        guardrails=(
            ("baseline", _text("guardrail-baseline.md")),
            ("review/reviewing", _text("review/guardrail-reviewing.md")),
        ),
        skills=(
            ("review/review-format", _text("skills/review-format.md")),
            ("review/review-revision", _text("skills/review-revision.md")),
        ),
    )


class ReviewPrompt(Prompt[ReviewParams, "dict[str, object]"]):
    """Base for the shipped lenses. Subclass and set `emphasis` for a house variant."""

    version: ClassVar[str] = "1"
    output: ClassVar[type | None] = dict
    aspect: ClassVar[str] = "review"

    def user_text(self, params: ReviewParams) -> str:
        return f"Review the change between {params.base} and {params.head} through the {self.aspect} lens."


class SecurityReviewPrompt(ReviewPrompt):
    aspect: ClassVar[str] = "security"
    body: ClassVar[Body | None] = Body.from_file("review/security.md", package="in_lockstep.prompts")


class IntentReviewPrompt(ReviewPrompt):
    aspect: ClassVar[str] = "intent"
    body: ClassVar[Body | None] = Body.from_file("review/intent.md", package="in_lockstep.prompts")


class PerformanceReviewPrompt(ReviewPrompt):
    aspect: ClassVar[str] = "performance"
    body: ClassVar[Body | None] = Body.from_file("review/performance.md", package="in_lockstep.prompts")


class TestsReviewPrompt(ReviewPrompt):
    aspect: ClassVar[str] = "tests"
    body: ClassVar[Body | None] = Body.from_file("review/tests.md", package="in_lockstep.prompts")


LENSES: dict[str, type[ReviewPrompt]] = {
    "security": SecurityReviewPrompt,
    "intent": IntentReviewPrompt,
    "performance": PerformanceReviewPrompt,
    "tests": TestsReviewPrompt,
}


@dataclass(frozen=True)
class Lens:
    """One review lens, declared: a prompt, and everything about running it that used to be the
    adapter's alone.

    Frozen, because it is bound at import time from a module loaded off a trusted ref and read at
    invoke time; a lens that could be mutated between the two would be a configuration surface
    nothing inspects. Every field but `prompt` defaults to "the adapter's", so `Lens(SomePrompt)`
    means exactly what the bare class meant before this type existed, and `AiReview(lenses=...)`
    takes either spelling.

    `emphasis` is the no-subclass form of `Prompt.emphasis`: one line beside the bind, keeping the
    shipped body and the shipped KEY. A subclass still works and is still the right tool for a
    replaced body or a bumped version; this is for the team that wants to add three sentences to
    `security` without a class, and without forking `review.security` into a name nobody's ledger
    has heard of.

    `layers` is this lens's whole stack when set, and the adapter's when not. Whole, not appended
    to: a lens that wants the adapter's stack plus one guardrail spells it
    `review_layers().plus(...)`, which is the greppable spelling of keeping the baseline, and the
    receipt's `guardrails_intact` reports a lens whose stack drops it.

    `max_turns` may only tighten and `max_tokens` replaces, and the asymmetry has a reason on each
    side. The adapter's turn cap is already `min(its own, the policy floor)` by the time a lens
    sees it, and the floor is not recoverable from the result -- so a lens asking for more turns
    than the adapter has cannot be honoured without guessing at a ceiling an organisation set, and
    is clipped instead. No such floor exists for output tokens (`Policy` deliberately carries none;
    the cap is the workshop's), so a lens's number is exact, and it is the number
    `review.truncated` tells a person to raise "for this lens".
    """

    prompt: type[ReviewPrompt]
    emphasis: str = ""
    layers: PromptLayers | None = None
    max_turns: int | None = None
    max_tokens: int | None = None

    def stack(self, default: PromptLayers) -> PromptLayers:
        """This lens's layers, or the adapter's."""
        return self.layers if self.layers is not None else default

    def under(self, policy: InvokePolicy) -> InvokePolicy:
        """The adapter's policy, bounded for this lens. The same object back when nothing differs,
        so an unchanged lens is the unchanged path and not a copy of it."""
        turns = policy.max_turns if self.max_turns is None else min(self.max_turns, policy.max_turns)
        tokens = policy.max_tokens if self.max_tokens is None else self.max_tokens
        if turns == policy.max_turns and tokens == policy.max_tokens:
            return policy
        return replace(policy, max_turns=turns, max_tokens=tokens)


def as_lens(declared: type[ReviewPrompt] | Lens) -> Lens:
    """Either spelling of a lens, as the declared object.

    The bare class stays legal because it is what every document, cookbook recipe and pack README
    written before `Lens` shows, and the map an adapter holds keeps whichever spelling it was
    given -- `adapter.lenses["security"] is OurSecurity` stays true. Normalised at the two read
    sites instead, which is cheaper than a migration and does not ask anybody to rewrite a bind.
    """
    return declared if isinstance(declared, Lens) else Lens(prompt=declared)
