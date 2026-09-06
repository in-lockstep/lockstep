"""Bringing home the records a read-only job could only bundle.

Every pull request here pays for a review whose job holds the provider credential and
`contents: read`, so its record cannot be pushed from where it was made. It leaves as a git bundle
inside a workflow artifact, on a 30-day clock — and for two days of pull requests that is where
every one of them stayed, recorded as O4 asks and then discarded one step further down the pipe,
while the branch `report` reads held eight paid runs (#294).

Two things close that. A privileged job absorbs the bundle the moment the review finishes, and a
scheduled sweep absorbs whatever that job missed — a run cancelled by the next push, a runner that
timed out, a push the host refused. This module is the sweep's deterministic half: which artifacts
are still outstanding, and taking each one in exactly once. The host is asked through two methods
a forge adapter may offer (`run_artifacts`, `download_artifact`) and the local git ledger does the
absorbing; nothing here decides anything a model could be asked.

"Once" is decided two ways, because one is not enough. A record carries `ci_run`, the host's id
for the run that wrote it, and an artifact carries the same id — so an artifact whose run some
record on the branch already names has been absorbed, and `report --scm` can say so without
downloading anything. But a bundle from a run that recorded nothing (a review skipped on a fork)
or from before records carried the id would read as outstanding forever, so the absorb commit
names the run as well, and the sweep reads those names back. An artifact the host has already
expired is counted, not skipped: those records are gone, and a count that left them out would be
the branch presenting itself as the whole record.
"""

from __future__ import annotations

import tempfile
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .history import GitLedger, HistoryError

#: The artifact a review-on-pull-request trampoline uploads its evidence under, in this repository
#: and in the scaffold `init` writes. One name, so the sweep and `report --scm` look where the
#: trampoline puts it.
RUN_ARTIFACT = "lockstep-run"

#: The file inside it that `history --bundle` wrote.
BUNDLE_NAME = "history.bundle"


@dataclass(frozen=True)
class RunArtifact:
    """One uploaded artifact as the host lists it: which run made it, and whether the bytes are
    still there."""

    id: int
    run_id: str
    expired: bool = False
    created_at: str = ""


@dataclass(frozen=True)
class Outstanding:
    """The artifacts the branch has not absorbed, split by whether they still can be."""

    pending: tuple[RunArtifact, ...]
    expired: tuple[RunArtifact, ...]
    absorbed: int


def outstanding(
    artifacts: Iterable[RunArtifact],
    records: Iterable[dict[str, Any]],
    *,
    absorbed: Collection[str] = (),
) -> Outstanding:
    """Which artifacts no record and no absorb commit accounts for.

    Decided by the run id both sides carry, and by nothing else: a bundle's contents are not
    downloaded to answer this, which is what lets `report` print the number on every run.
    """
    seen = {str(r.get("ci_run") or "") for r in records} | set(absorbed)
    seen.discard("")
    pending: list[RunArtifact] = []
    expired: list[RunArtifact] = []
    done = 0
    for artifact in artifacts:
        if artifact.run_id and artifact.run_id in seen:
            done += 1
        elif artifact.expired:
            expired.append(artifact)
        else:
            pending.append(artifact)
    return Outstanding(pending=tuple(pending), expired=tuple(expired), absorbed=done)


@dataclass(frozen=True)
class Absorbed:
    """What one sweep did, artifact by artifact, so the caller can print it and push once."""

    taken: tuple[RunArtifact, ...]
    empty: tuple[RunArtifact, ...]
    expired: tuple[RunArtifact, ...]
    failed: tuple[tuple[RunArtifact, str], ...]


def absorb_outstanding(ledger: GitLedger, host: Any, name: str = RUN_ARTIFACT) -> Absorbed:
    """Take in every outstanding bundle the host still holds under `name`, once each.

    The host is asked with `getattr`, the way `report --scm` asks for `delivery_rows`: listing
    and fetching artifacts is a forge's capability and the local git host has none, so putting the
    two methods on the `Scm` port would oblige every host to refuse them. A host without them is
    refused here by name rather than read as "nothing outstanding".

    One artifact's failure does not stop the sweep: what could be absorbed is, and what could not
    is returned with its reason, so a single corrupt bundle cannot hold forty behind it. Nothing
    is pushed here; the command that called this pushes once when it is told to.
    """
    listing = getattr(host, "run_artifacts", None)
    fetch = getattr(host, "download_artifact", None)
    if listing is None or fetch is None:
        raise HistoryError(
            f"{type(host).__name__} cannot list and download run artifacts, so nothing can be "
            f"absorbed from it; absorb a bundle you fetched yourself with `history --from-bundle`"
        )
    found = outstanding(listing(name), ledger.records(), absorbed=ledger.absorbed_runs())
    taken: list[RunArtifact] = []
    empty: list[RunArtifact] = []
    failed: list[tuple[RunArtifact, str]] = []
    for artifact in found.pending:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                into = Path(fetch(artifact.id, Path(tmp)))
                bundle = into / BUNDLE_NAME
                if not bundle.is_file():
                    # The review was skipped or recorded nothing, so the job bundled nothing. Not
                    # a failure, and not one to download again tomorrow: the ledger is told the
                    # run was looked at, which is what makes "once" true for this case too.
                    ledger.note_absorbed(artifact.run_id)
                    empty.append(artifact)
                    continue
                ledger.absorb(bundle, run_id=artifact.run_id)
            except (HistoryError, OSError, RuntimeError, ValueError) as e:
                failed.append((artifact, str(e)))
                continue
        taken.append(artifact)
    return Absorbed(taken=tuple(taken), empty=tuple(empty), expired=found.expired, failed=tuple(failed))
