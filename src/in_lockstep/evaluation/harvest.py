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

import hashlib
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

    order = data.get("order") if isinstance(data, dict) else None
    sessions = _sessions(calls, [str(k) for k in order] if isinstance(order, list) else [])
    if not sessions:
        sessions = [[key] for key in sorted(calls)]

    # ONE case per session, not per call. A nine-turn session recorded nine calls, and eight of
    # them are the same conversation with one more turn on the end — so nine cases are nine copies
    # of one question, each bigger than the last, and the ninth contains all of the others.
    #
    # The one kept is the session's LAST measurable call, because that is where the answer worth
    # grading is: a write verb's earlier turns answer with a tool call and empty prose, from which
    # `_expect` can derive nothing, so a case built on turn 1 would be a case that cannot fail.
    # Its request carries the whole session, which is exactly why `_elide` exists.
    buildable: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    without_request = 0
    unmeasurable = 0
    for session in sessions:
        measurable = None
        for key in session:
            entry = calls.get(key)
            if not isinstance(entry, dict):
                continue
            request = entry.get("request")
            if not isinstance(request, dict):
                without_request += 1
                continue
            expect = _expect(_answer(entry))
            if not expect:
                # A case with no expectation cannot fail, and `Case.parse` refuses one.
                unmeasurable += 1
                continue
            measurable = (key, request, expect, entry)
        if measurable is not None:
            buildable.append(measurable)

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
        elided, omitted = _elide(request)
        out.append(
            Harvested(
                name=f"{family}/{name}" if family else name,
                case={
                    "input": {"request": elided},
                    "expect": expect,
                    # What this case does NOT carry, counted and digested. Empty when the session
                    # read nothing and ran nothing, which is every `review` recording — and an
                    # empty block is omitted entirely rather than written as zeros, because a
                    # category nobody dropped is absent, not zero.
                    **({"omitted": omitted} if omitted else {}),
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


#: A string in a tool call's input longer than this is a BODY rather than an address. `path`,
#: `pattern` and `glob` are how a session says where it went; `contents` is what it wrote, and
#: `write_file` carries the whole new file rather than a diff. Shape rather than a list of tool
#: names, so an adopter's own tool is elided on the same rule as ours (O8).
MAX_INLINE_CHARS = 200


def _extends(earlier: dict[str, Any], later: dict[str, Any]) -> bool:
    """Whether `later` is the same conversation, one or more turns on.

    `AiInvoker` builds each turn from `list(history)` and only ever appends, so within one
    invocation every request after the first begins with the whole of the one before it. Across two
    invocations -- TDD's red phase and its green phase -- the system prompt differs, so no chain
    forms, which is correct: those are two questions and deserve two cases.
    """
    if earlier.get("system") != later.get("system") or earlier.get("model") != later.get("model"):
        return False
    before, after = earlier.get("messages") or [], later.get("messages") or []
    return len(after) > len(before) and after[: len(before)] == before


def _sessions(calls: dict[str, Any], order: list[str]) -> list[list[str]]:
    """The tape's calls, grouped into the conversations that produced them.

    Grouped from `order` -- the sequence they were recorded in -- and NOT from `sorted(calls)`,
    which is hash order and says nothing about what followed what. A tape with no `order` (one
    written before it was kept) falls back to one session per call, which is what harvesting did
    before sessions existed.
    """
    grouped: list[list[str]] = []
    for key in order:
        entry = calls.get(key)
        request = entry.get("request") if isinstance(entry, dict) else None
        if not isinstance(request, dict):
            continue
        previous = calls.get(grouped[-1][-1], {}) if grouped else {}
        if grouped and _extends(previous.get("request") or {}, request):
            grouped[-1].append(key)
        else:
            grouped.append([key])
    return grouped


def _elide(request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The request with its bodies removed, and a falsifiable account of what was removed.

    A write verb's last turn embeds every earlier turn verbatim -- so the final request IS the
    session's whole contents: every file the model chose to open, every command's stdout and
    stderr, and a whole post-image of each file it wrote. That is the thing that must not travel.

    What travels instead is addresses: which files, which commands, how much. Per category a count,
    a byte total and a sha256 over exactly what was dropped, in order -- because an elision nobody
    can check is an elision that quietly becomes a fabrication, and somebody holding the tape can
    reproduce the digest and see that this case left out what it says it left out.

    `recoverable` is honest rather than hopeful. Command output is not byte-stable across runs and
    a file body is only recoverable if you know which commit it was read at, which a case does not
    record. So both are `false`, and the digest is what stands in for them.
    """
    dropped: dict[str, list[str]] = {}

    def _cut(kind: str, text: str) -> str:
        dropped.setdefault(kind, []).append(text)
        return f"[elided: {len(text)} chars of {kind}; see `omitted`]"

    messages = []
    for message in request.get("messages") or []:
        if not isinstance(message, dict):
            continue
        rebuilt = dict(message)
        if rebuilt.get("role") == "tool_result" and isinstance(rebuilt.get("content"), str):
            rebuilt["content"] = _cut("tool_result", rebuilt["content"])
        calls_out = []
        for call in rebuilt.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            rebuilt_call = dict(call)
            arguments = dict(rebuilt_call.get("input") or {})
            for field_name, value in arguments.items():
                if isinstance(value, str) and len(value) > MAX_INLINE_CHARS:
                    arguments[field_name] = _cut("tool_call_input", value)
            rebuilt_call["input"] = arguments
            calls_out.append(rebuilt_call)
        if rebuilt.get("tool_calls") is not None:
            rebuilt["tool_calls"] = calls_out
        messages.append(rebuilt)

    elided = dict(request)
    elided["messages"] = messages
    omitted = {
        kind: {
            "count": len(texts),
            "bytes": sum(len(x) for x in texts),
            "sha256": hashlib.sha256("".join(texts).encode()).hexdigest(),
            "recoverable": False,
            "why": (
                "command output is not byte-stable across runs, and a file body is recoverable "
                "only against the commit it was read at, which a case does not record"
            ),
        }
        for kind, texts in sorted(dropped.items())
    }
    return elided, omitted


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
