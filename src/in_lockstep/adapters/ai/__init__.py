"""AI-backed verb adapters. The strategies ARE the adapters: `lockstep.bind(Implement, TDD(...))`."""

from .fix import DiagnoseThenFix, Fix, FixReport, FixSession
from .implement import Implement, ImplementReport, ImplementSession
from .oneshot import Oneshot
from .review import AiReview, Review, ReviewFinding, ReviewReport
from .tdd import TDD
from .triage import AiTriage, Triage, TriageDecision

# The request type (`Review`, `Implement`, ...) is the verb's INPUT and its dispatch key, and the
# strategy classes are what a binding names — both halves of `bind(Implement, TDD(...))` are
# exported here so a lockstep.py needs no deep imports.
__all__ = [
    "AiReview",
    "AiTriage",
    "DiagnoseThenFix",
    "Fix",
    "FixReport",
    "FixSession",
    "Implement",
    "ImplementReport",
    "ImplementSession",
    "Oneshot",
    "Review",
    "ReviewFinding",
    "ReviewReport",
    "TDD",
    "Triage",
    "TriageDecision",
]
