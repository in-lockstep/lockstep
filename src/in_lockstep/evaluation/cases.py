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


def load_cases(directory: str | Path) -> list[Case]:
    root = Path(directory)
    return [Case.load(p) for p in sorted(root.rglob("*.json"))]


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
                        "passed": actual == want,
                        "detail": f"expected {want}, got {actual}",
                    }
                )
        elif key == "contains":
            text = json.dumps(output, default=str)
            for needle in expected or []:
                checks.append(
                    {
                        "check": "contains",
                        "passed": str(needle) in text,
                        "detail": f"{needle!r}",
                    }
                )
        elif key == "absent":
            text = json.dumps(output, default=str)
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
