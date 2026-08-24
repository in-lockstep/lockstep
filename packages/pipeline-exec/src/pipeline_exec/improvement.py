"""Did a change to a prompt actually make the agent better?

A retro agent reading run history can say *what to try*. It cannot say whether the attempt worked —
it is the same kind of thing as the agent being changed, judging the change by reading it. The eval
suite can say, and this is what turns a suite that reports a number into one that reports a
direction.

The whole difficulty is that agents are non-deterministic. Run the identical suite against the
identical prompt twice and the scores differ. So a before-and-after comparison, done naively,
reports improvements and regressions that are nothing but sampling — and a gate built on that is
worse than no gate, because it blocks good changes and waves through bad ones with equal confidence.

**A comparison that does not know its own noise floor is an opinion with arithmetic on it.**

So this measures the noise before it reports a direction, and it gets both for free:

- **The baseline** is what the previous prompt scored. The default branch has already run it, so
  nothing here pays to re-run an old prompt — it looks the result up.
- **The noise floor** is the spread across runs *of that same prompt*. Those runs differ by nothing
  except sampling, which makes their variation the definition of what a meaningless delta looks
  like. A repository that has run its suite on main five times has measured its own noise five
  times without anybody deciding to.

A delta smaller than the noise is reported as `within noise`, not as an improvement. A baseline with
only one run is reported as having no measured noise floor, not as a clean comparison.

The per-case half matters more than the aggregate, and for the same reason. A case that passed every
baseline run and fails now is a real regression. A case that passed three baseline runs out of five
was never evidence of anything, and its flip today is not either — it is a flaky case, which is a
defect in the case rather than a finding about the agent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Below this many baseline runs of one prompt, there is no spread to measure and no honest way to
# separate a change from sampling. Two runs give one difference, which is a sample size of one.
MIN_BASELINE_RUNS = 3

# What a delta has to exceed on top of the observed noise before it is called a direction. The noise
# floor is the largest difference seen between two runs that differed by nothing; landing exactly on
# it is not clearing it.
MARGIN = 1e-9


def fingerprint(path: Path) -> str:
    """What the agent *was*, as one value.

    The compiled agent file is the whole prompt: its body, every guardrail, skill and context
    inlined or imported by content hash, the model, the budget, the turn cap. Hashing it means two
    runs share a fingerprint exactly when nothing that could move the agent's behaviour differs —
    which is the condition under which their difference is sampling and nothing else.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.is_file() else ""


def eval_record(report: dict[str, Any], *, prompt: str, identity: dict[str, Any]) -> dict[str, Any]:
    """One eval suite run, as the line that outlives it."""
    summary = report.get("summary") or {}
    return {
        "kind": "eval",
        "agent": str(report.get("agent") or ""),
        "prompt": prompt,
        "run_id": identity.get("run_id", ""),
        "run_url": identity.get("run_url", ""),
        "ref": identity.get("ref", ""),
        "sha": identity.get("sha", ""),
        "finished": identity.get("finished", ""),
        "pass_rate": summary.get("pass_rate"),
        "mean_score": summary.get("mean_score"),
        "decided": summary.get("total", 0) - len(summary.get("pending_rubric") or []),
        # Per case, because the aggregate hides the thing worth acting on: which case broke.
        "cases": {
            case["case"]: {"passed": bool(case.get("passed")), "score": case.get("score")}
            for case in (report.get("cases") or [])
        },
    }


def read_eval_records(directory: Path, *, agent: str) -> list[dict[str, Any]]:
    """Every recorded eval run for one agent, oldest first."""
    from .history import read_ledger

    return [
        record
        for record in read_ledger(directory)
        if record.get("kind") == "eval" and record.get("agent") == agent
    ]


