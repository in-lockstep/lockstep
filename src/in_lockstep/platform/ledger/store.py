"""In-repo ledger store."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = 2
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
    """One JSON file per run under `.in-lockstep/ledger/`."""

    root: Path = field(default_factory=lambda: Path(".in-lockstep/ledger"))
    scope: str = LedgerScope.LOCAL

    def path_for(self, run_id: str) -> Path:
        return self.root / f"{run_id}.json"

    async def append(self, run_id: str, record: dict[str, object]) -> None:
        stamped = {"schema": SCHEMA, "epoch": EPOCH, "run_id": run_id, **record}
        self.root.mkdir(parents=True, exist_ok=True)
        self.path_for(run_id).write_text(json.dumps(stamped, indent=2, sort_keys=True) + "\n")

    async def read(self, run_id: str) -> dict[str, object] | None:
        path = self.path_for(run_id)
        if not path.exists():
            return None
        return json.loads(path.read_text())  # type: ignore[no-any-return]

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


@dataclass
class Stat:
    """Absent is not zero. `None` means unmeasured, `0` means measured as none."""

    runs: int = 0
    failures: int = 0
    tokens: int | None = None
    cost_usd: float | None = None
    seconds: float | None = None

    @property
    def failure_rate(self) -> float | None:
        return self.failures / self.runs if self.runs else None

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
    # Accumulate ONLY when the key is present and numeric. Coercing an absent key to zero is what
    # produced a clean -100% reduction that nothing earned.
    for key, attr in (("tokens", "tokens"), ("cost_usd", "cost_usd"), ("wall_seconds", "seconds")):
        value = record.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            current = getattr(stat, attr)
            setattr(stat, attr, (current or 0) + value)


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
