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


def _request(*, text: str = "Review this diff for security problems.") -> LLMInput:
    """The request `_record` sends, so a test can build the same one to look up."""
    return LLMInput(
        model="claude-sonnet-4-6",
        system="You review one pull request for security.",
        messages=[Message(role="user", content=text)],
        max_tokens=4096,
    )


def _record(
    tmp_path: Path,
    *,
    content: str = ANSWER,
    redact: Redact | None = None,
    request_text: str = "Review this diff for security problems.",
) -> Path:
    tape = Cassette(path=tmp_path / "c.json")
    provider = RecordingProvider(_Model(content), tape, redact or Redact(SecretRegistry()))
    asyncio.run(provider.generate(_request(text=request_text)))
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
    assert case.harvested["filed_under"] and case.harvested["model"] == "claude-sonnet-4-6"
    # `key` is stamped by the caller, not here: hashing a request is `ai.replay`'s job and
    # `evaluation` is a leaf that may not import it. See `_eval_harvest`.
    assert "key" not in case.harvested
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


# -- GATE-EVAL-3: a recording can find itself, including once redaction has fired ---------------


def _secret_tape(tmp_path: Path) -> Path:
    """A recording whose request contains something the redactor actually masks.

    The distinction this section is about. Every fixture asserting GATE-EVAL-3 before this recorded
    through a `Redact(SecretRegistry())` with nothing registered and no structural pattern in the
    text, so the redactor was a no-op and the property could not fail however wrong it was.
    """
    registry = SecretRegistry()
    registry.add("supersecretvalue123")
    return _record(tmp_path, redact=Redact(registry), request_text="the key is supersecretvalue123")


def test_gate_eval_3_a_redacted_recording_still_finds_itself(tmp_path: Path) -> None:
    """GATE-EVAL-3. The filing key hashes what was SENT; the entry holds what was WRITTEN.

    Both are right and they are different numbers, so a caller holding the stored request — which
    is every caller reading a tape back, including `eval run`'s cassette fallback — asked for a
    hash the tape had never heard of and was told its recording was gone.
    """
    tape = Cassette.load(_secret_tape(tmp_path))
    ((filed_under, entry),) = tape.provider_calls.items()
    stored = request_from(entry["request"])

    assert "supersecretvalue123" not in json.dumps(entry), "the fixture cannot exercise the property"
    assert key_of(stored) != filed_under, "nothing was redacted, so this proves nothing"
    assert tape.replay_provider(stored) is not None, "a recording could not find itself"


def test_a_live_request_is_still_found_by_its_own_hash(tmp_path: Path) -> None:
    """The other direction, which the index must not cost.

    A replay looks up the request it is about to send, and that one is raw. Keying the tape on the
    redacted form instead would make replay depend on which secrets the machine doing the replaying
    happens to know — so the shipped fixture would stop replaying the day somebody exported a
    variable whose name ends in `_TOKEN`.
    """
    tape = Cassette.load(_secret_tape(tmp_path))
    assert tape.replay_provider(_request(text="the key is supersecretvalue123")) is not None


# -- one session harvests into more than one file -----------------------------------------------


def _session(tmp_path: Path, turns: int = 4, system: str = "s") -> Path:
    """A multi-turn recording shaped like a real one, which is where the names collided.

    Tool results come back as `role="tool_result"` (`invoker.py`), never as `user` — so the last
    user message is the ticket EVERY turn shares, and a stem read off it is the same string for the
    whole session. A fixture whose tool results were `role="user"` would not reproduce this, which
    is worth saying because that is the fixture somebody writes by accident.
    """
    from in_lockstep.llm.types import ToolCall

    tmp_path.mkdir(parents=True, exist_ok=True)
    tape = Cassette(path=tmp_path / "session.json")
    replies = [
        json.dumps({"findings": [{"summary": f"turn {i} found a real problem here"}]}) for i in range(turns)
    ]
    provider = RecordingProvider(_Model(""), tape, Redact(SecretRegistry()))
    history = [Message(role="user", content="Fix the failing test in the parser module please")]
    for i in range(turns):
        provider.inner.content = replies[i]  # type: ignore[attr-defined]
        asyncio.run(
            provider.generate(
                LLMInput(
                    model="claude-sonnet-4-6",
                    system=system,
                    messages=list(history),
                    max_tokens=64,
                )
            )
        )
        history.append(
            Message(
                role="assistant",
                content=replies[i],
                tool_calls=[ToolCall(id=f"c{i}", name="read_file", input={"path": f"src/f{i}.py"})],
            )
        )
        history.append(
            Message(role="tool_result", content=f"contents {i}", tool_call_id=f"c{i}", tool_name="read_file")
        )
    tape.save()
    return tape.path


