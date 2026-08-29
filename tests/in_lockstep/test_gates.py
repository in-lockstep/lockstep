"""The gate ledger, kept honest.

`design/gates.md` opens by arguing that a gate defined nowhere is indistinguishable from one that
does not exist. That argument has a second half the file did not make: a gate whose *status* is
recorded once and never re-checked decays into the same thing — a document asserting a property
nobody measures. Four rows of `docs/controls-crosswalk.md` reached 1.0 saying **Replaced** about
mechanisms with no caller on any live path, which is what that decay looks like in practice.

So the status column is a two-sided ratchet, the same shape as the coverage floor:

- a gate marked `held` must be discharged somewhere, or the claim is unbacked;
- a gate marked `unmet` must be discharged nowhere, so that *implementing* one fails this test
  until its status is updated.

The second direction is the one that matters. Without it, someone writes the missing test, the
suite goes green, and `gates.md` still says `unmet` — and the file drifts in the safe-looking
direction, which is exactly how the crosswalk got where it did.

`unit only` is deliberately unconstrained: those gates have tests, and what they lack is a call
site, which no grep over the test tree can see.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATES_MD = ROOT / "design" / "gates.md"

STATUSES = {"held", "unit only", "partial", "unmet", "deferred", "retired"}

# Where a gate may be discharged. The Makefile counts: GATE-TEST-3 is a property of how the suite
# is run, not something a test can assert about itself.
SEARCHED = [ROOT / "tests", ROOT / "Makefile"]

ROW = re.compile(r"^\| `(GATE-[A-Za-z0-9-]+)` \| ([^|]+) \| ([^|]+) \|")


def _rows() -> list[tuple[str, str]]:
    found = []
    for line in GATES_MD.read_text().splitlines():
        m = ROW.match(line)
        if m:
            found.append((m.group(1), m.group(3).strip()))
    return found


def _corpus() -> str:
    text = []
    for target in SEARCHED:
        if target.is_file():
            text.append(target.read_text())
            continue
        for path in target.rglob("*"):
            # Skip this file: it names every gate id, and would discharge all of them.
            if path.is_file() and path.suffix in {".py", ".md"} and path != Path(__file__):
                text.append(path.read_text())
    return "\n".join(text)


def _discharged(gate: str, corpus: str) -> bool:
    """A test may cite a gate by id or, as pytest naming requires, in snake_case."""
    return gate in corpus or gate.lower().replace("-", "_") in corpus


ROWS = _rows()
CORPUS = _corpus()


def test_the_ledger_is_not_empty() -> None:
    """A regex that silently matches nothing would make every assertion below vacuous."""
    assert len(ROWS) > 50, f"parsed only {len(ROWS)} gate rows from gates.md"


@pytest.mark.parametrize("gate,status", ROWS, ids=[g for g, _ in ROWS])
def test_every_gate_declares_a_known_status(gate: str, status: str) -> None:
    assert status in STATUSES, f"{gate} has status {status!r}; expected one of {sorted(STATUSES)}"


@pytest.mark.parametrize("gate", [g for g, s in ROWS if s == "held"], ids=[g for g, s in ROWS if s == "held"])
def test_a_held_gate_is_discharged_somewhere(gate: str) -> None:
    assert _discharged(gate, CORPUS), (
        f"{gate} is marked `held` in gates.md but nothing under tests/ or the Makefile names it. "
        f"Either discharge it or change its status."
    )


@pytest.mark.parametrize(
    "gate", [g for g, s in ROWS if s == "unmet"], ids=[g for g, s in ROWS if s == "unmet"]
)
def test_an_unmet_gate_is_discharged_nowhere(gate: str) -> None:
    assert not _discharged(gate, CORPUS), (
        f"{gate} is marked `unmet` in gates.md but something under tests/ names it. If you have "
        f"implemented it, say so in gates.md — and check whether a row in "
        f"docs/controls-crosswalk.md now understates what is in force."
    )
