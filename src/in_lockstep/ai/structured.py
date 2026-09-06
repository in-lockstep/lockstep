"""Structured output, with a bounded repair loop.

Providers differ in what they can guarantee, so this degrades explicitly rather than silently:
a call that needs a schema says so -- `invoker.run(schema=...)` -- and `AiInvoker` refuses a model
whose registration declares `ModelCaps.structured_output=False` before the first turn, naming the
model and the capability, instead of discovering it at parse time. This paragraph promised that
check for as long as the field existed and nothing made it (#274, `GATE-MODEL-1`); the check
lives in the invoker rather than here because the invoker is the thing that knows the model.

Where native support is absent, the request carries the schema in the prompt and the answer is
repaired — once, and then once more with the parse error quoted back. Bounded deliberately: an
unbounded repair loop against a model that cannot produce the shape is a way to spend a budget
on the same failure repeatedly.

That second sentence was a promise this module made and nothing kept. `parse` repaired a
truncated object by closing its brackets and then gave up, and the "once more with the error
quoted back" existed only here. Three reviews on this repository's own required check errored
`review.unparseable` on `Expecting ',' delimiter` a few hundred characters in — a malformed
object, not a cut-off one, which bracket-closing cannot touch — and each one turned a required
check red on a change nothing was wrong with (#254). `settle` is the re-prompt: one more turn,
carrying the reply and the parser's own words, and never a third.

The truncation repair is worth keeping: a JSON object cut off by a token limit is not malformed
input, it is a complete answer with its tail missing, and closing the brackets recovers it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .invoker import Invoker


class SchemaError(Exception):
    """The model did not produce the required shape, after repair."""


def extract_json(text: str) -> str:
    """Pull a JSON document out of a reply that may have wrapped it in prose or a fence."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
        stripped = stripped.strip()
    if stripped.startswith(("{", "[")):
        return stripped
    for opener, closer in (("{", "}"), ("[", "]")):
        start = stripped.find(opener)
        end = stripped.rfind(closer)
        if start != -1 and end > start:
            return stripped[start : end + 1]
    return stripped


def repair_truncated(text: str) -> str:
    """Close brackets a token limit cut off, respecting strings and escapes."""
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]" and stack:
            stack.pop()
    if in_string:
        text += '"'
    return text + "".join(reversed(stack))


@dataclass
class ParseResult:
    value: Any
    repaired: bool = False
    attempts: int = 1


def parse(text: str) -> ParseResult:
    """Parse, then repair once, then give up and say what happened."""
    candidate = extract_json(text)
    try:
        return ParseResult(value=json.loads(candidate))
    except json.JSONDecodeError as first:
        try:
            return ParseResult(value=json.loads(repair_truncated(candidate)), repaired=True, attempts=2)
        except json.JSONDecodeError:
            raise SchemaError(
                f"the reply is not JSON and could not be repaired: {first.msg} at position {first.pos}"
            ) from first


@dataclass
class Settled:
    """What one structured answer came to, after at most one re-prompt.

    `invocation` is the LAST call's, with the turns, cost and findings of both calls folded in when
    a re-prompt happened, so an adapter reporting a failure reports what it actually cost.
    `reason` is empty on success; `truncated` says the reply hit the output cap (never
    re-prompted -- that is a policy number, not a shape the model chose); `unparseable` and
    `schema_mismatch` say what still failed after the re-prompt, with `problems` holding every
    error seen, first attempt first.
    """

    invocation: Any
    value: Any = None
    reason: str = ""
    problems: tuple[str, ...] = ()
    reprompted: bool = False

    @property
    def detail(self) -> str:
        """The failure, in one line naming both attempts when there were two."""
        if not self.reprompted:
            return self.problems[0] if self.problems else ""
        first = "; ".join(self.problems[: self._split])
        second = "; ".join(self.problems[self._split :])
        return f"first reply: {first}. After one re-prompt quoting that back: {second}"

    _split: int = 1


def _shape(content: str, schema: dict[str, Any]) -> tuple[Any, tuple[str, ...], str]:
    """Parse and validate one reply: (value, problems, reason). Empty reason means it is usable."""
    try:
        value = parse(content).value
    except SchemaError as e:
        return None, (str(e),), "unparseable"
    problems = tuple(validate(value, schema))
    if problems:
        return None, problems, "schema_mismatch"
    return value, (), ""


