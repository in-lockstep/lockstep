"""The run ledger.

Append-only, one record per run, in the repository — but on an ORPHAN BRANCH rather than in the
working tree. One record per run means appends never conflict; in-repo means the record is
versioned, diffable and owned by whoever owns the repository; orphan means it is none of those
things at the cost of appearing in a diff somebody is trying to read.

`GitLedger` is the default and `InRepoLedger` is the fallback for a directory that is not a git
repository. The working-tree store was what shipped first, and it lost every local run's record:
`.lockstep/` is gitignored, so records were written, never committed, and never seen again.

Two things here exist because of a specific failure.

**Epochs.** The unit of measurement changed when invocation moved in-process: cost used to be
credits multiplied by a rate, and is now tokens multiplied by a price. Same field name, different
quantity. A reader that averages across the boundary reports a change that nothing earned — and
the pre-pivot reader did exactly that, because it coerced an absent `credits` key to `0.0` and
then computed a delta against it. So records carry an epoch, and comparing across epochs raises
rather than averaging.

**Absent is not zero.** `None` means unmeasured and `0` means measured as none, and collapsing
them is how a fabricated improvement gets reported as fact.
"""

from pathlib import Path

from .history import DEFAULT_BRANCH, GitLedger, HistoryError
from .store import (
    InRepoLedger,
    LedgerError,
    LedgerScope,
    Stat,
    Unsupported,
    compare,
    current_epoch,
    read_ledger,
    spent_in_window,
    summarize,
)


def store_for(container: object = None, root: str = ".") -> object:
    """Where a run record goes — one decision, made once, shared by writer and reader.

    Order: a store the repository bound, then the orphan branch, then a file in the working tree.
    The last is a fallback and not a default: `.lockstep/` is gitignored, so a record written
    there in a git repository is written and then lost — it stays for directories that are not
    repositories at all, where the branch cannot exist. Lifted out of the CLI so the pre-run
    spend ceiling reads the same store every run writes; two answers to "which ledger" is how a
    ceiling ends up summing records nothing appends to.
    """
    import subprocess

    if container is not None:
        from ...core.ports import LedgerStore

        has = getattr(container, "has", None)
        if callable(has) and has(LedgerStore):
            return container.resolve(LedgerStore)  # type: ignore[attr-defined]
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
        return InRepoLedger()
    return GitLedger(root=Path(root)) if inside.stdout.strip() == "true" else InRepoLedger()


__all__ = [
    "DEFAULT_BRANCH",
    "GitLedger",
    "HistoryError",
    "InRepoLedger",
    "LedgerError",
    "LedgerScope",
    "Stat",
    "Unsupported",
    "compare",
    "current_epoch",
    "read_ledger",
    "spent_in_window",
    "store_for",
    "summarize",
]
