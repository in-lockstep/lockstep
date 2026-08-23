"""The plan comment is updated in place across many runs, so its rendering must be stable."""

from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "render_plan", Path(__file__).parent.parent / "scripts" / "render-plan.py"
)
assert spec and spec.loader
render_plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render_plan)

PLAN = {
    "summary": "Skip items with no price when totalling an order.",
    "approach": "Filter in `total()` rather than at every call site.",
    "rejected": [{"option": "Default missing prices to zero", "reason": "Hides upstream data bugs."}],
    "changes": [{"path": "src/orders.py", "reason": "Where the total is computed."}],
    "verification": "A test totalling an order containing a priceless item.",
    "risks": ["Callers relying on the current TypeError."],
    "open_questions": ["Should a priceless item be logged?"],
}


def test_the_rendering_is_stable():
    """A comment that churns between identical runs trains reviewers to ignore it."""
    assert render_plan.render(PLAN) == render_plan.render(PLAN)


def test_every_section_a_reviewer_needs_is_present():
    rendered = render_plan.render(PLAN)
    for heading in ("Approach", "Considered and rejected", "Files this changes", "How this is proven"):
        assert heading in rendered


def test_what_could_break_is_shown_because_it_is_what_review_is_for():
    assert "Callers relying on the current TypeError." in render_plan.render(PLAN)


def test_open_questions_survive_into_the_comment():
    assert "Should a priceless item be logged?" in render_plan.render(PLAN)


def test_a_sparse_plan_renders_without_empty_sections():
    rendered = render_plan.render({"summary": "Just this."})
    assert rendered.strip() == "Just this."


def test_files_are_rendered_as_a_table():
    assert "| `src/orders.py` |" in render_plan.render(PLAN)
