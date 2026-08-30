"""AI-backed verb adapters. Thin: they resolve a strategy and delegate to the invoker."""

from .implement import AiImplement, Implement, ImplementReport, ImplementSession
from .oneshot import OneshotImplement
from .review import AiReview, Review, ReviewFinding, ReviewReport
from .triage import AiTriage, Triage, TriageDecision

# The request type (`Review`, `Implement`, ...) is the verb's INPUT and its dispatch key, so it is
# the first name a caller needs — exported beside the report types it produces.
__all__ = [
    "AiImplement",
    "AiReview",
    "AiTriage",
    "Implement",
    "ImplementReport",
    "ImplementSession",
    "OneshotImplement",
    "Review",
    "ReviewFinding",
    "ReviewReport",
    "Triage",
    "TriageDecision",
]
