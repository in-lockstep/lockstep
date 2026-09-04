"""The promoted corpus, and the two things that keep it honest.

`evidence/cases/` is the durable end of the recording loop. Everything upstream of it expires: a
cassette dies with the CI runner that wrote it, and the candidate cases harvested from it ride an
artifact with a declared retention, so inaction deletes them. Promotion is the one act that makes
any of it permanent, and permanent is the right word -- a promoted case holds a whole composed
prompt and a whole diff, and `git rm` does not unpublish it.

So two properties, and neither is about correctness of code:

  * the set stays small enough that somebody still reads it;
  * everything in it settles, on every commit, offline.

The second is wired in the Makefile rather than here, because it is a property of the run. This
asserts the wiring exists, for the same reason `GATE-TEST-3` asserts `make cov` runs in CI: a gate
that lives only in a file nobody executes is a gate that has already stopped holding.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evidence" / "cases"

#: Promoted cases per directory. Past this, promoting one means retiring one in the same pull
#: request, with the reason in the message.
#:
#: The number is not load-bearing and nobody has shown 24 discriminates between two prompts better
#: than 50 would. What it is for is a ceiling somebody has to argue with: an uncapped corpus grows
#: by accretion until nobody reads it, and a corpus nobody reads is a set of published prompts with
#: no counterpart benefit. Raise it deliberately, the way `.coverage-floor` moves, or not at all.
CAP = 24


def _over_cap(root: Path) -> dict[str, int]:
    """Families past the cap, and the ONLY place the comparison is written.

    The control below drives this same function. It used to restate the predicate instead, which
    made it a test of `>` rather than of the check: inverting the real assertion to `> CAP * 1000`
    left both of them green. A control that cannot fail when the thing fails is decoration.
    """
    return {name: len(paths) for name, paths in _families(root).items() if len(paths) > CAP}


def _prerequisites(makefile: list[str], target: str) -> list[str]:
    """What `target:` depends on, from the Makefile's own line for it."""
    for line in makefile:
        if line.startswith(f"{target}:"):
            return line.split(":", 1)[1].split()
    return []


def _recipe(makefile: list[str], target: str) -> list[str]:
    """The tab-indented commands under `target:`, stopping at the next unindented line."""
    out: list[str] = []
    inside = False
    for line in makefile:
        if line.startswith(f"{target}:"):
            inside = True
            continue
        if inside:
            if line.startswith("\t"):
                out.append(line.strip())
            elif line.strip():
                break
    return out


def _families(root: Path) -> dict[str, list[Path]]:
    """Cases grouped by the directory `eval harvest --family` files them under.

    Takes the root rather than closing over `EVIDENCE` so the control below can drive it over a
    tree it built. A counter that only ever runs against the real directory is a counter whose
    behaviour on a full one is a guess.
    """
    found: dict[str, list[Path]] = {}
    for path in sorted(root.rglob("*.json")):
        found.setdefault(path.parent.name, []).append(path)
    return found


def test_the_promoted_corpus_has_a_home() -> None:
    """It has to exist before `make evidence` can settle what is in it, and git does not track an
    empty directory. Empty is a real state here, not a missing one."""
    assert EVIDENCE.is_dir(), "evidence/cases/ is gone, so `make evidence` settles nothing"


def test_gate_evidence_1_no_family_exceeds_the_cap() -> None:
    """GATE-EVIDENCE-1, first half. The 25th promotion is a decision, not an accretion."""
    over = _over_cap(EVIDENCE)
    assert not over, (
        f"{over} exceeds the cap of {CAP}. Promoting past it means retiring one in the same pull "
        f"request and saying why, or raising the cap deliberately in this file."
    )


def test_the_cap_check_would_notice_one(tmp_path: Path) -> None:
    """The positive control, and load-bearing here in a way it usually is not.

    This corpus starts EMPTY. The assertion above therefore passes over zero directories and would
    go on passing if `_families` stopped matching, if `rglob` were pointed at the wrong root, or if
    the comparison were inverted -- for as long as nobody promotes anything, which is a while. So
    the counter is driven over a tree built to break it.
    """
    family = tmp_path / "review"
    family.mkdir()
    for i in range(CAP):
        (family / f"case-{i}.json").write_text("{}")
    assert _over_cap(tmp_path) == {}, "the check fires at the cap, which is one too early"

    (family / "one-too-many.json").write_text("{}")
    assert _over_cap(tmp_path) == {"review": CAP + 1}