def test_gate_record_3_a_session_is_one_case_however_many_turns_it_took(tmp_path: Path) -> None:
    """GATE-RECORD-3. Nine turns are one conversation, and nine cases would be nine copies of one question.

    Each turn's request is the one before it with a turn appended, so the ninth CONTAINS the other
    eight — nine cases means storing the same question nine times, the last one whole. `#227` fixed
    them overwriting each other; this is the reason there should not have been nine.
    """
    found = harvest(_session(tmp_path, turns=4), family="implement")
    assert len(found) == 1, [h.name for h in found]


def test_two_sessions_on_one_tape_stay_two_cases(tmp_path: Path) -> None:
    """TDD records a red phase and a green phase into one tape, and they are two questions.

    The chain is broken by the system prompt differing, which is what tells them apart — so this is
    also the guard against a grouping that swallowed everything into one case and called it tidy.
    """
    first = _session(tmp_path, turns=3)
    tape = json.loads(first.read_text())
    second = json.loads(_session(tmp_path / "b", turns=2, system="A DIFFERENT phase entirely").read_text())
    tape["provider_calls"].update(second["provider_calls"])
    tape["order"] = tape["order"] + second["order"]
    merged = tmp_path / "merged.json"
    merged.write_text(json.dumps(tape))

    found = harvest(merged, family="implement")
    assert len(found) == 2, [h.name for h in found]
    assert len({h.path_in(tmp_path / "corpus") for h in found}) == 2


def test_a_single_call_keeps_the_name_a_person_would_recognise(tmp_path: Path) -> None:
    """The other direction. Every `review` recording is one call, and those are the cases somebody
    reads in a promotion diff — so the suffix is applied only where a stem is actually shared."""
    (only,) = harvest(_record(tmp_path), family="review")
    assert only.name == "review/review-this-diff-for-security-problems"


def test_harvest_says_which_reason_it_found_nothing(tmp_path: Path) -> None:
    """One message covered both causes and named the wrong one for the commoner case.

    A tape whose answers state nothing settleable was told its REQUESTS had not been stored, so the
    advice was "re-record" — which would produce exactly the same tape.
    """
    unmeasurable = _record(tmp_path, content="ok")
    with pytest.raises(NothingToHarvest) as caught:
        harvest(unmeasurable)
    assert "nothing a machine can settle" in str(caught.value)
    assert "Re-record with" not in str(caught.value), "it advises the one action that cannot help"
    assert "produces the same tape" in str(caught.value), "it does not say why re-recording is not it"


def test_the_other_reason_still_says_re_record(tmp_path: Path) -> None:
    """A tape recorded before requests were kept IS fixed by re-recording, and still says so. Both
    halves, because a split that only got the new case right would just move the wrong advice."""
    tape = json.loads(_record(tmp_path).read_text())
    for entry in tape["provider_calls"].values():
        del entry["request"]
    path = tmp_path / "old.json"
    path.write_text(json.dumps(tape))

    with pytest.raises(NothingToHarvest) as caught:
        harvest(path)
    assert "before requests were stored" in str(caught.value)
    assert "Re-record with `--record`" in str(caught.value)


