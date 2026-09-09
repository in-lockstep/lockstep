"""GATE-BLAST-1: a review lens is told what the change reaches and the diff does not show.

A diff shows the lines that changed. What a reviewer needs and cannot see is what DEPENDS on them:
on one real sixteen-file change here, 142 symbols were impacted and 109 of them were outside the
diff. That is arithmetic over the repository's own call graph, so it is computed and handed over
rather than offered as a tool the model may or may not think to use — a reviewer's blind spot is
not something a session knows to look for (O7).

Per lens, because it is not equally useful to each, and off by default, because it changes the
composed prompt: a lens that turns it on invalidates recordings made against that lens, which is
the reason `search_code` is opt-in per strategy (#375).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from in_lockstep.adapters.ai.review import AiReview, Review, _blast_note
from in_lockstep.adapters.graft import BLAST_DEPTH, FORBIDDEN, Graft, blast_argv, blast_render, cache_root
from in_lockstep.ai.context import ContextCurator, ContextItem, ContextNeed
from in_lockstep.prompts.review import LENSES, Lens


def _payload(**over: Any) -> dict[str, Any]:
    base = {
        "changed": [{"path": "src/a.py"}, {"path": "tests/test_a.py"}],
        "impacted": [
            {"path": "src/a.py", "name": "in_the_diff"},
            {"path": "src/b.py", "name": "reaches_one"},
            {"path": "src/b.py", "name": "reaches_two"},
            {"path": "src/c.py", "name": "reaches_three"},
        ],
        "testModules": [{"files": ["tests/test_b.py", "tests/test_a.py"]}],
    }
    base.update(over)
    return base


# -- what is asked of graft ------------------------------------------------------------------------


def test_gate_blast_1_the_argv_is_the_frameworks_and_never_names_people() -> None:
    """GATE-BLAST-1. `blast` reads git history and names the people behind each affected area by
    default. That is contributor identities flowing into a model prompt to answer a question this
    framework does not ask, so `--no-owners` is not a preference."""
    argv = blast_argv("/repo", "origin/main", Path("/idx"))
    assert "--no-owners" in argv
    assert argv[:2] == ["blast", "--dir"] and argv[-1] == "/repo"
    assert "--format" in argv and argv[argv.index("--format") + 1] == "json"
    assert argv[argv.index("-d") + 1] == str(BLAST_DEPTH), "the depth is the framework's"
    assert "--name" not in argv and "--export-viz" not in argv


def test_gate_blast_1_the_model_layer_cannot_be_composed_even_by_mistake() -> None:
    """GATE-BLAST-1. `--name` names the areas with an LLM call under Graft's own key — a model
    call this framework did not route and a credential it does not hold. It is refused where every
    argv is composed, not left to whoever writes the next caller."""
    assert "--name" in FORBIDDEN and "--deep" in FORBIDDEN


def test_json_is_asked_for_the_way_blast_spells_it() -> None:
    """`--format json`, not the `--json` every other Graft query takes: `blast` answers
    `error: unknown option '--json'`, which is cheaper to find by running it than by reading."""
    assert "--json" not in blast_argv("/repo", "main", Path("/idx"))


# -- what a reviewer is shown ------------------------------------------------------------------------


def test_gate_blast_1_the_render_keeps_what_the_diff_cannot_show_and_drops_the_rest() -> None:
    """GATE-BLAST-1. The raw answer is 186 KB for a sixteen-file change here, most of it the diff's
    own hunks — which the review already has in front of it. What is kept is what depends on the
    changed lines and is NOT in the diff."""
    rendered = blast_render(_payload())
    assert "src/b.py: reaches_one, reaches_two" in rendered
    assert "src/c.py: reaches_three" in rendered
    assert "in_the_diff" not in rendered, "a symbol inside the diff is not news"
    assert "tests/test_b.py" in rendered, "the tests covering the reach are named"
    assert "tests/test_a.py" not in rendered.split("Test modules")[-1], "a test in the diff is not"


def test_a_change_that_reaches_nothing_renders_nothing() -> None:
    """An empty section is worse than none: a lens handed a heading and no content reads it as a
    finding about the change rather than as an absence of one."""
    assert blast_render(_payload(impacted=[{"path": "src/a.py", "name": "in_the_diff"}])) == ""


def test_gate_blast_1_the_render_is_bounded_and_says_what_it_left_out() -> None:
    """GATE-BLAST-1. A change that touches a hub reaches most of a repository, and a list that long
    stops being read. Bounded by files and by symbols per file, and the remainder is counted."""
    many = [{"path": f"src/f{n}.py", "name": f"sym{n}"} for n in range(60)]
    rendered = blast_render(_payload(impacted=many), max_files=5, max_symbols=2)
    assert "…and 55 more files" in rendered
    wide = [{"path": "src/one.py", "name": f"sym{n}"} for n in range(9)]
    assert "(+7 more)" in blast_render(_payload(impacted=wide), max_symbols=2)


# -- which lens gets it ------------------------------------------------------------------------------


def test_gate_blast_1_a_lens_asks_for_it_and_the_adapter_provisions_what_it_needs() -> None:
    """GATE-BLAST-1. Per lens, because it is not equally useful to each — and the adapter installs
    Graft only when one asked, so a repository whose lenses do not want it provisions nothing."""
    assert Lens(prompt=LENSES["security"]).blast_radius is False, "off unless asked"

    quiet = AiReview()
    assert quiet.graft is None and quiet.provisions == ()

    asked = AiReview(lenses={"security": Lens(prompt=LENSES["security"], blast_radius=True)})
    assert isinstance(asked.graft, Graft)
    assert [type(p).__name__ for p in asked.provisions] == ["Graft"]


def test_gate_blast_1_the_radius_rides_as_untrusted_beside_the_diff(tmp_path: Path) -> None:
    """GATE-BLAST-1. The symbol names in it were written by the party under review, exactly as the
    diff was. That the framework computed the shape does not make the words the framework's."""
    adapter = AiReview()
    package = adapter._gather(
        Review(aspect="security", base="a", head="b", diff="diff --git a/x b/x\n+one\n"),
        str(tmp_path),
        "src/b.py: reaches_one",
    )
    kinds = {item.kind: item for item in package.items}
    assert "impact" in kinds and "diff" in kinds
    assert kinds["impact"].provenance.name == "UNTRUSTED_EXTERNAL"
    assert kinds["impact"].path.endswith("#impact")

    without = adapter._gather(Review(aspect="security", base="a", head="b", diff="d\n"), str(tmp_path))
    assert [i.kind for i in without.items] == ["diff"], "nothing computed, nothing added"