def baseline_runs(records: list[dict[str, Any]], *, candidate_prompt: str) -> list[dict[str, Any]]:
    """The runs of the prompt this one replaces.

    The most recent fingerprint that is not the candidate's — not "everything before now", which
    would mix several prompts together and call their differences noise.
    """
    others = [r for r in records if r.get("prompt") and r["prompt"] != candidate_prompt]
    if not others:
        return []
    latest = others[-1]["prompt"]
    return [r for r in others if r["prompt"] == latest]


def _spread(values: list[float]) -> float:
    return round(max(values) - min(values), 6) if len(values) > 1 else 0.0


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


@dataclass
class Metric:
    """One measure, its baseline, and what counts as noise in it."""

    name: str
    baseline: float | None = None
    candidate: float | None = None
    noise: float = 0.0
    measurable: bool = False

    @property
    def delta(self) -> float | None:
        if self.baseline is None or self.candidate is None:
            return None
        return round(self.candidate - self.baseline, 6)

    @property
    def verdict(self) -> str:
        delta = self.delta
        if delta is None:
            return "no baseline"
        if not self.measurable:
            # A number without a noise floor is still a number; it is just not a direction.
            return "no noise floor"
        if abs(delta) <= self.noise + MARGIN:
            return "within noise"
        return "improved" if delta > 0 else "regressed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.name,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "delta": self.delta,
            "noise": self.noise,
            "verdict": self.verdict,
        }


def _metric(name: str, runs: list[dict[str, Any]], candidate: Any, *, measurable: bool) -> Metric:
    values = [float(r[name]) for r in runs if isinstance(r.get(name), (int, float))]
    return Metric(
        name=name,
        baseline=_mean(values),
        candidate=float(candidate) if isinstance(candidate, (int, float)) else None,
        noise=_spread(values),
        measurable=measurable and len(values) >= MIN_BASELINE_RUNS,
    )


@dataclass
class CaseVerdict:
    case: str
    baseline_passes: int
    baseline_runs: int
    candidate_passed: bool
    verdict: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "case": self.case,
            "baseline": f"{self.baseline_passes}/{self.baseline_runs}",
            "candidate_passed": self.candidate_passed,
            "verdict": self.verdict,
        }


def case_verdicts(runs: list[dict[str, Any]], candidate: dict[str, Any]) -> list[CaseVerdict]:
    """Per case, and this is the half worth reading.

    A case that passed every run of the old prompt and fails now is a regression somebody can go and
    look at. A case that passed some of those runs and not others was never evidence — its flip is
    the case being flaky, which is a defect in the case rather than a finding about the agent, and
    saying so is how a suite stops accumulating gates that fire at random.
    """
    cases = (candidate.get("cases") or {}) if candidate else {}
    verdicts: list[CaseVerdict] = []
    for name in sorted(set(cases) | {c for r in runs for c in (r.get("cases") or {})}):
        seen = [r for r in runs if name in (r.get("cases") or {})]
        passes = sum(1 for r in seen if (r["cases"][name] or {}).get("passed"))
        now = bool((cases.get(name) or {}).get("passed"))

        if not seen:
            verdict = "new case"
        elif len(seen) < MIN_BASELINE_RUNS:
            verdict = "too few baseline runs"
        elif 0 < passes < len(seen):
            verdict = "flaky"
        elif name not in cases:
            verdict = "gone"
        elif passes == len(seen) and not now:
            verdict = "regressed"
        elif passes == 0 and now:
            verdict = "fixed"
        else:
            verdict = "unchanged"
        verdicts.append(CaseVerdict(name, passes, len(seen), now, verdict))
    return verdicts


