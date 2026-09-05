"""The objectives ledger, kept honest. Discharges `GATE-TEST-8`.

`design/gates.md` has a two-sided ratchet because a status recorded once and never re-checked
decays into a document asserting a property nobody measures. The objectives in `CLAUDE.md` had no
such thing. They are cited in commit messages — genuinely, and often — but a citation is a claim
about one change, and nothing was making a claim about the whole. A survey found that O5 had been
unmet since the beginning and that O4's default contradicted O4's own sentence, which is the
failure this file exists to stop repeating: a property somebody has to go looking for is a
property nobody is holding.

So `design/objectives.md` is a join over `design/gates.md`, and this checks the join rather than
the prose. The direction that matters is the second one, exactly as it is for gates: implementing
an unmet gate must fail something until a person re-reads the objective that was claiming the gap.

This module is separate from `test_gates.py` for the reason `test_gate_ids.py` is: that file
excludes its own text from the corpus it searches, so a test living inside it cannot cite a gate.
It names `GATE-TEST-8` and no other id — every gate it reasons about is read from a file, because
a literal id here would discharge that gate in `test_gates.py`'s search and quietly satisfy the
ratchet it is supposed to be independent of.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[2]
CLAUDE_MD = ROOT / "CLAUDE.md"
GATES_MD = ROOT / "design" / "gates.md"
OBJECTIVES_MD = ROOT / "design" / "objectives.md"
README_MD = ROOT / "README.md"

# Every file that states the mission. It is quoted rather than referenced in all three because
# each has a different reader — an agent, an adopter evaluating the project, and somebody standing
# in front of the objective ledger — and none of them should have to follow a link to find out
# what the thing is for. Three copies of a sentence is three chances for it to drift, so the
# copies are checked instead of trusted.
STATES_THE_MISSION = (CLAUDE_MD, README_MD, OBJECTIVES_MD)

# An objective may be `held`, `partial` or `unmet` and nothing else. `unit only` is deliberately
# absent: it describes a mechanism with no call site, which is a property of a gate. An objective
# served only by mechanisms nobody calls is `unmet`, and the gates it cites carry the distinction.
STATUSES = {"held", "partial", "unmet"}

# The gate statuses an objective must account for. `deferred` is past the cut line by a recorded
# decision and `retired` has no subject left, so neither is a gap anybody is carrying.
UNSETTLED = {"unmet", "partial", "unit only"}

# `**O4 — Every model call is recorded.** An inference nobody kept...` — the title is what sits
# between the em dash and the period that closes the bold run.
HEADLINE = re.compile(r"^\*\*(O\d+) — (.+?)\.\*\*")

# | `O4` | Every model call is recorded | partial | `GATE-A-1`, ... | `GATE-A-2` | the gap |
#
# Two gate columns, not one. `carried by` holds gates that hold; `blocked on` holds gates that do
# not. Splitting them is what lets every claim be about a single gate, so an objective blocked on
# four things cannot go quiet when three of them close.
LEDGER_ROW = re.compile(r"^\| `(O\d+)` \| ([^|]+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \| (.+) \|\s*$")

# The `claimed by no objective` table, which carries gate ids rather than objective ids.
UNCLAIMED_ROW = re.compile(r"^\| `(GATE-[A-Za-z0-9-]+)` \| ([^|]+) \| (.+) \|\s*$")

GATE_ROW = re.compile(r"^\| `(GATE-[A-Za-z0-9-]+)` \| ([^|]+) \| ([^|]+) \|")

CITED = re.compile(r"`(GATE-[A-Za-z0-9-]+)`")


class Row(NamedTuple):
    oid: str
    title: str
    status: str
    carried: tuple[str, ...]
    blocked: tuple[str, ...]
    gap: str


def _titles(text: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip() for m in map(HEADLINE.match, text.splitlines()) if m}


def _ledger(text: str) -> list[Row]:
    rows = []
    for line in text.splitlines():
        m = LEDGER_ROW.match(line)
        if m:
            rows.append(
                Row(
                    m.group(1),
                    m.group(2).strip(),
                    m.group(3).strip(),
                    tuple(CITED.findall(m.group(4))),
                    tuple(CITED.findall(m.group(5))),
                    m.group(6).strip(),
                )
            )
    return rows


def _gate_status(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = GATE_ROW.match(line)
        if m:
            out[m.group(1)] = m.group(3).strip()
    return out


def _unclaimed_section(text: str) -> set[str]:
    """The gates the ledger admits no objective claims.

    Read only from below the heading, because the ledger table above it cites gates in the same
    backticked shape and a whole-file scan would silently conflate the two — which would make the
    section agree with itself no matter what it said.
    """
    _, _, tail = text.partition("## Claimed by no objective")
    return {m.group(1) for m in map(UNCLAIMED_ROW.match, tail.splitlines()) if m}


def _miscarried(rows: list[Row], gate_status: dict[str, str]) -> list[str]:
    """Gates claimed as carrying an objective that do not hold, and `held` rows carrying nothing.

    The over-claiming direction. An objective no gate carries can never read as satisfied.
    """
    bad = []
    for row in rows:
        if row.status == "held" and not row.carried:
            bad.append(f"{row.oid} is held and no gate carries it")
        bad.extend(
            f"{row.oid} is carried by {g}, which is {gate_status.get(g, 'absent')}"
            for g in row.carried
            if gate_status.get(g) != "held"
        )
    return bad


def _misblocked(rows: list[Row], gate_status: dict[str, str]) -> list[str]:
    """Gates claimed as blocking an objective that have since closed, and status disagreeing with
    the column.

    The direction that matters, and the reason `blocked on` is its own column. It fires on the
    FIRST gate to close rather than the last: implement one, and the row still naming it as a
    blocker turns red the moment its status flips. An earlier draft asked only that some cited gate
    be open, and O5's four `GATE-IMPROVE` blockers are why that was not enough — three could have
    been built with nothing here noticing.
    """
    bad = []
    for row in rows:
        bad.extend(
            f"{row.oid} is blocked on {g}, which is {gate_status.get(g, 'absent')}"
            for g in row.blocked
            if gate_status.get(g) not in UNSETTLED
        )
        if row.status == "held" and row.blocked:
            bad.append(f"{row.oid} is held while blocked on {', '.join(row.blocked)}")
        if row.status != "held" and not row.blocked:
            bad.append(f"{row.oid} is {row.status} and blocked on nothing")
    return bad


def _should_be_unclaimed(rows: list[Row], gate_status: dict[str, str]) -> set[str]:
    """Unsettled gates no objective is blocked on."""
    claimed = {g for row in rows for g in row.blocked}
    return {g for g, status in gate_status.items() if status in UNSETTLED and g not in claimed}


def test_the_ledgers_are_not_empty():
    """A positive control. Every assertion below is over a parse, and a regex that stopped
    matching would make all of them pass over nothing — the vacuity `GATE-TEST-8` was written
    against, and the same one `test_gates.py` had to be given after the fact."""
    assert len(_titles(CLAUDE_MD.read_text())) == 10
    assert len(_ledger(OBJECTIVES_MD.read_text())) == 10
    assert len(_gate_status(GATES_MD.read_text())) > 50


def test_the_ledger_names_every_objective_exactly_once():
    rows = _ledger(OBJECTIVES_MD.read_text())
    ids = [row.oid for row in rows]
    assert sorted(ids) == sorted(_titles(CLAUDE_MD.read_text())), "ledger and CLAUDE.md disagree on the set"
    assert len(ids) == len(set(ids)), f"an objective is listed more than once: {ids}"


def test_a_row_states_the_title_claude_md_states():
    """The objectives are the subject; this ledger is a claim about them. A claim whose subject
    moved is not a claim, so rewording an objective without re-reading its row fails here."""
    titles = _titles(CLAUDE_MD.read_text())
    for row in _ledger(OBJECTIVES_MD.read_text()):
        assert row.title == titles[row.oid], (
            f"{row.oid}: ledger says {row.title!r}, CLAUDE.md says {titles[row.oid]!r}"
        )


def test_every_status_is_one_of_the_three():
    for row in _ledger(OBJECTIVES_MD.read_text()):
        assert row.status in STATUSES, f"{row.oid} carries unknown status {row.status!r}"


def test_every_gate_an_objective_cites_exists():
    known = _gate_status(GATES_MD.read_text())
    for row in _ledger(OBJECTIVES_MD.read_text()):
        for gate in row.carried + row.blocked:
            assert gate in known, f"{row.oid} cites {gate}, which design/gates.md does not define"


def test_every_gate_said_to_carry_an_objective_holds():
    """The over-claiming direction, plus: a `held` objective must be carried by something."""
    text = OBJECTIVES_MD.read_text()
    assert not _miscarried(_ledger(text), _gate_status(GATES_MD.read_text()))


def test_the_carried_check_catches_a_gate_that_does_not_hold():
    # Ids invented for this control. They appear in no ledger, so naming them here discharges
    # nothing in `test_gates.py`'s search over the test tree.
    open_gate = Row("O1", "t", "partial", ("GATE-CONTROLONLY-1",), ("GATE-CONTROLONLY-2",), "a gap")
    assert _miscarried([open_gate], {"GATE-CONTROLONLY-1": "unmet"})
    assert _miscarried([Row("O1", "t", "held", (), (), "—")], {})
    assert not _miscarried([open_gate], {"GATE-CONTROLONLY-1": "held"})


def test_every_gate_said_to_block_an_objective_is_still_open():
    """The direction with teeth. Closing any one blocker forces its row to be re-read."""
    text = OBJECTIVES_MD.read_text()
    assert not _misblocked(_ledger(text), _gate_status(GATES_MD.read_text()))


def test_the_blocked_check_fires_on_the_first_blocker_to_close():
    """The scenario this column exists for: an objective blocked on four things must not go quiet
    when three of them are built."""
    row = Row("O5", "t", "partial", (), ("GATE-CONTROLONLY-1", "GATE-CONTROLONLY-2"), "a gap")
    both_open = {"GATE-CONTROLONLY-1": "unmet", "GATE-CONTROLONLY-2": "unmet"}
    assert not _misblocked([row], both_open)
    assert _misblocked([row], {**both_open, "GATE-CONTROLONLY-1": "held"})
    assert _misblocked([Row("O9", "t", "held", (), ("GATE-CONTROLONLY-1",), "—")], both_open)
    assert _misblocked([Row("O9", "t", "partial", (), (), "a gap")], both_open)


def test_an_objective_that_is_not_held_names_its_gap():
    for row in _ledger(OBJECTIVES_MD.read_text()):
        if row.status == "held":
            continue
        assert row.gap and row.gap != "—", f"{row.oid} is {row.status} and states no gap"


def test_the_unclaimed_section_lists_exactly_the_gates_no_objective_carries():
    """The under-claiming direction, and the one with teeth. When somebody implements an unmet
    gate its status flips in gates.md, and either an objective was claiming it as a gap — in which
    case that row now needs re-reading — or nothing wanted it, and this section says so out loud.
    CLAUDE.md is blunt about the second case: surface that serves no objective is surface to
    remove."""
    text = OBJECTIVES_MD.read_text()
    expected = _should_be_unclaimed(_ledger(text), _gate_status(GATES_MD.read_text()))
    assert _unclaimed_section(text) == expected


def test_the_unclaimed_computation_ignores_settled_and_claimed_gates():
    """The control for the assertion above. A `deferred` gate is past the cut line by a recorded
    decision and a `retired` one has no subject, so neither is a gap anybody carries."""
    rows = [Row("O1", "t", "partial", (), ("GATE-CONTROLONLY-1",), "a gap")]
    status = {
        "GATE-CONTROLONLY-1": "unmet",  # cited, so not unclaimed
        "GATE-CONTROLONLY-2": "unmet",  # cited by nobody
        "GATE-CONTROLONLY-3": "deferred",
        "GATE-CONTROLONLY-4": "held",
    }
    assert _should_be_unclaimed(rows, status) == {"GATE-CONTROLONLY-2"}


def _mission(text: str) -> str:
    """The blockquote under the mission heading, as one line.

    Whitespace-normalised because the three files wrap it to their own margins, and a test that
    failed on a re-wrap would be a test people learn to work around rather than one they trust.
    """
    _, _, tail = text.partition("## The mission")
    quoted = []
    for line in tail.splitlines():
        if line.startswith(">"):
            quoted.append(line.lstrip("> ").strip())
        elif quoted:
            break
    return " ".join(" ".join(quoted).split())


def test_every_file_that_states_the_mission_states_the_same_one():
    said = {path.name: _mission(path.read_text()) for path in STATES_THE_MISSION}
    assert len(set(said.values())) == 1, said


def test_the_mission_is_actually_found_in_each_file():
    """The positive control. `_mission` returns an empty string for a file with no such heading,
    and three empty strings agree with each other perfectly."""
    for path in STATES_THE_MISSION:
        assert _mission(path.read_text()).startswith("Enable teams of software engineers"), path.name
    assert _mission("# no mission here\n\n> a quote about something else\n") == ""


# `1 of 10 are `held`` — written this way in both files so one pattern finds both.
CENSUS = re.compile(r"\b(\d+) of (\d+) are `held`")


def _claimed_census(text: str) -> list[tuple[int, int]]:
    return [(int(a), int(b)) for a, b in CENSUS.findall(text)]


def test_every_prose_census_matches_the_table():
    """A count in prose that nobody recomputes is the failure this whole file is about, and the
    first draft committed it: the ledger said two objectives were `held` because one of them was
    when the sentence was written and `partial` by the time it was committed."""
    rows = _ledger(OBJECTIVES_MD.read_text())
    actual = (sum(1 for row in rows if row.status == "held"), len(rows))
    claims = [
        (path.name, claim)
        for path in (CLAUDE_MD, OBJECTIVES_MD)
        for claim in _claimed_census(path.read_text())
    ]
    assert claims, "no file states the census; the pattern that finds it has stopped matching"
    for name, claim in claims:
        assert claim == actual, f"{name} says {claim[0]} of {claim[1]} held; the table says {actual[0]}"
