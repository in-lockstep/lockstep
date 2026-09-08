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


def _test_functions(path: Path) -> list[str]:
    """The name and docstring of every collected test function in one file, and nothing else.

    The positive direction used to be a substring search over whole files, so a module docstring
    saying a test "lands in Phase 2", or a comment recording that a mechanism was deleted,
    discharged a `held` row (#315: `GATE-ASYNC-2` and `GATE-GUARD-3` held that way for months).
    What discharges a gate now is what pytest collects: a function whose name starts `test_`,
    at module level or in a `*Tests` class, and what it says about itself in its own docstring.
    A citation anywhere else is prose about a test rather than a test.
    """
    import ast

    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            out.append(node.name + "\n" + (ast.get_docstring(node) or ""))
    return out


def _corpus() -> str:
    text = []
    for target in SEARCHED:
        if target.is_file():
            # The Makefile counts whole: GATE-TEST-3 is a property of how the suite is run.
            text.append(target.read_text())
            continue
        for path in sorted(target.rglob("*.py")):
            # Skip this file: it names every gate id, and would discharge all of them.
            if path != Path(__file__):
                text.extend(_test_functions(path))
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


def test_every_section_a_gate_cites_resolves() -> None:
    """A `deferred` row is an exemption from the objectives ratchet, granted by a citation.

    `deferred` was defined as "the deferral recorded in the plan and ADR 0001 §17.11", and §17.11
    is not in ADR 0001 — it is §17.11 of `design/in-lockstep-design.md`, which says in terms that
    `ctx.park` and `ctx.fan_out` are not on `RunContext` at 1.0. So the decision was recorded
    correctly all along and only the pointer was wrong, which is the more dangerous of the two:
    #272 was filed saying the deferral was recorded nowhere findable, after reading ADR 0001,
    finding no §17, and not looking in the document the word "plan" names.

    A citation nobody can follow and a citation to nothing look identical from outside, and five
    rows are exempt from a ratchet on the strength of this one. So it is checked rather than read.
    """
    import re

    design = (ROOT / "design" / "in-lockstep-design.md").read_text()
    headings = set(re.findall(r"^#+ (\d+(?:\.\d+)*) ", design, re.M))
    assert headings, "no numbered headings parsed; this check would pass over nothing"

    cited = set(re.findall(r"§(\d+(?:\.\d+)*)", GATES_MD.read_text()))
    assert cited, "no section citations found; the pattern that finds them has stopped matching"
    missing = sorted(cited - headings)
    assert not missing, (
        f"design/gates.md cites {missing}, which are not headings in in-lockstep-design.md. "
        f"An exemption granted by a citation nobody can follow is granted by nothing."
    )


def test_a_gate_named_only_in_a_comment_or_a_module_docstring_is_undischarged(tmp_path: Path) -> None:
    """The positive direction's own control (#315). Before it, any text under `tests/` naming a
    gate discharged it, so a module docstring saying a test 'lands in Phase 2' held
    `GATE-ASYNC-2` for months. Only a collected test's name or its own docstring counts now."""
    (tmp_path / "test_prose.py").write_text(
        '"""GATE-CONTROLONLY-1 is described here and nowhere a test runs."""\n'
        "# GATE-CONTROLONLY-2 in a comment\n"
        "def helper():\n"
        '    """GATE-CONTROLONLY-3 in a helper nobody collects."""\n'
        "def test_real():\n"
        '    """GATE-CONTROLONLY-4 in a collected test."""\n'
        "def test_gate_controlonly_5_in_a_name():\n"
        "    pass\n"
    )
    corpus = "\n".join(_test_functions(tmp_path / "test_prose.py"))
    for undischarged in ("GATE-CONTROLONLY-1", "GATE-CONTROLONLY-2", "GATE-CONTROLONLY-3"):
        assert not _discharged(undischarged, corpus), undischarged
    assert _discharged("GATE-CONTROLONLY-4", corpus) and _discharged("GATE-CONTROLONLY-5", corpus)
