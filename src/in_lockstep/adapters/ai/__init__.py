"""AI-backed verb adapters. The strategies ARE the adapters: `lockstep.bind(Implement, TDD(...))`."""

from .fix import DiagnoseThenFix, Fix, FixReport, FixSession
from .implement import Implement, ImplementReport, ImplementSession
from .oneshot import Oneshot
from .review import AiReview, Review, ReviewFinding, ReviewReport
from .rfe import AiRfe, Rfe, RfeDraft
from .strategy import AGENCY, AiStrategy, UndeclaredAgency
from .tdd import TDD
from .triage import AiTriage, Triage, TriageDecision

# The request type (`Review`, `Implement`, ...) is the verb's INPUT and its dispatch key, and the
# strategy classes are what a binding names — both halves of `bind(Implement, TDD(...))` are
# exported here so a lockstep.py needs no deep imports.
#
# `AiStrategy` and `AGENCY` are here because `docs/extending.md` tells people to subclass the
# one and declare the other, and until now the only import that worked reached into a
# leading-underscore module. Advertised extension points are public or they are not extension
# points.
__all__ = [
    "AGENCY",
    "AiReview",
    "AiRfe",
    "AiStrategy",
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
    "Rfe",
    "RfeDraft",
    "TDD",
    "Triage",
    "TriageDecision",
    "UndeclaredAgency",
]
