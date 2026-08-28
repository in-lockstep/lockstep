"""Shipped prompts.

Each is a thin Python class binding a `.md` body. The class carries what code should carry — a
version that keys the eval store, an output schema, a place for a team to subclass and add
emphasis. The markdown carries the prose, which is what the people who maintain these actually
edit.
"""

from .review import (
    IntentReviewPrompt,
    PerformanceReviewPrompt,
    ReviewPrompt,
    SecurityReviewPrompt,
    TestsReviewPrompt,
    review_layers,
)

__all__ = [
    "IntentReviewPrompt",
    "PerformanceReviewPrompt",
    "ReviewPrompt",
    "SecurityReviewPrompt",
    "TestsReviewPrompt",
    "review_layers",
]