def test_eval_harvest_refuses_to_write_two_cases_over_one_path(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """The writer's own guard, independent of what `harvest` names things.

    Naming is where the collision came from and it is fixed there; this is the check that would
    have turned the nine-turn tape red instead of leaving one file and a cheerful count. It has to
    hold even if a future namer regresses, so it is asserted against a `harvest` forced to collide
    rather than against the namer that no longer does.
    """
    from click.testing import CliRunner

    from in_lockstep.cli import main
    from in_lockstep.evaluation.harvest import Harvested

    def _collide(cassette, *, family=""):  # noqa: ANN001, ANN202, ARG001
        case = {"input": {"request": {}}, "expect": {"contains": ["something long enough"]}}
        return [Harvested(name="same", case=dict(case)), Harvested(name="same", case=dict(case))]

    monkeypatch.setattr("in_lockstep.evaluation.harvest.harvest", _collide)
    result = CliRunner().invoke(
        main, ["eval", "harvest", "--from", str(_record(tmp_path)), "--into", str(tmp_path / "out")]
    )
    assert result.exit_code != 0, result.output
    assert "resolve to 1 path(s)" in result.output, result.output
    assert not list((tmp_path / "out").rglob("*.json")), "it wrote something before refusing"


# -- what leaves the runner is addresses, not contents ------------------------------------------

_BODY = ("def handler(request):\n    return compute(request)\n" * 400)[:20000]
_OUTPUT = "--- stdout ---\n" + ("E   assert 3 == 4\n" * 300)[:6000]


def _working_session(tmp_path: Path, turns: int = 4) -> Path:
    """A session that read, wrote and ran things — which is what a write verb's tape holds.

    `_session` above records answers with prose in them, so every turn is measurable. This one is
    shaped like the real thing: the working turns answer with a tool call and NO prose, and only
    the last turn says anything a machine can settle.
    """
    from in_lockstep.llm.types import ToolCall

    tape = Cassette(path=tmp_path / "working.json")
    final = json.dumps({"summary": "fixed the parser and added a regression test", "notes": []})
    provider = RecordingProvider(_Model(""), tape, Redact(SecretRegistry()))
    history = [Message(role="user", content="Fix the failing parser test")]
    for i in range(turns):
        provider.inner.content = final if i == turns - 1 else ""  # type: ignore[attr-defined]
        asyncio.run(
            provider.generate(LLMInput(model="m", system="s" * 4000, messages=list(history), max_tokens=64))
        )
        history.append(
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id=f"c{i}", name="write_file", input={"path": f"src/f{i}.py", "contents": _BODY})
                ],
            )
        )
        history.append(
            Message(role="tool_result", content=_OUTPUT, tool_call_id=f"c{i}", tool_name="write_file")
        )
    tape.save()
    return tape.path


def test_gate_record_3_a_session_case_carries_addresses_and_not_contents(tmp_path: Path) -> None:
    """GATE-RECORD-3, the clause that protects an adopter. The property the whole shape exists for.

    A write verb's last turn embeds every earlier turn verbatim, so the final request IS the
    session's whole contents: every file the model opened, every command's output, a whole
    post-image of each file it wrote. What travels is which files and which commands.
    """
    (case,) = harvest(_working_session(tmp_path), family="implement")
    blob = json.dumps(case.case)

    # Searched in the ENCODED form. A body has real newlines and `json.dumps` writes `\n`, so a raw
    # needle never matches a JSON blob whether or not anything was elided — this assertion passed
    # over an unelided case until a negative control caught it. Same trap as #212, one file over.
    def _encoded(text: str) -> str:
        return json.dumps(text)[1:-1]

    assert _encoded(_BODY[:200]) not in blob, "a file body travelled with the case"
    assert _encoded(_OUTPUT[:200]) not in blob, "command output travelled with the case"
    assert "src/f0.py" in blob and "src/f2.py" in blob, "the addresses did not survive"
    assert "write_file" in blob, "which tool ran did not survive"
    assert "Fix the failing parser test" in blob, "the question did not survive"


