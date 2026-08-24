"""Reading the run ledger, and saying what changed.

The ledger answers one question the metrics cannot: **is this getting better or worse?** A dashboard
shows today. A prompt change three weeks ago that made one lens slower and another one wrong is only
visible by comparing two windows, and comparing two windows needs both of them to still exist.

Everything here is arithmetic over the lines a run wrote. Deliberately: a retro agent that had to
compute its own averages would produce different ones each time it ran, and a trend nobody can
reproduce is an anecdote.

Two rules the comparisons follow, both learned from what makes a metric useless.

**A window with too few runs reports no trend.** Two runs against two runs is noise, and noise
presented as a direction is worse than silence — somebody acts on it.

**A change is reported with its base.** "Failures up 12 points" from 0/3 to 1/2 is not the same
finding as from 40/300 to 156/300, and a number without its denominator cannot tell them apart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Below this, a window is a handful of runs and any rate computed from it swings on one outcome.
MIN_RUNS = 5


class LedgerError(RuntimeError):
    """The ledger could not be read, which is different from it being empty."""


def read_ledger(directory: Path) -> list[dict[str, Any]]:
    """Every record under a directory of month files, oldest first.

    A malformed line is skipped rather than fatal: the ledger is append-only and shared, and one
    bad write should not make the history unreadable forever.
    """
    records: list[dict[str, Any]] = []
    for path in sorted(directory.rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("finished"):
                records.append(record)
    return sorted(records, key=lambda r: str(r.get("finished", "")))


def split_windows(records: list[dict[str, Any]], *, at: str) -> tuple[list[Any], list[Any]]:
    """Everything before a timestamp, and everything after. The comparison is between the two."""
    before = [r for r in records if str(r.get("finished", "")) < at]
    after = [r for r in records if str(r.get("finished", "")) >= at]
    return before, after


@dataclass
class Stat:
    """One measurement over one window, carrying what it was computed from."""

    runs: int = 0
    failures: int = 0
    seconds: float = 0.0
    cost: float = 0.0
    # `None`, not zero. The ledger records what a *run* spent and does not attribute it to the
    # agents inside it — gh-aw's usage artifacts carry no name this could be joined on. So a
    # per-agent credit figure is unmeasured rather than nil, and a delta of 0.0 would say
    # "unchanged" about a number nothing ever measured.
    credits: float | None = None

    @property
    def failure_rate(self) -> float | None:
        return round(self.failures / self.runs, 4) if self.runs else None

    @property
    def mean_seconds(self) -> float | None:
        return round(self.seconds / self.runs, 2) if self.runs else None

    @property
    def mean_credits(self) -> float | None:
        if self.credits is None or not self.runs:
            return None
        return round(self.credits / self.runs, 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "failures": self.failures,
            "failure_rate": self.failure_rate,
            "mean_seconds": self.mean_seconds,
            **({"mean_credits": self.mean_credits} if self.mean_credits is not None else {}),
            "cost": round(self.cost, 4),
        }


def by_agent(records: list[dict[str, Any]]) -> dict[str, Stat]:
    """Per agent, because "which lens is failing" is the question a retro asks first."""
    stats: dict[str, Stat] = {}
    for record in records:
        for name, entry in (record.get("agents") or {}).items():
            stat = stats.setdefault(name, Stat())
            stat.runs += 1
            stat.failures += 1 if entry.get("outcome") == "failure" else 0
            stat.seconds += float(entry.get("seconds") or 0)
    return stats


def by_workflow(records: list[dict[str, Any]]) -> dict[str, Stat]:
    stats: dict[str, Stat] = {}
    for record in records:
        stat = stats.setdefault(str(record.get("workflow") or "(unnamed)"), Stat())
        stat.runs += 1
        stat.failures += 1 if record.get("failed") else 0
        stat.seconds += float(record.get("wall_seconds") or 0)
        stat.credits = (stat.credits or 0.0) + float(record.get("credits") or 0)
        stat.cost += float(record.get("cost_usd") or 0)
    return stats


def compare(
    before: dict[str, Stat], after: dict[str, Stat], *, min_runs: int = MIN_RUNS
) -> list[dict[str, Any]]:
    """What moved between two windows, for the things both windows saw enough of.

    A subject present in only one window is reported as new or gone rather than as a change: there
    is no baseline to have moved from, and inventing one is how a first run becomes a regression.
    """
    moved: list[dict[str, Any]] = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        entry: dict[str, Any] = {
            "name": name,
            "before": old.as_dict() if old else None,
            "after": new.as_dict() if new else None,
        }
        if old is None:
            entry["change"] = "new"
        elif new is None:
            entry["change"] = "gone"
        elif old.runs < min_runs or new.runs < min_runs:
            # Not "no change" — not enough to say. Reported so a reader knows why it is silent.
            entry["change"] = "too few runs"
        else:
            entry["change"] = "compared"
            # Only what both windows actually measured. An absent key reads as unmeasured; a
            # present zero reads as unchanged, and they are different findings.
            deltas = {
                "failure_rate": _delta(old.failure_rate, new.failure_rate),
                "mean_seconds": _delta(old.mean_seconds, new.mean_seconds),
                "mean_credits": _delta(old.mean_credits, new.mean_credits),
            }
            entry["deltas"] = {key: value for key, value in deltas.items() if value is not None}
        moved.append(entry)
    return moved


def _delta(old: float | None, new: float | None) -> float | None:
    if old is None or new is None:
        return None
    return round(new - old, 4)


@dataclass
class Outlier:
    run_id: str
    workflow: str
    run_url: str
    credits: float
    times_median: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "run_url": self.run_url,
            "credits": self.credits,
            "times_median": self.times_median,
        }


def outliers(records: list[dict[str, Any]], *, factor: float = 5.0) -> list[Outlier]:
    """Runs that cost far more than the median of their own workflow.

    Against the median rather than the mean, because one runaway run drags a mean far enough to hide
    the next one. And per workflow, because a review costing ten times a triage is not an anomaly —
    it is two different pipelines.

    A cost anomaly is rarely about money. It is an agent in a retry loop, a prompt that grew a tool
    call, or a context filling with something irrelevant.
    """
    found: list[Outlier] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(str(record.get("workflow") or ""), []).append(record)

    for workflow, group in sorted(grouped.items()):
        credits = sorted(float(r.get("credits") or 0) for r in group)
        if len(credits) < MIN_RUNS:
            continue
        median = credits[len(credits) // 2]
        if median <= 0:
            continue
        for record in group:
            spent = float(record.get("credits") or 0)
            if spent >= median * factor:
                found.append(
                    Outlier(
                        run_id=str(record.get("run_id", "")),
                        workflow=workflow,
                        run_url=str(record.get("run_url", "")),
                        credits=round(spent, 2),
                        times_median=round(spent / median, 1),
                    )
                )
    return sorted(found, key=lambda o: o.times_median, reverse=True)


@dataclass
class Report:
    records: list[dict[str, Any]] = field(default_factory=list)
    since: str = ""

    def build(self, *, min_runs: int = MIN_RUNS) -> dict[str, Any]:
        before, after = split_windows(self.records, at=self.since) if self.since else ([], self.records)
        return {
            "window": {
                "since": self.since,
                "runs": len(after),
                "baseline_runs": len(before),
                # Said out loud: a comparison with no baseline is a snapshot, and a reader who
                # thought they were looking at a trend would draw a conclusion from one window.
                "compared": bool(before),
            },
            "totals": {
                "runs": len(after),
                "credits": round(sum(float(r.get("credits") or 0) for r in after), 2),
                "cost_usd": round(sum(float(r.get("cost_usd") or 0) for r in after), 4),
                "failed_runs": sum(1 for r in after if r.get("failed")),
                "reruns": sum(1 for r in after if int(r.get("attempt") or 1) > 1),
            },
            "agents": compare(by_agent(before), by_agent(after), min_runs=min_runs),
            "workflows": compare(by_workflow(before), by_workflow(after), min_runs=min_runs),
            "outliers": [o.as_dict() for o in outliers(after)],
        }


def materialize_branch(branch: str, *, path: str = "history", depth: int = 50) -> Path:
    """Put the ledger branch's files somewhere readable, from inside an ordinary checkout.

    Not a clone of the working directory, which is the obvious thing and does not work: the checkout
    a workflow runs in has fetched only the ref that triggered it, so the ledger branch is not in it
    and cloning `.` fails with "remote branch not found". Fetching from `origin` reuses the
    credentials the checkout already configured, and a detached worktree reads the files without
    disturbing the tree the rest of the pipeline is standing in.
    """
    import subprocess
    import tempfile

    fetch = subprocess.run(
        ["git", "fetch", "-q", f"--depth={depth}", "origin", branch],
        capture_output=True,
        text=True,
        check=False,
    )
    if fetch.returncode != 0:
        raise LedgerError(f"could not fetch {branch!r}: {fetch.stderr.strip()[:200]}")

    directory = Path(tempfile.mkdtemp()) / "ledger"
    added = subprocess.run(
        ["git", "worktree", "add", "-q", "--detach", str(directory), "FETCH_HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if added.returncode != 0:
        raise LedgerError(f"could not read {branch!r}: {added.stderr.strip()[:200]}")
    return directory / path
