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

Two things the judged half needs that a boolean cannot carry.

**A rubric can ask for a score rather than a verdict.** Prompt work degrades in degrees: an agent
that used to name the exploit and now only notices the unvalidated input is worse, and a pass/fail
rubric reports both as green. A rubric that says what a 5 requires and what a 3 requires makes that
slide visible while every case is still passing.

**A case can carry a repository.** An agent asked to review code needs code to read, and `input`
alone gives it a JSON object. A case naming a fixture gets that tree materialized beside its input
and is told where it is — which is the difference between evaluating a reviewer and evaluating its
ability to reason about a patch fragment.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# What `expect` may contain. Declared rather than inferred, so an unknown key is a typo somebody
# hears about instead of an expectation that silently never ran.
DETERMINISTIC_KEYS = ("schema", "equals", "contains", "absent", "count")
EXPECT_KEYS = (*DETERMINISTIC_KEYS, "rubric")


class CaseError(ValueError):
    """A case that cannot be graded, which is a defect in the case rather than in the agent."""


# What a scored rubric may declare. Same argument as `EXPECT_KEYS`: an unknown key is a typo
# somebody hears about rather than a threshold that silently never applied.
RUBRIC_KEYS = ("criteria", "levels", "min")

# Where a case's fixture tree lives, as a sibling of the cases directory:
#
#     evals/<agent>/cases/path-traversal.json     "fixture": "path-traversal"
#     evals/<agent>/fixtures/path-traversal/      src/files.py, …
#
# A convention rather than a path in the case file, so a fixture cannot name somewhere else in the
# repository — or outside it.
FIXTURES_DIR = "fixtures"

# The key an expanded input gains, naming where the fixture was materialized. The agent contract is
# `input_path` in and `output_path` out, so the input is the only place a case can say "the code is
# over there".
REPO_KEY = "repo"


@dataclass(frozen=True)
class Rubric:
    """The judged half of a case, either as a verdict or as a score.

    `levels` empty means a binary rubric: the judge answers passed or not. Populated, it maps a
    score to what earns it, and the judge answers a number — which is what makes an agent sliding
    from "names the exploit" to "notices the input" visible while both still pass.
    """

    criteria: str
    levels: dict[int, str] = field(default_factory=dict)
    threshold: int = 0

    @property
    def scored(self) -> bool:
        return bool(self.levels)

    @property
    def scale(self) -> tuple[int, int]:
        return (min(self.levels), max(self.levels)) if self.levels else (0, 0)

    def as_payload(self) -> dict[str, Any]:
        """What a judging agent is told about this rubric.

        A scored rubric hands over the levels themselves. A judge told only "score this out of 5"
        is inventing the scale on every call, and two runs of the same suite would not be
        comparable — which is the entire point of scoring instead of deciding.
        """
        payload: dict[str, Any] = {"rubric": self.criteria, "scored": self.scored}
        if self.scored:
            low, high = self.scale
            payload["levels"] = {str(score): self.levels[score] for score in sorted(self.levels)}
            payload["scale"] = {"min": low, "max": high}
            payload["min_score"] = self.threshold
        return payload


