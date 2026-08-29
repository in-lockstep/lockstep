"""AI-backed verb adapters. Thin: they resolve a strategy and delegate to the invoker."""

from .review import AiReview, Review, ReviewFinding, ReviewReport, ReviewSpec

# `ReviewSpec` is the verb's INPUT, and it was the one name missing here while both output types
# were exported — so calling the only AI verb needed a three-level import while reading its result
# did not. An input type is the harder half of a signature to discover, not the easier one.
__all__ = ["AiReview", "Review", "ReviewFinding", "ReviewReport", "ReviewSpec"]