def test_every_promoted_case_is_a_case() -> None:
    """`load_cases` is `rglob("*.json")` and `Case.parse` refuses an object with no `expect`, so a
    stray JSON file here -- a downloaded cassette, a provenance note -- does not sit inertly beside
    the cases. It crashes `eval run` for everyone. Caught here, where the message can say so.

    Vacuous while nothing is promoted, and left that way on purpose: it is a guard for the pull
    request that adds the first case, not a claim about today.
    """
    from in_lockstep.evaluation.cases import Case

    for path in sorted(EVIDENCE.rglob("*.json")):
        Case.parse(json.loads(path.read_text()), name=path.stem, path=path)


def test_gate_evidence_1_every_promoted_case_is_actually_settled() -> None:
    """GATE-EVIDENCE-1. `make evidence` exits 0 on a SKIP, so green does not mean settled.

    That is right for `eval run` in general -- the twenty-seven hand-written cases carry no request
    and skipping them is the honest outcome -- and wrong for this corpus, whose entire premise is
    that what is in it settles. A promoted case that skips is a published prompt and diff buying
    nothing, and it would sit there green forever.

    So the two ways a promoted case can skip are refused here, where the message can name the case:
    no recorded request, and a request that no longer hashes to the key it was recorded under.
    """
    from in_lockstep.ai.replay import key_of, request_from
    from in_lockstep.evaluation.cases import Case

    for path in sorted(EVIDENCE.rglob("*.json")):
        case = Case.parse(json.loads(path.read_text()), name=path.stem, path=path)
        request = (case.input or {}).get("request")
        assert isinstance(request, dict), f"{path.name} has no recorded request, so `eval run` skips it"
        declared = str((case.harvested or {}).get("key", ""))
        if declared:
            assert declared == key_of(request_from(request)), (
                f"{path.name}: its request no longer hashes to {declared[:12]}, so it carries an "
                f"answer to a different question and `eval run` refuses it"
            )


def test_a_promoted_case_settles_without_a_cassette() -> None:
    """GATE-EVIDENCE-1, second half, and the reason `GATE-EVAL-4` exists.

    The tape a case was harvested from was destroyed with the runner that wrote it, so a promoted
    case carrying only a pointer would be a file that can never be settled by anyone. Checked here
    rather than left to `make evidence`, whose failure would name the corpus rather than the case.
    """
    from in_lockstep.evaluation.cases import Case

    for path in sorted(EVIDENCE.rglob("*.json")):
        case = Case.parse(json.loads(path.read_text()), name=path.stem, path=path)
        assert case.recorded.get("content"), (
            f"{path.name} carries no recorded answer, so it can only be settled by reopening "
            f"{(case.harvested or {}).get('cassette', 'a cassette')}, which does not exist"
        )


def test_make_check_settles_the_promoted_corpus() -> None:
    """The wiring, asserted the way GATE-TEST-3 asserts `make cov` runs.

    A corpus that is only settled when somebody remembers to point a command at it is a corpus
    whose first regression is found by nobody. This is what makes the first promotion measured on
    the commit that makes it.
    """
    makefile = (ROOT / "Makefile").read_text().splitlines()

    # The recipe, not the file. A target whose body was emptied would leave the string in a comment
    # and this test green, which is the shape of the failure it exists to catch.
    body = _recipe(makefile, "evidence")
    assert any("eval run --corpus evidence/cases" in line for line in body), (
        f"the `evidence` target settles nothing: {body}"
    )

    # BOTH entry points. `make check` is what a person runs; `ci` is what CI runs, and asserting
    # only the first left the half the gate row claims covered by nothing -- delete `evidence` from
    # `ci:` and the suite stayed green while no pull request settled the corpus again.
    for target in ("check", "ci"):
        prerequisites = _prerequisites(makefile, target)
        assert "evidence" in prerequisites, f"`make {target}` does not settle the corpus: {prerequisites}"