def parse_rubric(raw: Any, *, name: str) -> Rubric | None:
    """Read the `rubric` expectation in either of its two forms."""
    if raw is None:
        return None
    if isinstance(raw, str):
        if not raw.strip():
            raise CaseError(f"{name}: `rubric` is empty, so it asks the judge nothing")
        return Rubric(criteria=raw)
    if not isinstance(raw, dict):
        raise CaseError(f"{name}: `rubric` is prose or an object with levels, not {type(raw).__name__}")

    unknown = sorted(set(raw) - set(RUBRIC_KEYS))
    if unknown:
        raise CaseError(
            f"{name}: unknown rubric key(s) {', '.join(unknown)} — known: {', '.join(RUBRIC_KEYS)}"
        )

    criteria = raw.get("criteria")
    if not isinstance(criteria, str) or not criteria.strip():
        raise CaseError(f"{name}: a rubric needs `criteria` — prose saying what a good answer does")

    levels_raw = raw.get("levels")
    if not isinstance(levels_raw, dict) or len(levels_raw) < 2:
        raise CaseError(
            f"{name}: `levels` maps at least two scores to what earns them. One level is not a "
            f"scale, and a rubric without levels is prose — write it as a string"
        )

    levels: dict[int, str] = {}
    for key, description in levels_raw.items():
        try:
            score = int(str(key))
        except ValueError:
            raise CaseError(f"{name}: level {key!r} is not a score") from None
        if not isinstance(description, str) or not description.strip():
            raise CaseError(f"{name}: level {score} says nothing about what earns it")
        levels[score] = description

    threshold = raw.get("min")
    if not isinstance(threshold, int) or isinstance(threshold, bool):
        raise CaseError(
            f"{name}: a scored rubric needs `min` — the score this case has to reach. Without one "
            f"the grader would be inventing the threshold it reports against"
        )
    low, high = min(levels), max(levels)
    if not low <= threshold <= high:
        raise CaseError(f"{name}: `min` of {threshold} is outside the scale ({low}-{high})")
    return Rubric(criteria=criteria, levels=levels, threshold=threshold)


def fixture_dir(name: str, cases: Path, *, case_name: str) -> Path:
    """Resolve a fixture name against the sibling `fixtures/` directory.

    A name rather than a path, and validated as one: a case that could name `../../..` would be a
    way to hand an agent the repository that is running the eval.
    """
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        raise CaseError(
            f"{case_name}: fixture {name!r} is a directory name under {FIXTURES_DIR}/, not a path"
        )
    directory = cases.parent / FIXTURES_DIR / name
    if not directory.is_dir():
        raise CaseError(f"{case_name}: no fixture at {directory}")
    if not any(path.is_file() for path in directory.rglob("*")):
        raise CaseError(f"{case_name}: fixture {name!r} has no files, so the agent is given nothing")
    return directory


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
    fixture: str = ""
    # Where the fixture tree was found on disk. Set when the case was loaded from a file, because
    # that is the only time there is a directory to resolve it against.
    fixture_source: Path | None = None

    @property
    def rubric(self) -> Rubric | None:
        return parse_rubric(self.expect.get("rubric"), name=self.name)

    @property
    def deterministic(self) -> dict[str, Any]:
        return {key: self.expect[key] for key in DETERMINISTIC_KEYS if key in self.expect}

    @classmethod
    def load(cls, path: Path) -> Case:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CaseError(f"{path.name}: not valid JSON ({exc.msg} at line {exc.lineno})") from None
        return cls.parse(raw, name=str(raw.get("case") or path.stem), cases=path.parent)

    @classmethod
    def parse(cls, raw: Any, *, name: str, cases: Path | None = None) -> Case:
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
                f"{name}: unknown expectation(s) {', '.join(unknown)} — known: {', '.join(EXPECT_KEYS)}"
            )
        if not expect:
            raise CaseError(
                f"{name}: `expect` asserts nothing, so this case passes for any output. "
                f"Add one of: {', '.join(EXPECT_KEYS)}"
            )

        fixture = str(raw.get("fixture") or "")
        source = fixture_dir(fixture, cases, case_name=name) if fixture and cases else None
        if fixture and not isinstance(raw["input"], dict):
            raise CaseError(
                f"{name}: a case with a fixture needs an object for `input` — there is nowhere to "
                f"record where the code was put"
            )
        if fixture and REPO_KEY in raw["input"]:
            raise CaseError(
                f"{name}: `input.{REPO_KEY}` is where the fixture path goes, so a case cannot set it as well"
            )

        # Read the rubric now rather than at grade time: a malformed rubric is a defect in the
        # case, and finding it after the agent has already run costs a model call to learn it.
        parse_rubric(expect.get("rubric"), name=name)

        return cls(
            name=name,
            input=raw["input"],
            expect=expect,
            context=raw.get("context") or {},
            fixture=fixture,
            fixture_source=source,
        )


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
        checks.append(Check("equals", field_name, matches, "" if matches else f"{actual!r} != {expected!r}"))

    for needle in _as_list(case.expect.get("contains")):
        found = needle.lower() in blob
        checks.append(Check("contains", needle, found, "" if found else "not found anywhere in the output"))

    for needle in _as_list(case.expect.get("absent")):
        found = needle.lower() in blob
        checks.append(Check("absent", needle, not found, "present in the output" if found else ""))

    for target, bounds in (case.expect.get("count") or {}).items():
        checks.append(_count_check(output, target, bounds))

    deterministic_passed = all(check.passed for check in checks)
    rubric = case.rubric
    result: dict[str, Any] = {
        "case": case.name,
        "checks": [check.as_dict() for check in checks],
        "deterministic_passed": deterministic_passed,
        # A rubric is not graded here. `passed` is therefore provisional whenever one exists, and
        # says so, rather than reporting a pass that only covers half the case.
        "rubric": rubric.criteria if rubric else "",
        "rubric_scored": bool(rubric and rubric.scored),
        "rubric_pending": bool(rubric),
        "passed": deterministic_passed and not rubric,
    }
    if rubric and rubric.scored:
        low, high = rubric.scale
        result["rubric_scale"] = {"min": low, "max": high}
        result["rubric_min_score"] = rubric.threshold
    return result


