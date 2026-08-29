"""Step checkpointing, so machine failure does not lose a run.

Recovery covers *machine* failure only. A run never waits on a person: human waits end the run at
a boundary and a continuation resumes it as a fresh run. `--recover` restarts the same interrupted
run; that is a different thing from resuming after a human, and conflating them is how a library
turns into a durable-execution engine with determinism rules on user code.

Two details matter more than they look.

**Presence is not success.** The compiler-era resume primitive was "an output file exists", which
cannot distinguish a step that succeeded from a step that wrote half a file before the runner was
killed. A checkpoint records the outcome, so a partial write is not mistaken for a result.

**Writes are atomic.** A checkpoint written non-atomically is exactly the artefact that breaks
recovery, because the thing being recovered from is a process dying at an arbitrary moment. Write
to a temporary file and rename.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..core.outcome import Cost, Finding, Outcome, Severity, Status
from ..privileged import sink

DEFAULT_ROOT = Path(".in-lockstep/runs")


@dataclass
class Checkpoint:
    step_id: str
    status: str
    reason: str | None = None
    decided: bool = True
    value: object = None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    def as_outcome(self) -> Outcome[object]:
        return Outcome(
            status=Status(self.status),
            value=self.value,
            reason=self.reason,
            decided=self.decided,
            cost=Cost(
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                usd=self.cost_usd,
            ),
            findings=(
                Finding(
                    id="recover.replayed",
                    message=f"replayed from checkpoint {self.step_id}",
                    severity=Severity.NOTE,
                ),
            ),
        )

    @classmethod
    def of(cls, step_id: str, outcome: Outcome[object]) -> Checkpoint:
        return cls(
            step_id=step_id,
            status=outcome.status.value,
            reason=outcome.reason,
            decided=outcome.decided,
            value=_serializable(outcome.value),
            cost_usd=outcome.cost.usd,
            input_tokens=outcome.cost.input_tokens,
            output_tokens=outcome.cost.output_tokens,
        )


class StateStore:
    """Filesystem-backed checkpoints, one directory per run."""

    def __init__(self, root: Path | str = DEFAULT_ROOT) -> None:
        self.root = Path(root)

    def run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def save(self, run_id: str, checkpoint: Checkpoint) -> None:
        directory = self.run_dir(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{_safe(checkpoint.step_id)}.json"
        payload = json.dumps(checkpoint.__dict__, indent=2, sort_keys=True, default=repr) + "\n"
        # Atomic, because the failure being recovered from is a process dying mid-write.
        sink.write_text_atomic(target, payload)

    def load(self, run_id: str, step_id: str) -> Checkpoint | None:
        path = self.run_dir(run_id) / f"{_safe(step_id)}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            # A torn checkpoint is not a result. Re-running the step is correct; trusting a
            # half-written one is how recovery produces a wrong answer rather than a slow one.
            return None
        return Checkpoint(**data)

    def completed(self, run_id: str) -> list[str]:
        directory = self.run_dir(run_id)
        if not directory.exists():
            return []
        return sorted(p.stem for p in directory.glob("*.json"))

    # -- the core/ports.StepStore seam -------------------------------------------

    def save_step(self, run_id: str, step_id: str, outcome: object) -> None:
        if isinstance(outcome, Outcome):
            self.save(run_id, Checkpoint.of(step_id, outcome))

    def load_step(self, run_id: str, step_id: str) -> object | None:
        checkpoint = self.load(run_id, step_id)
        return checkpoint.as_outcome() if checkpoint is not None else None

    def clear(self, run_id: str) -> None:
        directory = self.run_dir(run_id)
        if not directory.exists():
            return
        for path in directory.glob("*.json"):
            path.unlink()


def _safe(step_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in step_id)


def _serializable(value: object) -> object:
    """Checkpoints round-trip through JSON, so a value that cannot is recorded as absent.

    Recorded as absent rather than as a repr: a repr that looks like a value is worse than a
    missing one, because a recovered run would carry it forward as if it were real.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_serializable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _serializable(v) for k, v in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        try:
            return asdict(value)  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return None
    return None
