"""The composed prompt reproduces what the compiler produced.

This is the migration-equivalence half of the characterization corpus: the corpus recorded the
compiler's section identity while it still ran, and this asserts the new composer agrees.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from in_lockstep.ai.prompt import Body, BodyNotFound, Prompt, parse_frontmatter
from in_lockstep.prompts.review import LENSES, review_layers

CORPUS = Path(__file__).resolve().parents[1] / "characterization" / "corpus.json"


@pytest.mark.parametrize("aspect", sorted(LENSES))
def test_gate_test_2_composition_order_matches_the_frozen_corpus(aspect: str) -> None:
    """GATE-TEST-2 — guardrails, body, skills, checked against what the compiler did.

    This is the one test that closes the loop between the frozen corpus and a live composer.
    `tests/characterization/` only checks that the frozen files agree with each other.
    """
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
    """The property this test is named after, which it did not previously check.

    It used to assert that the parser READ `model` and `skills` — the opposite of advisory, and a
    claim nothing else in the package could have honoured, because no caller ever looked at the
    parsed object. A prompt body is data: a non-programmer edits it and a pack may ship one, so a
    body that could choose the model or raise a turn cap would be the configuration surface the
    docstring refuses. What a prompt may say is what it IS, never what should happen to it.
    """
    frontmatter, body = parse_frontmatter(
        "---\n"
        "name: x\n"
        "description: what it is\n"
        "model: claude-opus-5\n"
        "max_tool_turns: 900\n"
        "github: {max-ai-credits: 100000}\n"
        "---\n\nThe prose.\n"
    )
    assert frontmatter.name == "x"
    assert frontmatter.description == "what it is"
    assert body.strip() == "The prose."

    # Nothing that could steer a run is reachable as a field — only `raw`, which nothing reads.
    fields = {f.name for f in dataclasses.fields(frontmatter)}
    assert fields == {"name", "description", "raw"}, f"a configuring field came back: {fields}"
    assert frontmatter.raw["model"] == "claude-opus-5", "kept, unread, so doctor can flag it later"


def test_no_frontmatter_key_reaches_the_model() -> None:
    """The leak this replaced. The header was sent verbatim at the top of every system prompt."""
    prompt = LENSES["security"]()
    system = prompt.system(review_layers())
    for key in ("name:", "description:", "model:", "provider:", "max_tool_turns:", "github:"):
        assert key not in system, f"{key!r} reached the composed prompt"
    assert "security-reviewer" not in system.split("Guardrails are inlined first")[0]


def test_a_prompts_identity_comes_from_its_own_file() -> None:
    """`review/security.md` says `name: security-reviewer`; the projection says
    `body:review/security-reviewer`. One fact, one place — it used to be restated in Python."""
    assert LENSES["security"]().body_label() == "review/security-reviewer"
    assert LENSES["tests"]().body_label() == "review/tests-reviewer"
    assert LENSES["security"]().describe() == "Review a pull request for ways in"


def test_absent_frontmatter_is_not_an_error() -> None:
    frontmatter, body = parse_frontmatter("Just prose.\n")
    assert frontmatter.name == ""
    assert frontmatter.description == ""
    assert body == "Just prose.\n"


def test_composed_system_prompt_inlines_guardrails_first_verbatim() -> None:
    prompt = LENSES["security"]()
    system = prompt.system(review_layers())
    guardrail_at = system.index("Guardrails are inlined first")
    body_at = system.index(prompt.body_text().strip()[:40])
    assert guardrail_at < body_at, "guardrail position is a security property"


# -- repository-injected layers -------------------------------------------------------


def test_plus_appends_after_the_shipped_layers_never_replacing_them() -> None:
    """A house guardrail extends the stack; it cannot quietly drop the baseline underneath.
    Replacing wholesale stays possible — by constructing a fresh PromptLayers, visibly."""
    layered = review_layers().plus(guardrails=(("acme/no-vendoring", "Do not vendor dependencies."),))
    projection = layered.projection("review/security-reviewer")
    assert projection[0] == "guardrail:baseline"
    assert "guardrail:acme/no-vendoring" in projection
    body_at = projection.index("body:review/security-reviewer")
    assert projection.index("guardrail:acme/no-vendoring") < body_at, (
        "a house guardrail is still a guardrail: ahead of the body, where position is a security property"
    )


def test_a_house_guardrail_reaches_the_composed_system_prompt_verbatim() -> None:
    layered = review_layers().plus(guardrails=(("acme/dnx", "Do not use eval() anywhere."),))
    system = LENSES["security"]().system(layered)
    assert "Do not use eval() anywhere." in system
    assert system.index("Do not use eval() anywhere.") < system.index(
        LENSES["security"]().body_text().strip()[:40]
    )


def test_every_ai_adapter_takes_injected_layers_and_defaults_to_the_shipped_set() -> None:
    """`layers=` is the same seam `prompts=`/`lenses=` are: the binding site in lockstep.py is
    where a repository's guardrails enter, visibly. Absent, each adapter composes with the
    shipped set — the seam must not make the default a different prompt."""
    from in_lockstep.adapters.ai.fix import DiagnoseThenFix
    from in_lockstep.adapters.ai.oneshot import Oneshot
    from in_lockstep.adapters.ai.review import AiReview
    from in_lockstep.adapters.ai.triage import AiTriage
    from in_lockstep.prompts.implement import implement_layers

    custom = implement_layers().plus(guardrails=(("acme/house", "Do not xxx."),))

    implement = Oneshot(lambda ctx: None, layers=custom)
    session = implement._session(object())
    assert session.layers is custom, "the injected stack is the one strategies compose with"
    assert Oneshot(lambda ctx: None)._session(object()).layers.projection(
        "b"
    ) == implement_layers().projection("b")

    fix = DiagnoseThenFix(lambda ctx: None, layers=custom)
    assert fix._session(object()).layers is custom

    assert AiReview(lambda ctx: None, layers=custom).layers is custom
    assert AiTriage(lambda ctx: None, layers=custom).layers is custom


def test_a_house_prompt_gets_a_body_label_without_inventing_a_convention() -> None:
    """`body_label` is what a projection calls the body, and a third-party prompt needs one.

    Derived from the body resource, because that is the file a reader can open. Declared only
    where the corpus already knows a different name: the four review lenses are `review/x.md` on
    disk and `review/x-reviewer` in every captured projection, and that identity is what the
    characterization corpus asserts on.
    """
    from in_lockstep.ai.prompt import Body, Prompt
    from in_lockstep.prompts.review import LENSES

    class HousePrompt(Prompt[str, str]):
        body = Body.from_path("prompts/our-review.md")

    class Bodyless(Prompt[str, str]):
        pass

    assert HousePrompt().body_label() == "prompts/our-review"
    assert LENSES["security"]().body_label() == "review/security-reviewer"
    assert Bodyless().body_label() == "Bodyless", "a render-replacing subclass still needs a name"


def test_every_ai_adapter_reports_what_it_composes() -> None:
    """The seam `show-prompt` and `ls` read. Declared per adapter rather than discovered, because
    discovery would mean the CLI knowing that `AiReview` keeps its map in `lenses` and every other
    adapter keeps one in `prompts` — and a renamed attribute would then make an override invisible
    again, silently, which is the defect this closes."""
    from in_lockstep.adapters.ai.backport import AiBackportResolver
    from in_lockstep.adapters.ai.fix import DiagnoseThenFix
    from in_lockstep.adapters.ai.oneshot import Oneshot
    from in_lockstep.adapters.ai.review import AiReview
    from in_lockstep.adapters.ai.rfe import AiRfe
    from in_lockstep.adapters.ai.triage import AiTriage
    from in_lockstep.ai.prompt import Inspectable

    for adapter, expected in (
        (AiReview(), "review/security"),
        (Oneshot(), "implement/oneshot"),
        (DiagnoseThenFix(), "fix/reproducer"),
        (AiTriage(), "triage/analyst"),
        (AiRfe(), "rfe/drafter"),
        (AiBackportResolver(), "backport/conflict-resolver"),
    ):
        assert isinstance(adapter, Inspectable), f"{type(adapter).__name__} is not inspectable"
        composed = adapter.compositions()
        assert expected in composed, f"{type(adapter).__name__}: {sorted(composed)}"
        assert composed[expected].source == type(adapter).__name__
        # Every label is qualified by its verb, so two verbs may hold a prompt of one short name.
        assert all("/" in label for label in composed)
