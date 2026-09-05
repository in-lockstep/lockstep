"""Turning recorded runs into cases you can measure against.

The corpus that ships was written by hand, and a hand-written corpus has a ceiling: it holds the
situations somebody thought of. The situations that actually matter are the ones a repository ran
into — the ticket with no acceptance criteria, the diff that was mostly a lockfile, the review that
found nothing on a change that broke production a week later. Those went past every day and left a
recording, and nothing turned them into anything.

So this reads a cassette and writes cases. What comes out is what happened: a real request that was
really sent, and the answer that really came back, with expectations derived from that answer.

## What a harvested case is worth, and what it is not

A case whose expectations were derived from an answer will pass against that answer. That is
circular, and pretending otherwise would be the whole point of this project abandoned in one file.
It is worth having anyway, for the same reason a characterization test is:

*It settles.* Every deterministic expectation is checked against a real answer rather than reported
outstanding forever, which is what the corpus did before — twenty-seven cases, nothing decided.

*It fails when something below the model changes.* Parsing, schema repair, the adapter that turns a
reply into an outcome, the renderer. Those break silently today and a harvested case catches them.

*It is the baseline half of a comparison.* The recorded request is kept whole, so the same question
can be put to a changed prompt or a different model later and the two answers compared against the
same expectations. That second run is a real model call and costs real money; what this file
removes is the part that used to make it not worth doing — having nothing to compare against.

The honest boundary, stated here once and repeated wherever it matters: **replaying is free and
measures everything below the model; changing the prompt costs a call.** Nothing here pretends to
have found a way around that, and the day something claims to, this paragraph is the thing it has
to argue with.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: How much of a recorded answer becomes a `contains` expectation. A whole answer pinned verbatim
#: is a case that fails on any rewording, which teaches people to delete cases; a couple of the
#: most specific strings in it is a case that fails when the substance moves.
MAX_CONTAINS = 3

#: Strings short enough to appear by accident. A `contains` on "id" is a check that cannot fail.
MIN_NEEDLE_CHARS = 12


class NothingToHarvest(ValueError):
    """A cassette that cannot produce cases, with the reason a person can act on."""


@dataclass(frozen=True)
class Harvested:
    """One case, and the name it should be filed under.

    Deliberately does not write itself. A case holds a real request and a real answer, which is
    exactly the pair that has to pass through the redacting sink on its way to disk — and this
    layer may not reach `privileged`. So the caller writes, and the gate in `test_sinks.py` is what
    notices if one ever forgets.
    """

    name: str
    case: dict[str, Any]

    def path_in(self, root: Path) -> Path:
        return root / f"{self.name}.json"


def harvest(cassette: Path | str, *, family: str = "") -> list[Harvested]:
    """Every recorded call in `cassette` that carries its request, as a case.

    Calls recorded before requests were stored are skipped rather than guessed at — their answer is
    real, and a case wrapped around an answer whose question is unknown is a case that cannot be
    re-run, which is the one thing a case is for.
    """
    path = Path(cassette)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise NothingToHarvest(f"could not read {path}: {e}") from None

    calls = data.get("provider_calls") if isinstance(data, dict) else None
    if not isinstance(calls, dict) or not calls:
        raise NothingToHarvest(f"{path} holds no recorded calls")

    buildable: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    without_request = 0
    unmeasurable = 0
    for key, entry in sorted(calls.items()):
        if not isinstance(entry, dict):
            continue
        request = entry.get("request")
        if not isinstance(request, dict):
            without_request += 1
            continue
        expect = _expect(_answer(entry))
        if not expect:
            # A case with no expectation cannot fail, and `Case.parse` refuses one. An answer that
            # is neither JSON nor long enough to quote is a recording, not a measurement.
            unmeasurable += 1
            continue
        buildable.append((key, request, expect, entry))

    # Names are decided over the WHOLE tape, not one call at a time, because a name is only
    # ambiguous relative to its neighbours. `_stem` reads the last user message, and in a
    # multi-turn session that is the ticket every turn shares: tool results come back as
    # `role="tool_result"` (`invoker.py`), never as `user`, so a nine-turn implement run produced
    # nine cases with one name and `_eval_harvest` wrote nine files over each other. It reported
    # "9 case(s)" and left one, which is the shape of loss that looks like success.
    #
    # Suffixed from the KEY rather than a counter, so the name says which call it is rather than
    # what order it happened to be read in — and ALL of the colliding ones are suffixed, because
    # letting the first keep the clean name would invent a primacy the tape does not have. A tape
    # with one call per stem, which is every `review` recording, keeps the readable name.
    stems = [_stem(request) or f"call-{key[:8]}" for key, request, _, _ in buildable]
    shared = Counter(stems)

    out: list[Harvested] = []
    for (key, request, expect, entry), stem in zip(buildable, stems, strict=True):
        name = f"{stem}-{key[:8]}" if shared[stem] > 1 else stem
        out.append(
            Harvested(
                name=f"{family}/{name}" if family else name,
                case={
                    "input": {"request": request},
                    "expect": expect,
                    # The answer, in the case. Without this a case is a pointer at a cassette, and
                    # a pointer is worth what the thing it points at is still there: a case that
                    # travels — out of a CI runner in an artifact, into a repository, between two
                    # machines — arrives beside a path that no longer exists, and `eval run`
                    # reports it unplayable for a reason nobody can act on. The tape is the
                    # scratch; the case is the thing meant to last, so the case carries what it
                    # needs. `harvested.cassette` stays as provenance, not as a dependency.
                    "recorded": _recorded(entry),
                    # Not read by the grader. Read by a person asking "where did this come from,
                    # and is it still the kind of work we do?" — which is the question that decides
                    # whether a case is worth keeping.
                    # `key` is deliberately absent here and stamped by the caller. It is the hash
                    # of the request as this case carries it, and hashing a request is
                    # `ai.replay._key`'s job -- which this layer may not import, because
                    # `evaluation` is a leaf. Reimplementing the hash to avoid the import would be
                    # two writers of one format, and the one that drifted would drift silently.
                    #
                    # `filed_under` is what this layer DOES know: the key the tape filed the entry
                    # under. It is provenance, not integrity -- the two differ whenever redaction
                    # masked anything, because a tape is keyed on the request that was sent and
                    # holds the one that was written. Filing that key as `key` made a redacted case
                    # accuse itself of carrying somebody else's answer.
                    "harvested": {
                        "cassette": str(path),
                        "filed_under": key,
                        "model": str(request.get("model", "")),
                    },
                },
            )
        )

    if not out:
        # Split by cause. One message covered both and named the wrong one for the commoner case:
        # a tape whose every answer was unmeasurable was told its requests had not been stored, so
        # the advice was "re-record" when re-recording would produce exactly the same tape.
        if without_request and not unmeasurable:
            raise NothingToHarvest(
                f"{path} has {without_request} call(s) recorded before requests were stored, and no "
                f"case can be built from an answer whose question was not kept. Re-record with "
                f"`--record` to harvest from it."
            )
        if unmeasurable and not without_request:
            raise NothingToHarvest(
                f"{path} has {unmeasurable} call(s) whose answers state nothing a machine can "
                f"settle -- not JSON, and no string long enough to be worth quoting. Re-recording "
                f"produces the same tape; what would change this is a prompt whose answer has a "
                f"shape, which is what the structured output schema is for."
            )
        raise NothingToHarvest(
            f"{path} has {without_request} call(s) recorded before requests were stored and "
            f"{unmeasurable} whose answers state nothing a machine can settle, so no case can be "
            f"built from either."
        )
    return out


def _recorded(entry: dict[str, Any]) -> dict[str, Any]:
    """The recorded answer, exactly as the cassette holds it.

    The same field set `Cassette.replay_provider` reconstructs an `LLMOutput` from, so a case can
    be settled without the cassette and settled identically. Copied rather than reshaped: a
    friendlier summary here would be a second format for the same thing, and the two would drift.
    """
    return {
        "content": str(entry.get("content", "")),
        "tool_calls": list(entry.get("tool_calls") or []),
        "usage": dict(entry.get("usage") or {}),
        "stop_reason": str(entry.get("stop_reason", "")),
    }


def _answer(entry: dict[str, Any]) -> Any:
    """The recorded reply, parsed if it is JSON. Text otherwise, which is still gradeable."""
    content = str(entry.get("content", ""))
    try:
        return json.loads(content)
    except ValueError:
        return content


def _expect(answer: Any) -> dict[str, Any]:
    """Expectations a machine can settle, derived from what the model actually returned.

    Deliberately no rubric. A rubric is a judgement somebody has to make, and inventing one here
    would put a question nobody asked into a corpus where it would sit outstanding forever — the
    exact failure the shipped corpus already demonstrates twenty-seven times over.
    """
    expect: dict[str, Any] = {}
    if isinstance(answer, dict):
        keys = sorted(answer)
        if keys:
            expect["schema"] = keys
        # A FLOOR, not the exact number the old answer happened to reach. These cases are the
        # baseline a prompt change is measured against, and a prompt improved to catch one more
        # real vulnerability used to fail the corpus that existed to measure the improvement — the
        # metric was an inverse of its purpose. Same shape as #194/#195, which fixed it in the
        # hand-written corpus; the lesson never reached the harvester that writes the cases.
        #
        # Zero stays exact. "Found nothing" is a claim a single finding violates, and a floor of
        # zero would make `nothing-to-find` a case that cannot fail, which is the defect
        # `Case.parse` refuses by name at the other end.
        counts: dict[str, Any] = {
            k: ({"min": len(v)} if v else 0) for k, v in answer.items() if isinstance(v, list)
        }
        if counts:
            expect["count"] = counts
    needles = _needles(answer)
    if needles:
        expect["contains"] = needles
    return expect


def _needles(answer: Any) -> list[str]:
    """The most specific strings in an answer, longest first.

    Longest first because length is the cheapest available proxy for specificity: "the format
    parameter is validated against the shipped templates" is a claim, and "bug" is a word that
    appears in anything. A short string is a check that passes by accident, and a check that cannot
    fail is worse than no check because it counts toward a total.
    """
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if len(node) >= MIN_NEEDLE_CHARS:
                found.append(node)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(answer)
    # A whole paragraph pinned verbatim fails on any rewording. One line of it is the claim.
    trimmed = [s.strip().splitlines()[0][:120] for s in found if s.strip()]
    unique = sorted({s for s in trimmed if len(s) >= MIN_NEEDLE_CHARS}, key=len, reverse=True)
    return unique[:MAX_CONTAINS]


def _stem(request: dict[str, Any]) -> str:
    """A filename a person can recognise later, or "" when the request says nothing usable.

    The last user message is what the run was actually asked, so its first words are the best short
    name available — far better than a hash somebody would have to grep for. It is only a STEM,
    because in a multi-turn session every turn shares it: the caller decides what makes it unique.
    """
    messages = request.get("messages")
    last = ""
    if isinstance(messages, list):
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                last = str(message.get("content", ""))
                break
    words = [w for w in "".join(c if c.isalnum() else " " for c in last.lower()).split() if w][:6]
    return "-".join(words)