def reprompt_text(problems: tuple[str, ...]) -> str:
    """The one follow-up turn: the parser's own words, and the ask restated. Nothing about what
    the answer should SAY -- a re-prompt that restated the question would be asking twice."""
    listed = "\n".join(f"- {p}" for p in problems)
    return (
        f"Your previous reply could not be used:\n{listed}\n\n"
        "Reply again with a single JSON document and nothing else -- no prose, no code fence -- that "
        "validates against the output schema you were given. Keep what you found; change only the shape."
    )


async def settle(
    invoker: Invoker,
    invocation: Any,
    *,
    schema: dict[str, Any],
    system: str,
    messages: list[Any],
    context: Any = None,
    policy: Any = None,
) -> Settled:
    """The reply as the schema requires it, re-prompting once when it is not.

    Takes the FIRST invocation rather than making it, so an adapter's own handling of a refusal,
    an egress decision or a provider failure around that call is untouched -- and calls the
    invoker again inside the same adapter step, so the second bill lands under the same budget
    middleware and the same recording as the first (O4). A truncated reply is returned as it is:
    an output cap is a number in the policy, and a re-prompt would pay for the same cut-off
    again. Once and never a third time, because a model that cannot produce the shape after being
    shown exactly what was wrong with it will not on the third try, and the third bill is the one
    nobody argued for.
    """
    if invocation.truncated:
        return Settled(invocation=invocation, reason="truncated")
    value, problems, reason = _shape(invocation.content, schema)
    if not reason:
        return Settled(invocation=invocation, value=value)

    from dataclasses import replace

    from ..llm.types import Message
    from .invoker import InvokePolicy

    history = [
        *messages,
        Message(role="assistant", content=invocation.content),
        Message(role="user", content=reprompt_text(problems)),
    ]
    second = await invoker.run(
        system=system,
        messages=history,
        context=context,
        # One answer turn and no tools: the shape is being fixed, not the work redone.
        policy=replace(policy if policy is not None else InvokePolicy(), max_turns=1),
        schema=schema,
    )
    merged = replace(
        second,
        turns=invocation.turns + second.turns,
        cost=invocation.cost + second.cost,
        # The context was scanned once already; the second scan is over the same text.
        findings=invocation.findings,
    )
    if second.truncated:
        return Settled(invocation=merged, reason="truncated", problems=problems, reprompted=True)
    value, again, reason_again = _shape(second.content, schema)
    if not reason_again:
        return Settled(invocation=merged, value=value, problems=problems, reprompted=True)
    return Settled(
        invocation=merged,
        reason=reason_again,
        problems=problems + again,
        reprompted=True,
        _split=len(problems),
    )


def schema_instruction(schema: dict[str, Any]) -> str:
    """What to append to a system prompt when a provider has no native schema mode."""
    return (
        "## Output format\n\n"
        "Reply with a single JSON document and nothing else — no prose, no code fence.\n"
        "It must validate against this schema:\n\n"
        f"{json.dumps(schema, indent=2)}"
    )


def validate(value: Any, schema: dict[str, Any]) -> list[str]:
    """A deliberately small structural check: required keys and top-level types.

    Not a full JSON Schema implementation. It catches the failures that actually happen — a
    missing key, an object where a list was asked for — without taking a dependency whose
    behaviour would then need its own tests.
    """
    problems: list[str] = []
    expected = schema.get("type")
    if expected == "object" and not isinstance(value, dict):
        return [f"expected an object, got {type(value).__name__}"]
    if expected == "array" and not isinstance(value, list):
        return [f"expected an array, got {type(value).__name__}"]
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"missing required key {key!r}")
        properties = schema.get("properties", {})
        for key, spec in properties.items():
            if key not in value:
                continue
            want = spec.get("type")
            got = value[key]
            if want == "array" and not isinstance(got, list):
                problems.append(f"{key!r} should be an array")
            elif want == "string" and not isinstance(got, str):
                problems.append(f"{key!r} should be a string")
            elif want == "object" and not isinstance(got, dict):
                problems.append(f"{key!r} should be an object")
    return problems