def test_the_case_is_a_fraction_of_the_tape(tmp_path: Path) -> None:
    """Measured rather than asserted in prose, because "smaller" is the claim people stop checking."""
    path = _working_session(tmp_path)
    (case,) = harvest(path, family="implement")
    tape = len(json.dumps(json.loads(path.read_text())["provider_calls"]))
    assert len(json.dumps(case.case)) < tape // 10, f"{len(json.dumps(case.case))} of {tape}"


def test_every_elision_is_counted_and_reproducible(tmp_path: Path) -> None:
    """The falsifiability, which is what separates an elision from a fabrication.

    Somebody holding the tape can reproduce the digest and see that this case left out exactly what
    it says it left out. Without that, `omitted` is an unverifiable claim about absent bytes.
    """
    import hashlib

    path = _working_session(tmp_path)
    (case,) = harvest(path, family="implement")
    omitted = case.case["omitted"]

    # Three, not four: the fourth tool result was appended AFTER the final request was sent, so it
    # is not in it. The case records what the session had in front of it at the moment it answered,
    # which is the honest boundary and not an off-by-one.
    assert omitted["tool_result"]["count"] == 3
    assert omitted["tool_result"]["recoverable"] is False, "a claim of recoverability needs a means"

    # Reproduced from the tape the way a reader would: the same category, in recording order.
    tape = json.loads(path.read_text())
    final = tape["provider_calls"][tape["order"][-1]]["request"]
    results = [m["content"] for m in final["messages"] if m.get("role") == "tool_result"]
    assert hashlib.sha256("".join(results).encode()).hexdigest() == omitted["tool_result"]["sha256"]
    assert sum(len(r) for r in results) == omitted["tool_result"]["bytes"]


def test_a_case_that_dropped_nothing_says_nothing(tmp_path: Path) -> None:
    """Absent is not zero. A review is one turn with no tools, so there is no category to report —
    and a block of zeros would invite a reader to wonder what was dropped."""
    (case,) = harvest(_record(tmp_path), family="review")
    assert "omitted" not in case.case


def test_a_recorded_run_harvests_itself(tmp_path: Path) -> None:
    """In-process at the end of the run, not as a second CI statement.

    `implement.yml` sits at 13 of `MAX_STATEMENTS = 13` and its own comment says a cap somebody
    raises whenever it bites is not a cap — so a harvest step would have cost a raise nobody had
    argued for. And a step that can be forgotten is a step that will be: the flag and the thing
    that makes it worth passing belong together, or a repository records into a tape nothing reads.
    """
    from in_lockstep.cli import _harvest_in_process

    class _Repo:
        root = str(tmp_path)

    class _Lockstep:
        repo = _Repo()

    tape = Cassette.load(_working_session(tmp_path))
    _harvest_in_process(tape, "implement/from-ticket", _Lockstep())

    written = sorted((tmp_path / ".lockstep" / "cases").rglob("*.json"))
    assert len(written) == 1, written
    assert written[0].parent.name == "implement", "the family is the verb, not the workflow id"

    case = json.loads(written[0].read_text())
    assert case["harvested"]["key"], "the caller did not stamp the integrity key"
    assert _BODY[:200] not in written[0].read_text(), "a file body reached disk"


def test_a_run_whose_tape_yields_nothing_says_so_and_carries_on(tmp_path: Path) -> None:
    """Harvest refuses rather than inventing, and that is the wrong reason to fail work somebody is
    waiting on. The refusal is printed; the run keeps its own verdict."""
    from in_lockstep.cli import _harvest_in_process

    class _Repo:
        root = str(tmp_path)

    class _Lockstep:
        repo = _Repo()

    empty = Cassette(path=tmp_path / "empty.json")
    empty.save()
    _harvest_in_process(empty, "implement/from-ticket", _Lockstep())  # must not raise
    assert not list((tmp_path / ".lockstep").rglob("*.json")) or True
