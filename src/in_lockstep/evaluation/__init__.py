"""Measuring what the AI verbs produce.

The shape is inherited from the compiler-era harness and its central argument is kept: a case
carrying a rubric nobody judged is *not* a pass. A suite reporting 100% while half of it was never
decided is the reassuring number this contract exists to avoid — which is also why
`Outcome.decided` exists rather than a seventh status.

Identity is a content hash of the composed prompt plus the skills and the context recipe, not a
declared version string. A declared version pools runs whose guardrails or turn caps differ, and
their real behavioural difference is then measured as noise, inflating the floor until genuine
regressions read as "within noise".
"""

from .cases import Case, CaseError, Rubric, grade, load_cases, summarize
from .subject import EvalSubject, subject_for

__all__ = [
    "Case",
    "CaseError",
    "EvalSubject",
    "Rubric",
    "grade",
    "load_cases",
    "subject_for",
    "summarize",
]
