"""The verdict sidecar: what a judge decided, kept beside the case it decided about.

Appended through `sink`, never rewritten, and never into the case file itself. A promoted case is
publication -- the bytes a person read and merged -- and a run that rewrote it to add a verdict
would be a run editing evidence. The sidecar is the run's own record: one JSON line per verdict,
each carrying the replay key `Judge` reads it back by, so a rubric judged once over one answer is
not paid for again (`GATE-JUDGE-3`).

Here and not in `improver`, because writing is a privileged act and `improver` is a layer that
may read the corpus and may not reach a sink. The improver says WHERE (`sidecar_for`); this says
how.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from ..core.improve import Verdict
from ..privileged import sink


def append_verdicts(improver: Any, verdicts: Sequence[Verdict]) -> tuple[str, ...]:
    """Append each verdict to its case's sidecar. Returns the paths written, for the run to print.

    Only a verdict carrying its replay key is kept: one without a key cannot be replayed, and a
    sidecar row that could never be matched would be a line nobody reads back.
    """
    by_case: dict[str, list[Verdict]] = {}
    for verdict in verdicts:
        if verdict.rubric_sha256 and verdict.answer_sha256:
            by_case.setdefault(verdict.case, []).append(verdict)
    written: list[str] = []
    for case, rows in by_case.items():
        path = improver.sidecar_for(case)
        sink.append_text(path, "".join(json.dumps(v.as_record(), sort_keys=True) + "\n" for v in rows))
        written.append(str(path))
    return tuple(written)
