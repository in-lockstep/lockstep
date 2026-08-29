"""AI-backed verb adapters. Thin: they resolve a strategy and delegate to the invoker."""

from .implement import AiImplement, Implement, ImplementReport, ImplementSession, ImplementSpec
from .oneshot import OneshotImplement
from .review import AiReview, Review, ReviewFinding, ReviewReport, ReviewSpec
from .triage import AiTriage, Triage, TriageDecision, TriageSpec

# `ReviewSpec` is the verb's INPUT, and it was the one name missing here while both output types
# were exported — so calling the only AI verb needed a three-level import while reading its result
# did not. An input type is the harder half of a signature to discover, not the easier one.
__all__ = [
    "AiImplement",
    "AiReview",
    "AiTriage",
    "Implement",
    "ImplementReport",
    "ImplementSession",
    "ImplementSpec",
    "OneshotImplement",
    "Review",
    "ReviewFinding",
    "ReviewReport",
    "ReviewSpec",
    "Triage",
    "TriageDecision",
    "TriageSpec",
]
