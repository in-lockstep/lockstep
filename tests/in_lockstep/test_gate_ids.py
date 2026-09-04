"""The gate ledger's primary key.

This lives beside `test_gates.py` rather than inside it, and the reason is the ratchet itself.
`test_gates.py` excludes its own text from the corpus it searches — it argues about gate ids in
prose, and a file that names an id discharges the row carrying it. So a gate *about* the ledger,
discharged from that file, could never be seen. This one can, at the cost of parsing the id column
twice; the duplicated regex is one line and is guarded below by a count, so a regex that stopped
matching fails here rather than passing vacuously.

The same constraint applies to what may be written here: this file IS in the corpus, so it names no
gate id but the one it discharges, and the positive control uses strings that are not gate-shaped
at all.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

GATES_MD = Path(__file__).resolve().parents[2] / "design" / "gates.md"

ROW_ID = re.compile(r"^\| `(GATE-[A-Za-z0-9-]+)` \|")


def _ids() -> list[str]:
    return [m.group(1) for line in GATES_MD.read_text().splitlines() if (m := ROW_ID.match(line))]


def _duplicates(ids: list[str]) -> list[str]:
    return sorted(name for name, n in Counter(ids).items() if n > 1)


def test_the_id_column_parses() -> None:
    """A uniqueness assertion is satisfied by any small number of rows, including none."""
    assert len(_ids()) > 50, f"parsed only {len(_ids())} gate ids from gates.md"


def test_gate_test_7_no_gate_id_appears_twice() -> None:
    """GATE-TEST-7. Nothing else checks it, and the two-sided ratchet is keyed on it.

    `test_gates.py` searches its corpus as one concatenated string, so two rows sharing an id are
    each discharged by the other's test: delete either test and both rows stay green. The unmet
    half fares worse — an id some unrelated test already cites can never be marked `unmet`, so the
    only status a duplicated id can carry is a reassuring one. One id named two unrelated
    properties from #136 until #213 on exactly that footing, and `pytest.mark.parametrize` hid it:
    colliding `ids=` are silently suffixed `0` and `1`.
    """
    duplicated = _duplicates(_ids())
    assert not duplicated, f"design/gates.md defines these ids more than once: {duplicated}"


def test_the_duplicate_check_would_notice_one() -> None:
    """The positive control, over plain strings: `_duplicates` is not gate-aware, and a literal
    gate id written in this file would discharge whichever row carries it."""
    assert _duplicates(["a", "a", "b"]) == ["a"]
