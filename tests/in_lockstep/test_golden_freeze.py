"""GATE-TEST-4 — the golden tree is frozen for the duration of the pivot.

This is the stated replacement for the retired drift gate. That gate compared committed workflow
output against a fresh compile, which stops meaning anything once the emitter is being deleted —
but the property it protected (generated output cannot silently drift) still matters right up to
the moment `src/lockstep/` goes away.

So the tree is pinned by hash instead. A golden test you may regenerate during a pivot is not a
test: `make golden` still exists for a deliberate change, and it must move this pin in the same
commit, where a reviewer sees both.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "golden"
PIN = ROOT / "tests" / "golden.sha256"


def tree_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted(GOLDEN.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(GOLDEN).as_posix().encode())
            digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode())
    return digest.hexdigest()


def test_golden_tree_matches_the_phase_0_pin() -> None:
    expected = PIN.read_text().strip()
    actual = tree_hash()
    assert actual == expected, (
        "the golden tree moved. If deliberate, run `make golden` and update "
        "tests/golden.sha256 in the SAME commit so the diff and the pin are reviewed together.\n"
        f"  pinned: {expected}\n  actual: {actual}"
    )


def test_regen_is_never_set_in_ci() -> None:
    """A gate you can regenerate in the run that checks it is not a gate."""
    if os.environ.get("CI"):
        assert not os.environ.get("LOCKSTEP_REGEN"), (
            "LOCKSTEP_REGEN must never be set in CI — it would rewrite the thing under test"
        )
