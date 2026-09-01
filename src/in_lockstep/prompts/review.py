"""The four review lenses.

An aspect is an agent, not a data row — which is what makes each lens independently testable,
budgetable and evaluable, and is why these are four classes rather than one prompt with a
parameter.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import ClassVar

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
