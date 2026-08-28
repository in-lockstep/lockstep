"""The composed prompt reproduces what the compiler produced.

This is the migration-equivalence half of the characterization corpus: the corpus recorded the
compiler's section identity while it still ran, and this asserts the new composer agrees.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from in_lockstep.ai.prompt import Body, BodyNotFound, Prompt, parse_frontmatter
from in_lockstep.prompts.review import LENSES, review_layers

CORPUS = Path(__file__).resolve().parents[1] / "characterization" / "corpus.json"


@pytest.mark.parametrize("aspect", sorted(LENSES))
def test_composition_order_matches_the_frozen_corpus(aspect: str) -> None:
    """Guardrails, body, skills — the invariant, checked against what the compiler did."""
    projection = review_layers().projection(f"review/{aspect}-reviewer")

    recorded = json.loads(CORPUS.read_text())[f"repo/review/{aspect}-reviewer"]["projection"]
    # The corpus was captured for THIS repository, which contributes one extra context layer of
    # its own. Everything the framework ships must match exactly.
    assert projection == [s for s in recorded if not s.startswith("context:")]


@pytest.mark.parametrize("aspect", sorted(LENSES))
def test_baseline_guardrail_leads_and_body_sits_after_guardrails(aspect: str) -> None:
    projection = review_layers().projection(f"review/{aspect}-reviewer")
    assert projection[0] == "guardrail:baseline"
    body_at = projection.index(f"body:review/{aspect}-reviewer")
    assert all(s.startswith("guardrail:") for s in projection[:body_at])
    assert all(s.startswith("skill:") for s in projection[body_at + 1 :])


def test_bodies_are_files_not_python_literals() -> None:
    """The design amendment: prose stays reviewable by the people who write it."""
    for lens in LENSES.values():
        assert isinstance(lens.body, Body)
        text = lens().body_text()
        assert len(text) > 200, "a real prompt body, read from disk"


def test_body_resolution_is_lazy_so_import_performs_no_io() -> None:
    class Missing(Prompt):
        body = Body.from_file("does/not/exist.md", package="in_lockstep.prompts")

    # Constructing is fine; only rendering touches the filesystem.
    prompt = Missing()
    with pytest.raises(BodyNotFound, match="not found"):
        prompt.body_text()


def test_frontmatter_is_advisory_not_a_configuration_surface() -> None:
    frontmatter, body = parse_frontmatter(
        "---\nname: x\nmodel: claude-sonnet-4-6\nskills: [a, b]\n---\n\nThe prose.\n"
    )
    assert frontmatter.model == "claude-sonnet-4-6"
    assert frontmatter.skills == ("a", "b")
    assert body.strip() == "The prose."


def test_absent_frontmatter_is_not_an_error() -> None:
    frontmatter, body = parse_frontmatter("Just prose.\n")
    assert frontmatter.model == ""
    assert body == "Just prose.\n"


def test_composed_system_prompt_inlines_guardrails_first_verbatim() -> None:
    prompt = LENSES["security"]()
    system = prompt.system(review_layers())
    guardrail_at = system.index("Guardrails are inlined first")
    body_at = system.index(prompt.body_text().strip()[:40])
    assert guardrail_at < body_at, "guardrail position is a security property"
