"""Eval cases: what a machine can settle, and what needs a model.

Ported from the compiler-era harness with its central contract intact. Two halves per case:

**Deterministic** — schema, equals, contains, absent, count. A machine settles these, and they
either passed or they did not.

**Rubric** — a judgement a model has to make. Until a judge has made it, the case is *outstanding*
rather than passed. That distinction is the whole point of the file: a suite that reports 100%
while half of it was never judged is a reassuring number computed from no evidence, and once it
lands in a baseline it is compared against forever.

Which is why `summarize` returns `pass_rate: None` rather than 1.0 when nothing was decided, and
why an outcome carries `decided` alongside its status.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DETERMINISTIC_KEYS = ("schema", "equals", "contains", "absent", "count")
EXPECT_KEYS = (*DETERMINISTIC_KEYS, "rubric")

#: What a `count` may say beyond an exact number. Closed, and validated at parse, for the same
#: reason `EXPECT_KEYS` is: an unrecognised comparator reads like it means something and settles
#: nothing. `{"at_least": 1}` looked fine in twelve shipped cases and meant "fails forever".
COUNT_COMPARATORS = ("min", "max")


class CaseError(ValueError):
    """A case that cannot mean what it says."""


@dataclass(frozen=True)
class Rubric:
    text: str

    @property
    def scored(self) -> bool:
        return bool(self.text)


@dataclass(frozen=True)
class Case:
    name: str
    input: dict[str, Any] = field(default_factory=dict)
    expect: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None
    #: Where a harvested case came from — the cassette, the key, the model. Never graded, and
    #: kept out of `input` on purpose: a grader that could see it might come to depend on it, and
    #: then a case would mean something different depending on how it was made.
    harvested: dict[str, Any] = field(default_factory=dict)

    @property
    def rubric(self) -> Rubric | None:
        text = self.expect.get("rubric")
        return Rubric(str(text)) if text else None

    @property
    def deterministic(self) -> dict[str, Any]:
        return {k: v for k, v in self.expect.items() if k in DETERMINISTIC_KEYS}

    @classmethod
    def parse(cls, raw: object, *, name: str, path: Path | None = None) -> Case:
        if not isinstance(raw, dict):
            raise CaseError(f"{name}: a case must be an object")
        expect = raw.get("expect") or {}
        if not isinstance(expect, dict):
            raise CaseError(f"{name}: `expect` must be an object")
        unknown = set(expect) - set(EXPECT_KEYS)
        if unknown:
            raise CaseError(
                f"{name}: unknown expectation(s) {sorted(unknown)}; "
                f"a case that expects something nothing checks is a case that always passes"
            )
        if not expect:
            raise CaseError(f"{name}: a case with no expectation cannot fail")
        _refuse_unsatisfiable_counts(expect, name=name)
        harvested = raw.get("harvested") or {}
        return cls(
            name=name,
            input=raw.get("input") or {},
            expect=expect,
            path=path,
            harvested=harvested if isinstance(harvested, dict) else {},
        )

    @classmethod
    def load(cls, path: Path) -> Case:
        return cls.parse(json.loads(path.read_text()), name=path.stem, path=path)


def _refuse_unsatisfiable_counts(expect: dict[str, Any], *, name: str) -> None:
    """The unknown-key refusal above, from the other side.

    That one refuses a case that always passes. This refuses a case that always fails: a comparator
    nothing implements, a bound that is not a number, or a range with no integer in it. Both are
    cases whose result does not depend on the answer, and a corpus full of those reports a rate
    against a comparison that was never made.
    """
    counts = expect.get("count")
    if counts is None:
        return
    if not isinstance(counts, dict):
        raise CaseError(f"{name}: `count` maps a field name to a number or a range, not {counts!r}")

    for field_name, want in counts.items():
        where = f"{name}: count.{field_name}"
        # bool is an int, and `{"findings": True}` would otherwise quietly mean "exactly one".
        if isinstance(want, bool) or not isinstance(want, (int, dict)):
            raise CaseError(
                f"{where} is {want!r}; a count is a number, or a range of {list(COUNT_COMPARATORS)}"
            )
        if not isinstance(want, dict):
            continue
        unknown = sorted(set(want) - set(COUNT_COMPARATORS))
        if unknown or not want:
            raise CaseError(
                f"{where} says {unknown or 'nothing'}; a range is {list(COUNT_COMPARATORS)}, and a "
                f"comparator nothing implements is a case no answer can pass"
            )
        if any(isinstance(bound, bool) or not isinstance(bound, int) for bound in want.values()):
            raise CaseError(f"{where} bounds must be numbers, not {sorted(want.values(), key=repr)!r}")
        low, high = want.get("min"), want.get("max")
        if low is not None and high is not None and low > high:
            raise CaseError(f"{where} wants at least {low} and at most {high}, which no answer satisfies")


def load_cases(directory: str | Path) -> list[Case]:
    root = Path(directory)
    return [Case.load(p) for p in sorted(root.rglob("*.json"))]


def _searchable(output: Any) -> str:
    """An answer as text a needle can be looked for in.

    `json.dumps` was what this searched, and it made a whole class of expectation unsatisfiable: a
    quote, a backslash or a newline in the answer is ESCAPED in the JSON encoding, so a needle
    lifted from the answer's own prose never matched the answer it came from. `eval harvest` prints
    "these pass against those answers today" and it was not true of any answer containing a quote.

    Keys are included as well as values, so an `absent` expectation naming a field still finds it.
    Nothing shipped relied on the JSON punctuation — checked before changing this — so a needle now
    means what a person reading the answer would think it means.
    """
    parts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, dict):
            for key, value in node.items():
                parts.append(str(key))
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
        elif node is not None:
            parts.append(str(node))

    walk(output)
    return "\n".join(parts)


def _count_holds(actual: int | None, want: Any) -> bool:
    """Whether a length satisfies a count expectation.

    An exact number means exactly that many, because several cases count zero and mean it: a
    reviewer that invents a finding on `nothing-to-find` is the failure that case exists to catch,
    and reading the exact form as a floor would let it through. A mapping is a range, which is what
    "found at least one" needs -- an answer reporting two real findings where one was expected is
    right, and comparing a length to `{"min": 1}` with `==` called it wrong for every answer any
    model could give (#194).
    """
    if actual is None:
        return False
    if isinstance(want, dict):
        low, high = want.get("min"), want.get("max")
        return bool((low is None or actual >= low) and (high is None or actual <= high))
    return bool(actual == want)


def _count_says(want: Any) -> str:
    """The expectation in words. `expected {'min': 1}, got 1` is a true sentence that reads as a
    contradiction, and it is what a person sees at the moment they are trying to tell a real
    failure from a broken check."""
    if not isinstance(want, dict):
        return f"exactly {want}"
    bounds = [
        f"at least {want[k]}" if k == "min" else f"at most {want[k]}" for k in COUNT_COMPARATORS if k in want
    ]
    return " and ".join(bounds)


def grade(case: Case, output: Any) -> dict[str, Any]:
    """Settle the deterministic half. The rubric half is recorded as outstanding, not assumed."""
    checks: list[dict[str, Any]] = []

    for key, expected in case.deterministic.items():
        if key == "schema":
            missing = [k for k in expected if not (isinstance(output, dict) and k in output)]
            checks.append(
                {
                    "check": "schema",
                    "passed": not missing,
                    "detail": f"missing {missing}" if missing else "",
                }
            )
        elif key == "count":
            for field_name, want in (expected or {}).items():
                got = output.get(field_name) if isinstance(output, dict) else None
                actual = len(got) if isinstance(got, (list, tuple)) else None
                checks.append(
                    {
                        "check": f"count.{field_name}",
                        "passed": _count_holds(actual, want),
                        "detail": f"expected {_count_says(want)}, got {actual}",
                    }
                )
        elif key == "contains":
            text = _searchable(output)
            for needle in expected or []:
                checks.append(
                    {
                        "check": "contains",
                        "passed": str(needle) in text,
                        "detail": f"{needle!r}",
                    }
                )
        elif key == "absent":
            text = _searchable(output)
            for needle in expected or []:
                checks.append(
                    {
                        "check": "absent",
                        "passed": str(needle) not in text,
                        "detail": f"{needle!r}",
                    }
                )
        elif key == "equals":
            checks.append({"check": "equals", "passed": output == expected, "detail": ""})

    rubric = case.rubric
    return {
        "case": case.name,
        "checks": checks,
        "deterministic_passed": all(c["passed"] for c in checks) if checks else None,
        # Outstanding, not passed. A judge has to answer this and has not yet.
        "rubric_outstanding": bool(rubric and rubric.scored),
        "rubric": rubric.text if rubric else "",
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """A pass rate over cases that were actually decided, or None if none were.

    Returning 1.0 for a suite that decided nothing would put a perfect score computed from no
    evidence into a baseline, and it would be compared against forever.
    """
    decided = [
        r for r in results if r.get("deterministic_passed") is not None and not r.get("rubric_outstanding")
    ]
    outstanding = [r for r in results if r.get("rubric_outstanding")]
    passed = [r for r in decided if r["deterministic_passed"]]
    return {
        "total": len(results),
        "decided": len(decided),
        "outstanding": len(outstanding),
        "passed": len(passed),
        "pass_rate": (len(passed) / len(decided)) if decided else None,
        # `ok` stays true when nothing was decided: a run is not blocked by its own honesty.
        "ok": all(r["deterministic_passed"] is not False for r in results),
    }
