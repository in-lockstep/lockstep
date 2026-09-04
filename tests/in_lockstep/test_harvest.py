"""Turning recorded runs into cases that actually settle.

The corpus that shipped was twenty-seven hand-written cases, every one of them carrying a rubric,
none of them ever decided — `eval report` graded them against `None` and reported "outstanding" for
all twenty-seven, forever. That is an honest number and a useless one, and it is what a corpus looks
like when nothing can run it.

What was missing was not a grader. It was the recordings: a cassette kept a hash of its request,
which is enough to look a recording up and not enough to build anything from. So a repository could
accumulate real runs for a year and have nothing it could measure against at the end of it.

These cover the whole path — the recorder keeping the request, the harvester turning it into a case,
and the case settling against the answer that was really returned.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from in_lockstep.ai.replay import Cassette, RecordingProvider, key_of, request_from
from in_lockstep.evaluation.cases import Case, grade
from in_lockstep.evaluation.harvest import MIN_NEEDLE_CHARS, NothingToHarvest, harvest
from in_lockstep.llm.types import LLMInput, LLMOutput, Message, TokenUsage
from in_lockstep.privileged.redact import Redact, SecretRegistry


class _Model:
    """A provider that says one thing, so a test can be about the recording rather than the reply."""

    def __init__(self, content: str) -> None:
        self.content = content

    def name(self) -> str:
        return "stub"

    def base_url(self) -> str:
        return ""

    async def generate(self, request: LLMInput) -> LLMOutput:
        return LLMOutput(content=self.content, usage=TokenUsage(9, 3), stop_reason="end_turn")


ANSWER = json.dumps(
    {
        "findings": [
            {"path": "actions/save/action.yml", "line": 29, "summary": "Unquoted variable in find"},
            {"path": "app/models.py", "line": 12, "summary": "Session outlives the request"},
        ]
    }
)


def _record(tmp_path: Path, *, content: str = ANSWER, redact: Redact | None = None) -> Path:
    tape = Cassette(path=tmp_path / "c.json")
    provider = RecordingProvider(_Model(content), tape, redact or Redact(SecretRegistry()))
    asyncio.run(
        provider.generate(
            LLMInput(
                model="claude-sonnet-4-6",
                system="You review one pull request for security.",
                messages=[Message(role="user", content="Review this diff for security problems.")],
                max_tokens=4096,
            )
        )
    )
    return tape.path


# -- the recording ---------------------------------------------------------------------------


def test_gate_eval_3_a_stored_request_hashes_back_to_the_key_it_is_filed_under(tmp_path: Path) -> None:
    """GATE-EVAL-3. The property everything else here rests on.

    A stored request that hashed differently from the one recorded would be a recording that cannot
    find itself: harvest would build a case, `eval run` would replay it, the lookup would miss, and
    the case would report as unplayable for a reason nobody could act on. Cheap to assert, and the
    failure it prevents is one that would look like a data problem rather than a code one.
    """
    data = json.loads(_record(tmp_path).read_text())
    ((key, entry),) = data["provider_calls"].items()
    assert key_of(request_from(entry["request"])) == key


def test_a_secret_never_reaches_the_recorded_request(tmp_path: Path) -> None:
    """The request is the likelier of the two halves to carry one — an answer is a model's prose,
    a request is whatever the repository put in front of it."""
    registry = SecretRegistry()
    registry.add("sk-secret-value-here")
    tape = Cassette(path=tmp_path / "c.json")
    provider = RecordingProvider(_Model("fine"), tape, Redact(registry))
    asyncio.run(
        provider.generate(
            LLMInput(
                model="m",
                system="the key is sk-secret-value-here",
                messages=[Message(role="user", content="also sk-secret-value-here")],
            )
        )
    )
    assert "sk-secret-value-here" not in tape.path.read_text()


# -- harvesting ------------------------------------------------------------------------------


def test_a_harvested_case_settles_against_the_answer_it_came_from(tmp_path: Path) -> None:
    """Circular on the first run, and said so in the module docstring rather than hidden.

    What it buys is that the case *settles* — `deterministic_passed` is True or False rather than
    None forever — which is the difference between a corpus and a list.
    """
    (harvested,) = harvest(_record(tmp_path))
    case = Case.parse(harvested.case, name=harvested.name)
    result = grade(case, json.loads(ANSWER))
    assert result["deterministic_passed"] is True
    assert result["rubric_outstanding"] is False


def test_a_harvested_case_fails_when_the_answer_moves(tmp_path: Path) -> None:
    """The point of having one. A finding lost between the model and the outcome is a regression
    in parsing, in schema repair, or in the adapter — and it used to be invisible."""
    (harvested,) = harvest(_record(tmp_path))
    case = Case.parse(harvested.case, name=harvested.name)

    fewer = json.loads(ANSWER)
    fewer["findings"] = fewer["findings"][:1]
    result = grade(case, fewer)

    assert result["deterministic_passed"] is False
    failed = {c["check"] for c in result["checks"] if not c["passed"]}
    assert "count.findings" in failed


def test_harvest_never_invents_a_rubric(tmp_path: Path) -> None:
    """A rubric is a judgement somebody has to make. Writing one here would put a question nobody
    asked into a corpus, where it would sit outstanding forever — which is the failure the shipped
    corpus already demonstrates twenty-seven times over."""
    (harvested,) = harvest(_record(tmp_path))
    assert "rubric" not in harvested.case["expect"]
    assert Case.parse(harvested.case, name=harvested.name).rubric is None


def test_provenance_is_kept_and_is_not_graded(tmp_path: Path) -> None:
    """Kept, because "where did this come from and is it still the kind of work we do" is what
    decides whether a case is worth keeping. Out of `input`, because a grader that could see it
    might come to depend on it."""
    (harvested,) = harvest(_record(tmp_path))
    case = Case.parse(harvested.case, name=harvested.name)
    assert case.harvested["key"] and case.harvested["model"] == "claude-sonnet-4-6"
    assert "harvested" not in case.input
    assert "harvested" not in case.expect


def test_short_strings_are_not_used_as_expectations(tmp_path: Path) -> None:
    """A `contains` on "bug" is a check that cannot fail, and a check that cannot fail is worse
    than no check because it counts toward a total."""
    answer = json.dumps({"kind": "bug", "note": "ok", "detail": "x" * (MIN_NEEDLE_CHARS + 5)})
    (harvested,) = harvest(_record(tmp_path, content=answer))
    for needle in harvested.case["expect"].get("contains", []):
        assert len(needle) >= MIN_NEEDLE_CHARS


def test_the_case_is_named_after_what_was_asked(tmp_path: Path) -> None:
    """A hash is a name nobody can recognise a year later."""
    (harvested,) = harvest(_record(tmp_path))
    assert harvested.name == "review-this-diff-for-security-problems"
    assert harvest(_record(tmp_path), family="review")[0].name.startswith("review/")


# -- refusing rather than guessing ------------------------------------------------------------


def test_a_recording_without_its_request_is_refused_by_name(tmp_path: Path) -> None:
    """Cassettes recorded before requests were stored. Their answer is real, and a case wrapped
    around an answer whose question is unknown cannot be re-run — which is the one thing a case is
    for. The refusal names the remedy rather than silently harvesting nothing."""
    path = _record(tmp_path)
    data = json.loads(path.read_text())
    for entry in data["provider_calls"].values():
        entry.pop("request")
    path.write_text(json.dumps(data))

    with pytest.raises(NothingToHarvest, match="recorded before requests were stored"):
        harvest(path)


def test_an_empty_or_unreadable_cassette_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"provider_calls": {}}))
    with pytest.raises(NothingToHarvest, match="no recorded calls"):
        harvest(empty)

    with pytest.raises(NothingToHarvest, match="could not read"):
        harvest(tmp_path / "nope.json")


def test_an_answer_with_nothing_checkable_produces_no_case(tmp_path: Path) -> None:
    """A one-word reply is a recording, not a measurement. Emitting a case for it would mean a
    case with no expectation, which `Case.parse` refuses anyway — better to not write the file."""
    with pytest.raises(NothingToHarvest):
        harvest(_record(tmp_path, content="ok"))


def test_every_harvested_case_survives_the_parser_that_guards_the_corpus(tmp_path: Path) -> None:
    """Harvest writes files that `load_cases` reads. A shape only one of them agrees with is two
    formats, and the second one is discovered by a corpus that will not load."""
    for harvested in harvest(_record(tmp_path)):
        parsed = Case.parse(harvested.case, name=harvested.name)
        assert parsed.expect and parsed.input["request"]["model"]


# -- a harvested case can pass, and can reward a better answer ------------------------------


def test_a_harvested_case_passes_against_the_answer_it_came_from() -> None:
    """The claim `eval harvest` prints: "Expectations were derived from the answers that were
    recorded, so these pass against those answers today."

    It was false for any answer containing a quote. `_needles` lifts a raw substring out of the
    answer, and `grade`'s `contains` searches `json.dumps(output)` — where that quote is escaped.
    So the needle never matched, and the case failed against the exact answer it was derived from.
    """
    from in_lockstep.evaluation.cases import Case, grade
    from in_lockstep.evaluation.harvest import _expect

    answer = {"findings": [{"summary": 'Unquoted "path" allows word-splitting', "severity": "high"}]}
    case = Case(name="c", expect=_expect(answer))
    assert grade(case, answer)["deterministic_passed"] is True, grade(case, answer)["checks"]


def test_a_harvested_case_passes_against_an_answer_carrying_a_newline_or_a_backslash() -> None:
    """The same bug, in the other two characters `json.dumps` escapes."""
    from in_lockstep.evaluation.cases import Case, grade
    from in_lockstep.evaluation.harvest import _expect

    answer = {"findings": [{"summary": "reads C:\\Users\\x", "detail": "line one\nline two"}]}
    case = Case(name="c", expect=_expect(answer))
    assert grade(case, answer)["deterministic_passed"] is True, grade(case, answer)["checks"]


def test_a_better_answer_that_finds_one_more_thing_is_not_a_regression() -> None:
    """The property #163 needs and did not have. A harvested case demanded the OLD answer's exact
    finding count, so a prompt improved to catch one more real vulnerability failed the corpus that
    was supposed to be measuring the improvement. The metric was an inverse of its purpose.

    Same shape as #194/#195, which fixed it in the hand-written corpus; the lesson never reached
    the harvester that writes the cases."""
    from in_lockstep.evaluation.cases import Case, grade
    from in_lockstep.evaluation.harvest import _expect

    before = {"findings": [{"summary": "session scope"}]}
    after = {"findings": [{"summary": "session scope"}, {"summary": "and an unquoted path"}]}
    case = Case(name="c", expect=_expect(before))
    assert grade(case, after)["deterministic_passed"] is True, grade(case, after)["checks"]


def test_an_answer_that_found_less_than_before_is_still_a_regression() -> None:
    """The negative control. A floor that anything clears is not a floor, and the deterministic half
    has to keep failing the case where a prompt change lost something."""
    from in_lockstep.evaluation.cases import Case, grade
    from in_lockstep.evaluation.harvest import _expect

    before = {"findings": [{"summary": "session scope"}, {"summary": "unquoted path"}]}
    case = Case(name="c", expect=_expect(before))
    assert grade(case, {"findings": [{"summary": "session scope"}]})["deterministic_passed"] is False


def test_an_answer_that_found_nothing_still_means_nothing() -> None:
    """Zero stays exact. `nothing-to-find` exists to catch a reviewer inventing a concern, and a
    floor of zero would make it a case that cannot fail — which is the defect `Case.parse` refuses
    by name at the other end."""
    from in_lockstep.evaluation.cases import Case, grade
    from in_lockstep.evaluation.harvest import _expect

    case = Case(name="c", expect=_expect({"findings": []}))
    assert grade(case, {"findings": []})["deterministic_passed"] is True
    assert grade(case, {"findings": [{"summary": "invented"}]})["deterministic_passed"] is False


# -- a case outlives the recording it came from ------------------------------------------------


def test_a_harvested_case_carries_the_answer_it_was_derived_from(tmp_path: Path) -> None:
    """A case that only points at a cassette is worth what that cassette is still there.

    Which, for the case that matters most, is nothing: a CI recording is written to the runner's
    temporary directory and destroyed with the runner, so the case arrives in an artifact beside a
    path that no longer exists on any machine.
    """
    (case,) = harvest(_record(tmp_path), family="review")
    recorded = case.case["recorded"]
    assert json.loads(recorded["content"])["findings"], "the answer did not travel with the case"
    assert recorded["stop_reason"] == "end_turn"
    assert recorded["usage"]["input_tokens"] == 9


def test_gate_eval_4_a_case_settles_after_its_cassette_is_gone(tmp_path: Path) -> None:
    """GATE-EVAL-4, first half. The property the `recorded` field exists for, asserted by
    deleting the tape rather than by trusting that nothing reopens it."""
    from click.testing import CliRunner

    from in_lockstep.cli import main

    tape = _record(tmp_path)
    corpus = tmp_path / "corpus"
    CliRunner().invoke(main, ["eval", "harvest", "--from", str(tape), "--into", str(corpus)])
    tape.unlink()

    result = CliRunner().invoke(main, ["eval", "run", "--corpus", str(corpus)])
    assert "1 replayed, 0 skipped" in result.output, result.output
    assert "SKIP" not in result.output, result.output


def test_a_case_whose_request_and_answer_were_never_a_pair_is_refused(tmp_path: Path) -> None:
    """GATE-EVAL-4, second half. Self-contained means editable, so the pair is checked.

    `harvested.key` is the hash of the request as recorded. If the request no longer hashes to it,
    the answer beside it belongs to a different question, and grading would settle one question
    against the answer to another -- the fabrication this whole corpus exists to refuse.
    """
    from click.testing import CliRunner

    from in_lockstep.cli import main

    corpus = tmp_path / "corpus"
    CliRunner().invoke(main, ["eval", "harvest", "--from", str(_record(tmp_path)), "--into", str(corpus)])
    (path,) = sorted(corpus.rglob("*.json"))
    raw = json.loads(path.read_text())
    raw["input"]["request"]["system"] = "A different question entirely."
    path.write_text(json.dumps(raw))

    result = CliRunner().invoke(main, ["eval", "run", "--corpus", str(corpus)])
    assert "not the pair recorded" in result.output, result.output
    assert "0 replayed, 1 skipped" in result.output, result.output


def test_a_case_with_no_answer_still_falls_back_to_its_cassette(tmp_path: Path) -> None:
    """Every case harvested before this field existed, and every one somebody wrote by hand."""
    from click.testing import CliRunner

    from in_lockstep.cli import main

    tape = _record(tmp_path)
    corpus = tmp_path / "corpus"
    CliRunner().invoke(main, ["eval", "harvest", "--from", str(tape), "--into", str(corpus)])
    (path,) = sorted(corpus.rglob("*.json"))
    raw = json.loads(path.read_text())
    del raw["recorded"]
    path.write_text(json.dumps(raw))

    result = CliRunner().invoke(main, ["eval", "run", "--corpus", str(corpus)])
    assert "1 replayed, 0 skipped" in result.output, result.output
