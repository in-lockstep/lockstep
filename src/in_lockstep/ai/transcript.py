"""Per-turn transcripts, so a failed session leaves more than metadata behind.

The ledger records what a run cost and what it decided; it has never recorded what the model
actually said turn by turn — so debugging a failed AI session meant re-running it and hoping it
failed the same way. This is the missing artifact: every invocation the bootstrap-built invoker
makes appends one JSONL line holding the full message history it ended with, including tool
results, and how it ended.

Written always, not only on failure, deliberately: the invoker cannot know whether the *session*
failed — a schema mismatch is discovered by the adapter after a perfectly successful invocation —
and a transcript that exists only when something upstream remembered to ask for one is the
metadata problem again. The cost is a small local file under `.lockstep/` (gitignored, like the
rest of the run's working state), and the payoff is that the evidence for the run that just
failed already exists.

Everything goes through `privileged.sink`, so a provider error quoting a credential or a tool
result that read one out of a fixture is masked before it lands on disk — a transcript is
precisely the file most likely to be pasted whole into an issue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..privileged import sink
from ..privileged.redact import Redact

#: One message's content, bounded. A transcript is for reading a failure, not for archiving a
#: vendored tree somebody read_file'd; the tail of a long tool result is where a traceback lives,
#: so the head is kept and the cut is named.
MAX_CONTENT_CHARS = 20_000


def _safe(run_id: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_." else "-" for c in run_id).strip("-.")
    return cleaned or "run"


@dataclass
class TranscriptWriter:
    """Appends one line per invocation to `.lockstep/transcripts/<run>.jsonl`.

    Append rather than overwrite, because one run is several invocations — a TDD strategy talks
    to the model once to write the test and again to implement — and the transcript should read
    as the session did: in order, all of it.
    """

    run_id: str
    root: Path = field(default_factory=lambda: Path(".lockstep/transcripts"))
    redact: Redact = field(default_factory=Redact)

    def path(self) -> Path:
        return self.root / f"{_safe(self.run_id)}.jsonl"

    def append(
        self,
        *,
        model: str,
        ended: str,
        messages: list[Any],
        system_chars: int = 0,
    ) -> None:
        """Record one invocation. Failure to write must not sink the run that is being recorded —
        the run's answer is worth more than its transcript — but it is said, not swallowed."""
        import json

        record = {
            "model": model,
            # "answered", "exhausted", or "provider_error" — how the loop ended, which is the
            # first thing a person debugging a failed session needs and the one thing the raw
            # messages cannot say.
            "ended": ended,
            "system_chars": system_chars,
            "messages": [self._message(m) for m in messages],
        }
        try:
            sink.append_text(
                self.path(), json.dumps(record, sort_keys=True, default=repr) + "\n", redact=self.redact
            )
        except OSError as e:
            print(f"transcript NOT WRITTEN: {e}")

    def _message(self, message: Any) -> dict[str, Any]:
        content = str(getattr(message, "content", ""))
        cut = len(content) > MAX_CONTENT_CHARS
        out: dict[str, Any] = {
            "role": str(getattr(message, "role", "")),
            "content": content[:MAX_CONTENT_CHARS],
        }
        if cut:
            out["truncated_chars"] = len(content) - MAX_CONTENT_CHARS
        calls = getattr(message, "tool_calls", None) or []
        if calls:
            out["tool_calls"] = [str(getattr(c, "name", "")) for c in calls]
        name = str(getattr(message, "tool_name", ""))
        if name:
            out["tool_name"] = name
        return out
