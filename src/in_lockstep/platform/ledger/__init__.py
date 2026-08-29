"""The run ledger.

Append-only, one file per run, in the repository. One file per run means appends never conflict;
in-repo means the record is versioned, diffable and owned by whoever owns the repository.

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

from .store import (
    InRepoLedger,
    LedgerError,
    LedgerScope,
    Stat,
    Unsupported,
    compare,
    current_epoch,
    read_ledger,
    summarize,
)

__all__ = [
    "InRepoLedger",
    "LedgerError",
    "LedgerScope",
    "Stat",
    "Unsupported",
    "compare",
    "current_epoch",
    "read_ledger",
    "summarize",
]
