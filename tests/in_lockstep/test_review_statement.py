"""A lens says what it made of the change, not only what it found wrong.

GATE-REVIEW-7. On a change it has no findings about, a lens used to post four characters:

    ## in-lockstep review — performance
    **succeeded** · $0.0370 · 3 in / 48 out tokens
    No findings.

That is a reviewer saying nothing, and a reader cannot tell it from a reviewer that did not look.
Both of #442's clean lenses rendered exactly that, at 48 and 111 output tokens -- a real spend
producing a sentence anybody could have written without reading the diff.

The path was already half-built and entirely dead: `REVIEW_SCHEMA` carried an optional `verdict`,
`review.py` parsed it onto `ReviewReport`, the format skill asked for it in prose -- and nothing
rendered it anywhere. Unenforced, the model omitted it, which is why the shipped recording has
none.
"""

from __future__ import annotations

from in_lockstep.adapters.ai.review import MAX_STATEMENT_CHARS, _statement_finding
from in_lockstep.core.outcome import Finding, Outcome, Severity, Status
from in_lockstep.platform.report import review_comment


def _outcome(*findings: Finding, status: Status = Status.SUCCEEDED, decided: bool = True) -> Outcome[None]:
    return Outcome(status=status, decided=decided, findings=findings)


def _statement(text: str = "A small change. The new branch is covered and the control fires.") -> Finding:
    return Finding(id="review.statement", message=text, severity=Severity.NOTE)


# -- the finding the statement rides on ------------------------------------------------


def test_gate_review_7_a_statement_is_a_non_blocking_note() -> None:
    """Never a verdict. `AiReview` emits findings as non-blocking warnings precisely so four
    lenses are defensible on a required check, and a paragraph of prose that could turn a check
    red would undo that."""
    (finding,) = _statement_finding("it looks fine", "tests")

    assert finding.id == "review.statement"
    assert finding.severity is Severity.NOTE
    assert finding.blocking is False


def test_a_lens_that_gave_no_statement_produces_no_finding() -> None:
    """Empty rather than a placeholder: inventing "no statement given" would put prose in the
    record that no model wrote, which is the shape `absent is not zero` refuses everywhere here."""
    assert _statement_finding("", "tests") == ()
    assert _statement_finding("   \n  ", "tests") == ()


def test_a_statement_is_bounded_so_the_lenses_beside_it_stay_readable() -> None:
    """Four sticky comments per pull request. A lens that writes a page makes the other three
    unreadable -- the argument `GATE-VERDICT-2` makes about a finding's message."""
    (finding,) = _statement_finding("x" * (MAX_STATEMENT_CHARS + 500), "tests")

    assert len(finding.message) <= MAX_STATEMENT_CHARS + len("…[truncated]")
    assert finding.message.endswith("…[truncated]")


def test_a_statement_that_fits_is_not_truncated() -> None:
    """The negative control for the cap: it has to be capable of leaving text alone."""
    (finding,) = _statement_finding("short and whole", "tests")

    assert finding.message == "short and whole"


# -- where it lands in the comment -----------------------------------------------------


def test_gate_review_7_the_statement_sits_between_the_cost_line_and_the_findings() -> None:
    """The layout: title, verdict-and-cost, statement, table. A reader meets what the lens
    concluded before the list of what it objected to."""
    body = review_comment(
        "tests",
        _outcome(
            _statement("Two independent readers would land in the same place."),
            Finding(id="x", message="the control asserts a literal", severity=Severity.WARNING, path="t.py"),
        ),
    )
    lines = [line for line in body.splitlines() if line.strip()]

    cost = next(i for i, line in enumerate(lines) if line.startswith("**succeeded**"))
    statement = next(i for i, line in enumerate(lines) if "Two independent readers" in line)
    table = next(i for i, line in enumerate(lines) if line.startswith("| |"))

    assert cost < statement < table


def test_gate_review_7_a_clean_lens_says_what_it_concluded(self_check: None = None) -> None:
    """The case this exists for. "No findings" from a lens that read the diff carefully and one
    that skimmed it are the same two words; the statement is what tells them apart."""
    body = review_comment("performance", _outcome(_statement("Nothing here is on a hot path.")))

    assert "Nothing here is on a hot path." in body
    assert "No findings." in body, "and the table's own sentence still says the table is empty"


def test_the_statement_is_not_rendered_as_a_finding_row() -> None:
    """Partitioned out of the table like the injection signals and the omissions, and for the same
    reason: it is not a finding about the code, and a paragraph in a column headed `finding` reads
    as one."""
    body = review_comment("tests", _outcome(_statement("A prose assessment.")))

    assert "| | location | finding |" not in body, "no table at all when there are no findings"
    assert "A prose assessment." in body


def test_a_refused_review_renders_no_statement() -> None:
    """A control stopped the run before it could read anything. There is no assessment to make,
    and the refusal block is what a reader needs -- the same partition `review_comment` already
    draws for a blocked outcome."""
    body = review_comment(
        "security",
        _outcome(
            Finding(id="cost.budget_exceeded", message="ceiling reached", severity=Severity.ERROR),
            status=Status.BLOCKED,
            decided=False,
        ),
    )

    assert "Refused before it could review" in body
    assert "No findings." not in body


# -- the schema is what enforces it ----------------------------------------------------


def test_gate_review_7_the_schema_requires_a_statement() -> None:
    """Required, so structured output enforces it rather than the prompt asking nicely: a lens
    returning none is a `review.schema_mismatch`, which the adapter already refuses.

    It was optional for as long as it existed, and the shipped recording is the evidence that
    optional means absent -- the format skill asked for it in prose and the model omitted it.
    """
    from in_lockstep.prompts.review import REVIEW_SCHEMA

    assert "statement" in REVIEW_SCHEMA["required"]
    assert "statement" in REVIEW_SCHEMA["properties"]
    assert "verdict" not in REVIEW_SCHEMA["properties"], "renamed: a review has no pass/fail to give"


def test_every_lens_is_told_what_a_statement_is() -> None:
    """The instruction lives in the shared format skill, so an adopter's own lens is held to it by
    the same words as the four shipped ones -- and by the schema either way (O9)."""
    from in_lockstep.prompts.review import review_layers

    skills = dict(review_layers().skills)
    fmt = skills["review/review-format"]

    assert '"statement"' in fmt
    assert "as a whole" in fmt
    assert "not a summary" in " ".join(fmt.split())