def unanswered(case: Case) -> dict[str, Any]:
    """The result for a case the agent never answered.

    A failure, not a skip: the agent was asked and did not answer, which is exactly the regression a
    suite is for. It goes through here rather than being assembled by the caller so that every case
    in a report has the same keys — a second place that spells out the shape is a second place for
    it to drift.
    """
    rubric = case.rubric
    result: dict[str, Any] = {
        "case": case.name,
        "checks": [{"check": "answered", "target": case.name, "passed": False, "detail": "no output"}],
        "deterministic_passed": False,
        "rubric": rubric.criteria if rubric else "",
        "rubric_scored": bool(rubric and rubric.scored),
        "rubric_pending": False,
        "passed": False,
    }
    if rubric and rubric.scored:
        low, high = rubric.scale
        result["rubric_scale"] = {"min": low, "max": high}
        result["rubric_min_score"] = rubric.threshold
    return result


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


def expand(cases: list[Case], directory: Path, *, repos: Path | None = None) -> list[dict[str, Any]]:
    """Write one input file per case and describe the items a fan-out will run over.

    The agent contract is `input_path` in, `output_path` out — the same one every other agent step
    uses. An eval is therefore not a special way of running an agent; it is the ordinary way, with
    the input coming from a case file instead of an earlier step.

    A case with a fixture gets its tree copied to its own directory under `repos` and the path
    written into the input it is handed. Its own directory, because two cases sharing one checkout
    would let the first case's run change what the second one sees.
    """
    directory.mkdir(parents=True, exist_ok=True)
    items = []
    for case in cases:
        payload = case.input
        if case.fixture:
            payload = {**case.input, REPO_KEY: str(_materialize(case, repos))}
        (directory / f"{case.name}.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        items.append({"case": case.name, "rubric": bool(case.rubric), "fixture": case.fixture})
    return items


def _materialize(case: Case, repos: Path | None) -> Path:
    """Lay a case's fixture tree down where the agent will run, from scratch each time.

    From scratch because a leftover file from an earlier run is a fixture nobody wrote, and an
    agent that answers differently because of one is reporting on the wrong repository.
    """
    if repos is None:
        raise CaseError(f"{case.name}: carries a fixture, but no directory was given to materialize it into")
    if case.fixture_source is None:
        raise CaseError(f"{case.name}: fixture {case.fixture!r} was never resolved to a directory")
    destination = repos / case.name
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(case.fixture_source, destination)
    return destination


def judge_inputs(cases: list[Case], outputs: Path, directory: Path) -> list[str]:
    """Pair each rubric with the answer it is about, for a judging agent to read.

    Only cases that carry a rubric *and* produced an answer. A case with no answer already failed
    for that reason, and sending it to a judge would spend a model call to be told so again.
    """
    directory.mkdir(parents=True, exist_ok=True)
    pending = []
    for case in cases:
        answer = outputs / f"{case.name}.json"
        rubric = case.rubric
        if not rubric or not answer.is_file():
            continue
        (directory / f"{case.name}.json").write_text(
            json.dumps(
                {
                    "case": case.name,
                    **rubric.as_payload(),
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
    green for the wrong reason. That applies to a scored rubric answered with a boolean as much as
    to no answer at all: the suite asked for a number and did not get one.
    """
    if result.get("rubric_scored"):
        return _apply_score(result, judgement)
    if not isinstance(judgement, dict) or not isinstance(judgement.get("passed"), bool):
        return _unreadable(result, "the judge did not answer `passed` as a boolean")
    verdict = {"passed": judgement["passed"], "reason": str(judgement.get("reason") or "")}
    return {
        **result,
        "rubric_pending": False,
        "passed": result["deterministic_passed"] and verdict["passed"],
        "rubric_verdict": verdict,
    }


def _apply_score(result: dict[str, Any], judgement: Any) -> dict[str, Any]:
    scale = result.get("rubric_scale") or {}
    low, high = int(scale.get("min", 0)), int(scale.get("max", 0))
    threshold = int(result.get("rubric_min_score", high))

    score = judgement.get("score") if isinstance(judgement, dict) else None
    # `bool` is an `int` in Python, and a judge answering `true` to a request for a score on a
    # 1-5 scale has not scored anything — it would silently become a 1.
    if isinstance(score, bool) or not isinstance(score, int):
        return _unreadable(result, f"the judge did not answer `score` as a whole number {low}-{high}")
    if not low <= score <= high:
        return _unreadable(result, f"the judge answered {score}, which is outside the scale {low}-{high}")

    verdict = {
        "score": score,
        "passed": score >= threshold,
        "reason": str(judgement.get("reason") or ""),
    }
    return {
        **result,
        "rubric_pending": False,
        # Recorded at the top level as well: the score is what a later run is compared against, and
        # a number buried in a verdict is a number nothing rolls up.
        "score": score,
        "passed": result["deterministic_passed"] and verdict["passed"],
        "rubric_verdict": verdict,
    }


def _unreadable(result: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        **result,
        "rubric_pending": False,
        "passed": False,
        "rubric_verdict": {"passed": False, "reason": reason},
    }


def summarize(
    results: list[dict[str, Any]],
    *,
    min_pass_rate: float | None = None,
    min_score: float | None = None,
) -> dict[str, Any]:
    """Roll case results into the answer a gate needs.

    Cases with an ungraded rubric are counted separately rather than as passes. A suite that reports
    100% while half of it was never judged is the reassuring number this contract exists to avoid.

    Scores are rolled up beside the passes rather than folded into them. An agent whose answers slid
    from 5s to 4s is worse and every case still passes; a summary that reported only `pass_rate`
    would say nothing had happened, which is the reason scores exist.
    """
    total = len(results)
    # Pending first: a case awaiting judgement is neither passed nor failed, and counting it as
    # either is the lie. Everything else is decided, and a rubric a judge rejected is a failure just
    # as much as a missing field — which is why this asks `passed` rather than the deterministic half.
    pending = [r["case"] for r in results if r.get("rubric_pending")]
    failed = [r["case"] for r in results if r["case"] not in pending and not r["passed"]]
    decided = total - len(pending)
    rate = (decided - len(failed)) / decided if decided else 1.0

    scores = {
        r["case"]: r["score"] for r in results if r["case"] not in pending and isinstance(r.get("score"), int)
    }
    mean = round(sum(scores.values()) / len(scores), 4) if scores else None

    summary: dict[str, Any] = {
        "total": total,
        "passed": decided - len(failed),
        "failed": failed,
        "pending_rubric": pending,
        "pass_rate": round(rate, 4),
        "scores": scores,
        "mean_score": mean,
        # The distribution, not just the average: four 5s and a 1 average the same as five 4.2s and
        # are a different agent.
        "score_counts": {
            str(value): sorted(scores.values()).count(value) for value in sorted(set(scores.values()))
        },
    }
    summary["ok"] = not failed if min_pass_rate is None else rate >= min_pass_rate
    # A mean below the floor fails a suite in which every case individually passed. That is the
    # regression a binary gate cannot see, so it is the one this exists to catch.
    if min_score is not None and mean is not None and mean < min_score:
        summary["ok"] = False
    return summary
