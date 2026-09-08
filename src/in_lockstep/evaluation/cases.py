"""Eval cases: what a machine can settle, and what needs a model.

Ported from the compiler-era harness with its central contract intact. Two halves per case:

**Deterministic** — schema, equals, contains, absent, count. A machine settles these, and they
either passed or they did not.

**Rubric** — a judgement a model has to make, parsed into criteria, a scale and a bar. Until a
judge has made it, the case is *outstanding* rather than passed; a verdict handed to `grade`
settles it either way. That distinction is the whole point of the file: a suite that reports 100%
while half of it was never judged is a reassuring number computed from no evidence, and once it
lands in a baseline it is compared against forever.

Which is why `summarize` returns `pass_rate: None` rather than 1.0 when nothing was decided, and
why an outcome carries `decided` alongside its status.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
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


#: A rubric's defaults when a case states only the text: five levels, and four is the bar. Stated
#: once, here, so a judged rubric and a case author reading it agree on what "passed" meant.
RUBRIC_LEVELS = 5
RUBRIC_MINIMUM = 4
#: The object spelling the shipped corpus uses: `criteria` (a sentence or a list), `levels` (a
#: number of rungs, or a mapping of rung to what an answer on it looks like -- the anchors a
#: judge is shown), and `min`, the rung a passing answer reaches; `minimum` is the same key
#: spelled out.
RUBRIC_KEYS = ("criteria", "levels", "min", "minimum")


@dataclass(frozen=True)
class Rubric:
    """A judgement a model has to make, as a question a judge can answer with an integer.

    `criteria` are the things a passing answer does; `levels` is how many rungs the judge's
    scale has, and `minimum` the rung a passing answer reaches. A case writes either the text
    alone -- one criterion, five levels, four to pass -- or an object naming all three, and
    `parse` refuses at load what no judge could satisfy (`minimum` above `levels`, a scale of
    nothing, no criterion at all), the way `_refuse_unsatisfiable_counts` refuses a count with no
    integer in its range: a rubric whose result cannot depend on the answer is not a rubric.
    """

    text: str
    criteria: tuple[str, ...] = ()
    levels: int = RUBRIC_LEVELS
    minimum: int = RUBRIC_MINIMUM
    #: What an answer on a given rung looks like, where the case says: `(5, "names the mechanism
    #: and the fix")`. The judge is shown these; a rubric with none is graded on the criteria alone.
    anchors: tuple[tuple[int, str], ...] = ()

    @property
    def scored(self) -> bool:
        return bool(self.criteria)

    @classmethod
    def parse(cls, raw: object, *, name: str = "case") -> Rubric:
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                raise CaseError(f"{name}: a rubric with no text asks the judge nothing")
            return cls(text=text, criteria=(text,))
        if not isinstance(raw, dict):
            raise CaseError(
                f"{name}: a rubric is a sentence or an object of {list(RUBRIC_KEYS)}, not {raw!r}"
            )
        unknown = sorted(set(raw) - set(RUBRIC_KEYS))
        if unknown:
            raise CaseError(
                f"{name}: rubric names {unknown}, which no judge reads; it takes {list(RUBRIC_KEYS)}"
            )
        criteria_raw = raw.get("criteria")
        if isinstance(criteria_raw, str):
            criteria_raw = [criteria_raw]
        criteria = tuple(str(c).strip() for c in (criteria_raw or []) if str(c).strip())
        if not criteria:
            raise CaseError(f"{name}: a rubric with no criteria asks the judge nothing")
        if "min" in raw and "minimum" in raw:
            raise CaseError(f"{name}: rubric says both `min` and `minimum`; they are one bar")
        levels_raw = raw.get("levels", RUBRIC_LEVELS)
        anchors: tuple[tuple[int, str], ...] = ()
        if isinstance(levels_raw, dict):
            rungs: list[tuple[int, str]] = []
            for key, what in levels_raw.items():
                try:
                    rung = int(str(key))
                except ValueError:
                    raise CaseError(
                        f"{name}: rubric level {key!r} is not a rung a judge can answer with"
                    ) from None
                if rung < 1:
                    raise CaseError(f"{name}: rubric level {rung} is below the scale; rungs start at 1")
                rungs.append((rung, str(what)))
            if not rungs:
                raise CaseError(f"{name}: a rubric with no levels is a scale no answer sits on")
            anchors = tuple(sorted(rungs))
            levels = max(rung for rung, _ in anchors)
        else:
            levels = levels_raw
        minimum_raw = raw.get("minimum", raw.get("min", RUBRIC_MINIMUM))
        for label, value in (("levels", levels), ("min", minimum_raw)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise CaseError(f"{name}: rubric {label} is {value!r}; a judge answers with an integer")
        assert isinstance(levels, int) and isinstance(minimum_raw, int)  # noqa: S101 - narrowed above
        minimum = minimum_raw
        if levels < 1:
            raise CaseError(f"{name}: a rubric with {levels} level(s) is a scale no answer sits on")
        if not 1 <= minimum <= levels:
            raise CaseError(
                f"{name}: rubric min {minimum} is outside its {levels} level(s); "
                f"{'no' if minimum > levels else 'every'} answer would pass, whatever it said"
            )
        return cls(
            text="; ".join(criteria), criteria=criteria, levels=levels, minimum=minimum, anchors=anchors
        )

    def as_record(self) -> dict[str, Any]:
        return {
            "criteria": list(self.criteria),
            "levels": self.levels,
            "min": self.minimum,
            "anchors": [[rung, what] for rung, what in self.anchors],
        }

    def settles(self, level: int) -> bool:
        """Whether a judge's level is a pass. The one place the bar is read."""
        return level >= self.minimum


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
    #: The answer this case's expectations were derived from, so the case can be settled without
    #: the cassette it came from. A case outlives its recording: the tape is scratch, deleted with
    #: the runner that made it, while the case travels in an artifact and into a repository. Kept
    #: out of `input` for the same reason `harvested` is — a grader that could see the answer it is
    #: grading would stop being a grader.
    recorded: dict[str, Any] = field(default_factory=dict)

    @property
    def rubric(self) -> Rubric | None:
        raw = self.expect.get("rubric")
        return Rubric.parse(raw, name=self.name) if raw else None

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
        if expect.get("rubric"):
            Rubric.parse(expect["rubric"], name=name)  # refused here, before a judge is ever paid
        harvested = raw.get("harvested") or {}
        recorded = raw.get("recorded") or {}
        return cls(
            name=name,
            input=raw.get("input") or {},
            expect=expect,
            path=path,
            harvested=harvested if isinstance(harvested, dict) else {},
            recorded=recorded if isinstance(recorded, dict) else {},
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


def grade(case: Case, output: Any, verdict: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Settle the deterministic half; settle the rubric half only with a verdict handed in.

    `verdict` is a judge's answer -- `level` (an integer on the rubric's scale), `reason`, and
    whatever `evidence` it quoted -- and this function does not care who the judge was: a model
    behind `Judge`, a person at a terminal, a sidecar replaying a level already given. Without
    one the rubric stays `outstanding`, which is not a pass (`GATE-OUT-2`, `GATE-JUDGE-1`). A
    verdict for a case with no rubric is ignored rather than counted: an answer to a question
    nobody asked is not evidence.
    """
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
    result: dict[str, Any] = {
        "case": case.name,
        "checks": checks,
        "deterministic_passed": all(c["passed"] for c in checks) if checks else None,
        # Outstanding, not passed. A judge has to answer this and has not yet.
        "rubric_outstanding": bool(rubric and rubric.scored),
        "rubric": rubric.text if rubric else "",
        "rubric_passed": None,
    }
    if rubric is not None and rubric.scored and verdict is not None:
        level = verdict.get("level")
        if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= rubric.levels:
            # A level off the scale is not a verdict, and the rubric stays outstanding rather than
            # being read as whichever side of the bar a bad number happens to fall on.
            result["rubric_reason"] = (
                f"judge answered {level!r}, not a level on a {rubric.levels}-point scale"
            )
            return result
        result["rubric_outstanding"] = False
        result["rubric_passed"] = rubric.settles(level)
        result["rubric_level"] = level
        result["rubric_reason"] = str(verdict.get("reason", ""))
        result["rubric_evidence"] = [str(e) for e in (verdict.get("evidence") or [])]
    return result


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """A pass rate over cases that were actually decided, or None if none were.

    Returning 1.0 for a suite that decided nothing would put a perfect score computed from no
    evidence into a baseline, and it would be compared against forever.
    """
    # Decided: nothing left outstanding, and at least one half actually said something -- a
    # deterministic check, or a rubric a judge settled. A judged rubric decides a case on its
    # own; a case with checks and an unjudged rubric is still outstanding, because half an
    # answer is not one.
    decided = [
        r
        for r in results
        if not r.get("rubric_outstanding")
        and (r.get("deterministic_passed") is not None or r.get("rubric_passed") is not None)
    ]
    outstanding = [r for r in results if r.get("rubric_outstanding")]
    passed = [r for r in decided if _passed(r)]
    judged = [r for r in results if r.get("rubric_passed") is not None]
    return {
        "total": len(results),
        "decided": len(decided),
        "outstanding": len(outstanding),
        "judged": len(judged),
        "passed": len(passed),
        "pass_rate": (len(passed) / len(decided)) if decided else None,
        # `ok` stays true when nothing was decided: a run is not blocked by its own honesty. A
        # judged rubric that failed is a failure like a check that failed (GATE-JUDGE-1).
        "ok": all(
            r["deterministic_passed"] is not False and r.get("rubric_passed") is not False for r in results
        ),
    }


def _passed(result: Mapping[str, Any]) -> bool:
    """Both halves that were settled agree it passed; a half that was not settled abstains."""
    return result.get("deterministic_passed") is not False and result.get("rubric_passed") is not False
