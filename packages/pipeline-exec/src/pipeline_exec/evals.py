"""Grading one agent's output against one eval case.

An eval case used to be a JSON file with an `expect.notes` string in it — prose describing what a
good answer looks like, addressed to a human who was never going to read it. `lockstep lint`
required the file to exist and nothing required it to say anything, so "no agent ships without
evals" was a check on the file system.

This is the contract that makes a case assert something. It has two halves, and the split is the
same one `enforce:` draws elsewhere in this framework:

**The deterministic half** — `schema`, `equals`, `contains`, `absent`, `count` — is applied here, and
means the same thing on every run. It is what a case can promise.

**The judged half** — `rubric` — is prose, because "cites the file and says what an attacker does"
is not a substring match and pretending otherwise produces a test that passes on nonsense. It is
graded by a model, which makes it a weaker signal than the deterministic half and an honest one:
this module records that a rubric is outstanding rather than quietly scoring it itself.

A case must assert at least one of the two. A case that asserts nothing passed before it was
written, which is the failure mode this whole contract exists to remove.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# What `expect` may contain. Declared rather than inferred, so an unknown key is a typo somebody
# hears about instead of an expectation that silently never ran.
DETERMINISTIC_KEYS = ("schema", "equals", "contains", "absent", "count")
EXPECT_KEYS = (*DETERMINISTIC_KEYS, "rubric")


class CaseError(ValueError):
    """A case that cannot be graded, which is a defect in the case rather than in the agent."""


@dataclass
class Check:
    check: str
    target: str
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"check": self.check, "target": self.target, "passed": self.passed, "detail": self.detail}


@dataclass
class Case:
    name: str
    input: Any
    expect: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def rubric(self) -> str:
        return str(self.expect.get("rubric") or "")

    @property
    def deterministic(self) -> dict[str, Any]:
        return {key: self.expect[key] for key in DETERMINISTIC_KEYS if key in self.expect}

    @classmethod
    def load(cls, path: Path) -> Case:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CaseError(f"{path.name}: not valid JSON ({exc.msg} at line {exc.lineno})") from None
        return cls.parse(raw, name=str(raw.get("case") or path.stem))

    @classmethod
    def parse(cls, raw: Any, *, name: str) -> Case:
        if not isinstance(raw, dict):
            raise CaseError(f"{name}: a case is an object, not {type(raw).__name__}")
        if "input" not in raw:
            raise CaseError(f"{name}: no `input` — a case has to say what the agent was given")

        expect = raw.get("expect") or {}
        if not isinstance(expect, dict):
            raise CaseError(f"{name}: `expect` is an object, not {type(expect).__name__}")

        unknown = sorted(set(expect) - set(EXPECT_KEYS))
        if unknown:
            raise CaseError(
                f"{name}: unknown expectation(s) {', '.join(unknown)} — "
                f"known: {', '.join(EXPECT_KEYS)}"
            )
        if not expect:
            raise CaseError(
                f"{name}: `expect` asserts nothing, so this case passes for any output. "
                f"Add one of: {', '.join(EXPECT_KEYS)}"
            )
        return cls(name=name, input=raw["input"], expect=expect, context=raw.get("context") or {})


def grade(case: Case, output: Any) -> dict[str, Any]:
    """Apply the deterministic half of a case to one agent output.

    The rubric is reported as outstanding rather than judged: this function has no model, and a
    grader that quietly scored prose itself would be inventing the number it reports.
    """
    checks: list[Check] = []
    blob = _searchable(output)

    for field_name in _as_list(case.expect.get("schema")):
        present = isinstance(output, dict) and field_name in output
        checks.append(
            Check("schema", field_name, present, "" if present else "not a top-level field of the output")
        )

    for field_name, expected in (case.expect.get("equals") or {}).items():
        actual = output.get(field_name) if isinstance(output, dict) else None
        matches = actual == expected
        checks.append(
            Check("equals", field_name, matches, "" if matches else f"{actual!r} != {expected!r}")
        )

    for needle in _as_list(case.expect.get("contains")):
        found = needle.lower() in blob
        checks.append(Check("contains", needle, found, "" if found else "not found anywhere in the output"))

    for needle in _as_list(case.expect.get("absent")):
        found = needle.lower() in blob
        checks.append(Check("absent", needle, not found, "present in the output" if found else ""))

    for target, bounds in (case.expect.get("count") or {}).items():
        checks.append(_count_check(output, target, bounds))

    deterministic_passed = all(check.passed for check in checks)
    return {
        "case": case.name,
        "checks": [check.as_dict() for check in checks],
        "deterministic_passed": deterministic_passed,
        # A rubric is not graded here. `passed` is therefore provisional whenever one exists, and
        # says so, rather than reporting a pass that only covers half the case.
        "rubric": case.rubric,
        "rubric_pending": bool(case.rubric),
        "passed": deterministic_passed and not case.rubric,
    }


def _count_check(output: Any, target: str, bounds: Any) -> Check:
    value = output.get(target) if isinstance(output, dict) else None
    if not isinstance(value, (list, tuple, str, dict)):
        return Check("count", target, False, f"{target!r} is not something with a length")
    size = len(value)
    if not isinstance(bounds, dict):
        return Check("count", target, size == bounds, f"{size} != {bounds}" if size != bounds else "")

    low, high = bounds.get("min"), bounds.get("max")
    if low is not None and size < low:
        return Check("count", target, False, f"{size} < min {low}")
    if high is not None and size > high:
        return Check("count", target, False, f"{size} > max {high}")
    return Check("count", target, True, f"{size}")


def _searchable(output: Any) -> str:
    """Everything in the output, flattened and lowercased.

    `contains` asks whether the agent mentioned something, and an agent may legitimately put it in a
    nested field, an array, or prose. Searching the serialized form answers the question actually
    being asked instead of requiring the case to know the output's shape.
    """
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    return text.lower()


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def expand(cases: list[Case], directory: Path) -> list[dict[str, Any]]:
    """Write one input file per case and describe the items a fan-out will run over.

    The agent contract is `input_path` in, `output_path` out — the same one every other agent step
    uses. An eval is therefore not a special way of running an agent; it is the ordinary way, with
    the input coming from a case file instead of an earlier step.
    """
    directory.mkdir(parents=True, exist_ok=True)
    items = []
    for case in cases:
        (directory / f"{case.name}.json").write_text(
            json.dumps(case.input, indent=2) + "\n", encoding="utf-8"
        )
        items.append({"case": case.name, "rubric": bool(case.rubric)})
    return items


def judge_inputs(cases: list[Case], outputs: Path, directory: Path) -> list[str]:
    """Pair each rubric with the answer it is about, for a judging agent to read.

    Only cases that carry a rubric *and* produced an answer. A case with no answer already failed
    for that reason, and sending it to a judge would spend a model call to be told so again.
    """
    directory.mkdir(parents=True, exist_ok=True)
    pending = []
    for case in cases:
        answer = outputs / f"{case.name}.json"
        if not case.rubric or not answer.is_file():
            continue
        (directory / f"{case.name}.json").write_text(
            json.dumps(
                {
                    "case": case.name,
                    "rubric": case.rubric,
                    "output": json.loads(answer.read_text(encoding="utf-8")),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        pending.append(case.name)
    return pending


def apply_judgement(result: dict[str, Any], judgement: Any) -> dict[str, Any]:
    """Fold one judge's verdict into a graded case.

    A verdict that cannot be read is not a pass. A judging agent that answers in a shape nobody
    expected has not judged anything, and treating that as approval is how a suite starts reporting
    green for the wrong reason.
    """
    if not isinstance(judgement, dict) or not isinstance(judgement.get("passed"), bool):
        return {
            **result,
            "rubric_pending": False,
            "passed": False,
            "rubric_verdict": {"passed": False, "reason": "the judge did not answer `passed` as a boolean"},
        }
    verdict = {"passed": judgement["passed"], "reason": str(judgement.get("reason") or "")}
    return {
        **result,
        "rubric_pending": False,
        "passed": result["deterministic_passed"] and verdict["passed"],
        "rubric_verdict": verdict,
    }


def summarize(results: list[dict[str, Any]], *, min_pass_rate: float | None = None) -> dict[str, Any]:
    """Roll case results into the answer a gate needs.

    Cases with an ungraded rubric are counted separately rather than as passes. A suite that reports
    100% while half of it was never judged is the reassuring number this contract exists to avoid.
    """
    total = len(results)
    # Pending first: a case awaiting judgement is neither passed nor failed, and counting it as
    # either is the lie. Everything else is decided, and a rubric a judge rejected is a failure just
    # as much as a missing field — which is why this asks `passed` rather than the deterministic half.
    pending = [r["case"] for r in results if r.get("rubric_pending")]
    failed = [r["case"] for r in results if r["case"] not in pending and not r["passed"]]
    decided = total - len(pending)
    rate = (decided - len(failed)) / decided if decided else 1.0
    summary = {
        "total": total,
        "passed": decided - len(failed),
        "failed": failed,
        "pending_rubric": pending,
        "pass_rate": round(rate, 4),
    }
    summary["ok"] = not failed if min_pass_rate is None else rate >= min_pass_rate
    return summary
