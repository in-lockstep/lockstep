"""Structured output, with a bounded repair loop.

Providers differ in what they can guarantee, so this degrades explicitly rather than silently:
a strategy that needs a schema checks `ModelCaps.structured_output` and is refused a model that
cannot honour one, instead of discovering it at parse time.

Where native support is absent, the request carries the schema in the prompt and the answer is
repaired — once, and then once more with the parse error quoted back. Bounded deliberately: an
unbounded repair loop against a model that cannot produce the shape is a way to spend a budget
on the same failure repeatedly.

The truncation repair is worth keeping: a JSON object cut off by a token limit is not malformed
input, it is a complete answer with its tail missing, and closing the brackets recovers it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


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