def test_gate_blast_1_a_budget_that_cannot_hold_both_keeps_the_diff() -> None:
    """GATE-BLAST-1. The radius is ABOUT the diff, so it is the commentary and the diff is the
    subject. A curator that dropped the subject to keep the commentary would be reviewing the
    call graph."""
    from in_lockstep.ai.context import Provenance

    items = [
        ContextItem(kind="impact", content="x" * 4000, provenance=Provenance.UNTRUSTED_EXTERNAL, path="i"),
        ContextItem(kind="diff", content="y" * 4000, provenance=Provenance.UNTRUSTED_EXTERNAL, path="d"),
    ]
    kept = ContextCurator().curate(items, ContextNeed(token_budget=1100))
    assert [i.kind for i in kept.items] == ["diff"], kept.dropped


def test_the_record_says_whether_the_lens_had_it() -> None:
    """Two reviews of one change that saw different context are two different reviews, and a reader
    of the ledger has to be able to tell which one they are reading."""
    assert [f.id for f in _blast_note("some radius", "")] == ["review.blast"]
    (missing,) = _blast_note("", "search_code.no_index: the build failed")
    assert missing.id == "review.blast.unavailable" and "no_index" in missing.message
    assert _blast_note("", "") == (), "a lens that never asked says nothing"


# -- the real thing, where it is provisioned ----------------------------------------------------------


def test_gate_blast_1_live_the_radius_of_a_real_change_names_a_caller_outside_the_diff(
    tmp_path: Path,
) -> None:
    """GATE-BLAST-1 against the real Graft. Skips by name where the cache is cold, because a test
    must not reach a registry; `in-lockstep provision` on a repository whose lenses ask fills it."""
    graft = Graft(cache=cache_root())
    if shutil.which("node") is None or not graft.binary.exists():
        pytest.skip(f"GATE-BLAST-1 live clause not checked: no provisioned graft at {graft.binary}")

    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (["git", "init", "-q"], ["git", "branch", "-M", "main"]):
        subprocess.run(command, cwd=repo, check=True)
    (repo / "a.py").write_text("def helper():\n    return 1\n")
    (repo / "b.py").write_text("from a import helper\n\n\ndef caller():\n    return helper()\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "one"], cwd=repo, check=True
    )
    # The change: `helper` gains an argument. `caller` is not in the diff and is exactly what a
    # reviewer needs to be told about.
    (repo / "a.py").write_text("def helper(scale=1):\n    return scale\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "two"], cwd=repo, check=True
    )

    fingerprint = asyncio.run(graft.fingerprint(str(repo)))
    rendered, why = asyncio.run(graft.blast(str(repo), "HEAD~1", str(repo), fingerprint))
    assert why == "", why
    assert "b.py" in rendered and "caller" in rendered, rendered
    assert "a.py:" not in rendered, "the changed file is what the diff already shows"
