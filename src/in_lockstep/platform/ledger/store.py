"""In-repo ledger store."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ...privileged import sink

# 3: `findings` became {count, items} instead of a bare count. Bumped rather than added beside,
# because a count stored next to the list it counts is two states that can disagree — and the
# whole argument for keeping the list is that a record saying "3" and nothing else is not evidence.
# Nothing read the field, so the migration cost was zero; doing it after adopters exist would not
# have been.
#
# 4: every record gains provenance — `ts` (wall-clock UTC), `head`/`branch`/`dirty`, `base` and
# `ci_actor` on CI, and `config` (which lockstep.py constrained the run: trusted ref or working
# tree). Additive, so a schema-3 reader still parses a schema-4 record; bumped anyway because
# "since when do records carry timestamps" deserves one answer, not a per-field dig through
# history. `summarize`/`compare` are unchanged: none of the new fields is a number to aggregate.
#
# 5: a workflow record's `status` is always a member of the status set, derived from its steps
# when the workflow returned no `Outcome` of its own, and `steps` carries each step's verb,
# status, reason and findings. Schema-4 records from a workflow that returned a dict say
# `"completed"`, which no reader can count; `report` names how many of those it holds rather than
# folding them into "not failed", which is how eleven red selfchecks read as 0% failed (#166).
SCHEMA = 5
EPOCH = "in-process"
LEGACY_EPOCH = "ghaw"

# Below this, a window is a coincidence rather than a trend.
MIN_RUNS = 5


class LedgerError(RuntimeError):
    """Something the reader refuses to do, rather than doing badly."""


class Unsupported(LedgerError):
    """A store was asked for a capability its scope cannot provide."""


class LedgerScope:
    LOCAL = "local"
    SHARED = "shared"


def current_epoch() -> str:
    return EPOCH


@dataclass
class InRepoLedger:
    """One JSON file per run under `.lockstep/ledger/`."""

    root: Path = field(default_factory=lambda: Path(".lockstep/ledger"))
    scope: str = LedgerScope.LOCAL

    def path_for(self, run_id: str) -> Path:
        # Sanitised, because a run id is a path component and run ids are partly caller-supplied —
        # a triage of GitLab's `group/project#42` or a cross-repo `owner/repo#42` would otherwise
        # write to a nested `group/project#42.json` that the non-recursive read glob never finds,
        # so the record would be lost while the write reported success. `GitLedger` already does
        # this; the two stores must agree, or a record readable from one is invisible from the
        # other. `run` is the fallback for a name that sanitises to nothing.
        safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in run_id).strip("-.") or "run"
        return self.root / f"{safe}.json"

    def location(self, run_id: str) -> str:
        """Where a record went, in words a person can act on."""
        return str(self.path_for(run_id))

    async def append(self, run_id: str, record: dict[str, object]) -> None:
        stamped = {"schema": SCHEMA, "epoch": EPOCH, "run_id": run_id, **record}
        sink.write_json(self.path_for(run_id), stamped)

    async def read(self, run_id: str) -> dict[str, object] | None:
        path = self.path_for(run_id)
        if not path.exists():
            return None
        return json.loads(path.read_text())  # type: ignore[no-any-return]

    def records(self) -> list[dict[str, object]]:
        """Every record in the store, oldest file name first — the same read `GitLedger.records`
        provides, so `report` can ask either store the same question."""
        return read_ledger(self.root)

    async def compare_and_set(self, key: str, expected: str | None, new: str) -> bool:
        """Declared from day one; refused by this store, on purpose.

        A local-filesystem compare-and-set is implementable and semantically empty across CI
        runners, each of which has its own working directory. Park claims and fan-out barriers
        need a store more than one machine can see, so this refuses rather than appearing to
        provide something it cannot.
        """
        raise Unsupported(
            "the in-repo ledger is LOCAL-scoped and cannot provide compare-and-set across "
            "machines. Human boundaries and human fan-out branches require a SHARED store."
        )


#: Every status a record can honestly carry. Duplicated in `metrics.py`, which is a leaf and may
#: import nothing of ours; the two must agree.
VERDICTS = ("succeeded", "failed", "errored", "blocked")


@dataclass
class Stat:
    """Absent is not zero. `None` means unmeasured, `0` means measured as none."""

    runs: int = 0
    failures: int = 0
    #: Runs whose status is not a verdict (schema-4 workflow records say `"completed"`). They are
    #: in `runs` and out of the failure rate, because a record with no verdict is not evidence of
    #: success and folding it into "not failed" is how #166 read as 0% failed.
    unclassified: int = 0
    tokens: int | None = None
    cost_usd: float | None = None
    seconds: float | None = None

    @property
    def judged(self) -> int:
        return self.runs - self.unclassified

    @property
    def failure_rate(self) -> float | None:
        return self.failures / self.judged if self.judged else None

    @property
    def mean_cost(self) -> float | None:
        return self.cost_usd / self.runs if self.runs and self.cost_usd is not None else None


def read_ledger(directory: str | Path, *, epoch: str | None = EPOCH) -> list[dict[str, object]]:
    """Read one epoch. A malformed line is skipped; a foreign epoch is filtered, not merged."""
    path = Path(directory)
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    for file in sorted(path.glob("*.json")):
        try:
            record = json.loads(file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(record, dict):
            continue
        record.setdefault("epoch", LEGACY_EPOCH)
        if epoch is None or record.get("epoch") == epoch:
            records.append(record)
    return records


def _accumulate(stat: Stat, record: dict[str, object]) -> None:
    stat.runs += 1
    if record.get("status") in ("failed", "errored"):
        stat.failures += 1
    elif record.get("status") not in VERDICTS:
        stat.unclassified += 1
    # Accumulate ONLY when the key is present and numeric. Coercing an absent key to zero is what
    # produced a clean -100% reduction that nothing earned.
    for key, attr in (("tokens", "tokens"), ("cost_usd", "cost_usd"), ("wall_seconds", "seconds")):
        value = record.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            current = getattr(stat, attr)
            setattr(stat, attr, (current or 0) + value)


def spent_in_window(
    records: list[dict[str, object]], *, now: object, window_seconds: float = 86_400.0
) -> float:
    """What the recorded runs inside the rolling window cost, in USD.

    Reads the `ts` every schema-4 record carries. A record with no timestamp cannot be placed in
    a window and does not count — which is the honest reading for pre-provenance records (they
    predate the field, so they predate today), and follows the ledger's rule that a measurement
    nobody took is not a measurement of nothing. `now` is passed in rather than read here, so the
    window is testable and the caller owns the clock.
    """
    from datetime import datetime, timedelta

    assert isinstance(now, datetime)
    floor = now - timedelta(seconds=window_seconds)
    total = 0.0
    for record in records:
        raw = record.get("ts")
        if not isinstance(raw, str):
            continue
        try:
            ts = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if ts.tzinfo is None or ts < floor:
            continue
        cost = record.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            total += float(cost)
    return total


def summarize(records: list[dict[str, object]], *, by: str = "kind") -> dict[str, Stat]:
    stats: dict[str, Stat] = {}
    for record in records:
        key = str(record.get(by, "(unknown)"))
        stat = stats.setdefault(key, Stat())
        _accumulate(stat, record)
    return stats


def compare(
    before: list[dict[str, object]],
    after: list[dict[str, object]],
    *,
    min_runs: int = MIN_RUNS,
) -> list[dict[str, object]]:
    """Compare two windows, refusing to mix epochs.

    Refusing is the whole point. Averaging a credits-era record with a tokens-era one reports a
    direction that is a schema artefact, and the reader has no way to tell it apart from a real
    one.
    """
    epochs = {str(r.get("epoch", LEGACY_EPOCH)) for r in (*before, *after)}
    if len(epochs) > 1:
        raise LedgerError(
            f"refusing to compare across epochs {sorted(epochs)}: the unit of measurement "
            "changed between them, so any delta would be an artefact of the schema rather than "
            "a change in behaviour."
        )

    left = summarize(before)
    right = summarize(after)
    out: list[dict[str, object]] = []
    for key in sorted(set(left) | set(right)):
        a, b = left.get(key), right.get(key)
        if a is None or b is None or a.runs < min_runs or b.runs < min_runs:
            out.append(
                {
                    "key": key,
                    "verdict": "too few runs",
                    "before_runs": a.runs if a else 0,
                    "after_runs": b.runs if b else 0,
                }
            )
            continue
        out.append(
            {
                "key": key,
                "verdict": "measured",
                "failure_rate_before": a.failure_rate,
                "failure_rate_after": b.failure_rate,
                "mean_cost_before": a.mean_cost,
                "mean_cost_after": b.mean_cost,
            }
        )
    return out