@dataclass
class Comparison:
    agent: str
    candidate: dict[str, Any] = field(default_factory=dict)
    runs: list[dict[str, Any]] = field(default_factory=list)

    def build(self) -> dict[str, Any]:
        measurable = len(self.runs) >= MIN_BASELINE_RUNS
        metrics = [
            _metric("pass_rate", self.runs, self.candidate.get("pass_rate"), measurable=measurable),
            _metric("mean_score", self.runs, self.candidate.get("mean_score"), measurable=measurable),
        ]
        cases = case_verdicts(self.runs, self.candidate)
        regressed = [c.case for c in cases if c.verdict == "regressed"]
        fixed = [c.case for c in cases if c.verdict == "fixed"]
        flaky = [c.case for c in cases if c.verdict == "flaky"]

        return {
            "agent": self.agent,
            "baseline": {
                "prompt": self.runs[-1]["prompt"] if self.runs else "",
                "runs": len(self.runs),
                # Said plainly, because every verdict below depends on it and a reader skimming for
                # a green tick will not otherwise notice that nothing was measured.
                "noise_measured": measurable,
            },
            "candidate_prompt": self.candidate.get("prompt", ""),
            "metrics": [metric.as_dict() for metric in metrics],
            "cases": [case.as_dict() for case in cases],
            "regressed": regressed,
            "fixed": fixed,
            "flaky": flaky,
            "verdict": self._verdict(metrics, regressed, fixed, measurable),
        }

    def _verdict(
        self, metrics: list[Metric], regressed: list[str], fixed: list[str], measurable: bool
    ) -> str:
        if not self.runs:
            return "no baseline"
        # A case that was solid and broke outranks any aggregate. An average can absorb one case
        # failing and still tick upward, and the case is the thing somebody has to go and fix.
        if regressed:
            return "regressed"
        if not measurable:
            return "no noise floor"
        directions = {metric.verdict for metric in metrics}
        if "regressed" in directions:
            return "regressed"
        if "improved" in directions or fixed:
            return "improved"
        return "within noise"


def render(comparison: dict[str, Any]) -> str:
    """The comparison, for the pull request comment where somebody will decide on it."""
    baseline = comparison["baseline"]
    lines = [f"### `{comparison['agent']}` — {comparison['verdict']}", ""]

    if comparison["verdict"] == "no baseline":
        lines += [
            "No previous run of this suite was recorded, so there is nothing to compare against.",
            "This run becomes the baseline for the next change.",
        ]
        return "\n".join(lines) + "\n"

    if not baseline["noise_measured"]:
        lines += [
            f"**The noise floor was not measured.** Only {baseline['runs']} baseline run(s) of the "
            f"previous prompt were recorded, and at least {MIN_BASELINE_RUNS} are needed before a "
            "difference can be told apart from sampling. The numbers below are real; the direction "
            "is not established.",
            "",
        ]

    lines += ["| Metric | Baseline | Now | Delta | Noise | |", "|---|---|---|---|---|---|"]
    for metric in comparison["metrics"]:
        if metric["baseline"] is None and metric["candidate"] is None:
            continue
        lines.append(
            f"| {metric['metric']} | {_num(metric['baseline'])} | {_num(metric['candidate'])} "
            f"| {_num(metric['delta'])} | ±{_num(metric['noise'])} | {metric['verdict']} |"
        )

    if comparison["regressed"]:
        lines += [
            "",
            "**Cases that passed every baseline run and fail now:** "
            + ", ".join(f"`{name}`" for name in comparison["regressed"]),
        ]
    if comparison["fixed"]:
        lines += [
            "",
            "**Cases that failed every baseline run and pass now:** "
            + ", ".join(f"`{name}`" for name in comparison["fixed"]),
        ]
    if comparison["flaky"]:
        lines += [
            "",
            "**Flaky, so they decide nothing:** "
            + ", ".join(f"`{name}`" for name in comparison["flaky"])
            + ". These passed some baseline runs and not others with the prompt unchanged, so a "
            "flip today says nothing about this change. Worth fixing as cases.",
        ]
    return "\n".join(lines) + "\n"


def _num(value: Any) -> str:
    if value is None:
        return "—"
    return f"{value:+.4g}" if isinstance(value, float) and value < 0 else f"{value:.4g}"


def load(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))
