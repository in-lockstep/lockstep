"""The repository's standing instructions reach the model that writes in it.

`Lockstep.detect` has found `AGENTS.md`/`CLAUDE.md` since the beginning and `ls` has listed them,
and that was the whole of it: the names went into `RepoFacts.agent_instructions`, `summary()`
printed them for `doctor`, and nothing ever opened the files. A repository could write down the one
convention its agents keep getting wrong and the agent would never see a word of it.

It is not hypothetical here. This repository's CLAUDE.md leads with `python_classes = ["*Tests"]`,
and two `/implement` runs — $21 and $31 — failed with `tdd.not_red` because the model wrote `Test*`
classes that pytest collected silently as nothing. Both ran in a checkout containing the file that
explains it.

The other half of these tests is the trust boundary, which is the reason this is opt-in per verb
rather than on for everything.
"""

from __future__ import annotations

from pathlib import Path

from in_lockstep.adapters.ai.fix import DiagnoseThenFix, FixStrategy
from in_lockstep.adapters.ai.instructions import MAX_INSTRUCTION_CHARS, house_rules
from in_lockstep.adapters.ai.oneshot import Oneshot
from in_lockstep.adapters.ai.strategy import AiStrategy
from in_lockstep.adapters.ai.tdd import TDD
from in_lockstep.core.context import AGENT_INSTRUCTION_FILES


def test_every_instruction_file_is_read_not_just_the_first(tmp_path: Path) -> None:
    """`AGENTS.md` is the vendor-neutral spelling other clients read; `CLAUDE.md` is Claude Code's.
    A repository that keeps both usually means both, and their contents are often different — so
    first-match-wins would drop half of what somebody deliberately wrote down, in the direction of
    the model knowing less."""
    (tmp_path / "AGENTS.md").write_text("Prefer module-level test functions.")
    (tmp_path / "CLAUDE.md").write_text("Test classes end in Tests, not begin with Test.")

    names = [name for name, _ in house_rules(tmp_path)]
    texts = "\n".join(text for _, text in house_rules(tmp_path))

    assert names == ["house-rules/AGENTS.md", "house-rules/CLAUDE.md"]
    assert "Prefer module-level test functions." in texts
    assert "Test classes end in Tests" in texts


def test_the_order_is_fixed_so_the_composed_prompt_is_deterministic(tmp_path: Path) -> None:
    """A cassette is keyed on the whole composed prompt. A set that came back in directory order
    would replay for nobody, and the failure would look like a cache problem."""
    for name in AGENT_INSTRUCTION_FILES:
        (tmp_path / name).write_text(f"rules from {name}")
    assert [n for n, _ in house_rules(tmp_path)] == [f"house-rules/{n}" for n in AGENT_INSTRUCTION_FILES]


def test_a_repository_with_no_instructions_adds_no_layers(tmp_path: Path) -> None:
    assert house_rules(tmp_path) == ()


def test_an_empty_file_is_not_a_layer(tmp_path: Path) -> None:
    """A blank CLAUDE.md is a placeholder somebody has not filled in. Rendering it would put an
    empty labelled section into every prompt, which reads like an instruction to ignore."""
    (tmp_path / "CLAUDE.md").write_text("   \n\n  \n")
    assert house_rules(tmp_path) == ()


def test_an_oversized_file_is_truncated_rather_than_dropped(tmp_path: Path) -> None:
    """Nothing curates a system layer, so the bound is here. Truncated from the top rather than
    dropped whole, because the top of such a file is the rules and the bottom is the appendices —
    and dropping it entirely would lose the conventions to protect a token budget."""
    (tmp_path / "CLAUDE.md").write_text("R" * (MAX_INSTRUCTION_CHARS + 5_000))
    ((_name, text),) = house_rules(tmp_path)
    assert len(text) < MAX_INSTRUCTION_CHARS + 200
    assert "truncated" in text


def test_an_undecodable_file_is_skipped_rather_than_failing_the_run(tmp_path: Path) -> None:
    """Losing one convention beats losing the pipeline over what is probably a stray binary."""
    (tmp_path / "CLAUDE.md").write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    assert house_rules(tmp_path) == ()


# -- who reads them, and why it is not everyone ------------------------------------------------


def test_the_writing_verbs_read_house_rules_and_the_default_is_off() -> None:
    """The default is off so that a new strategy does not silently acquire a repo-authored system
    layer by subclassing. Opting in is a line somebody wrote."""
    assert AiStrategy.reads_house_rules is False
    assert TDD.reads_house_rules is True
    assert Oneshot.reads_house_rules is True
    assert DiagnoseThenFix.reads_house_rules is True
    assert FixStrategy.reads_house_rules is True


def test_review_does_not_read_them_because_its_checkout_is_the_contributors() -> None:
    """The load-bearing half of the opt-in.

    `implement.yml` and `fix.yml` trigger on `issue_comment`/`issues`, which GitHub runs on the
    DEFAULT branch — the reviewed file. `lockstep.yml` triggers on `pull_request`, where
    `actions/checkout` gives the merge ref, so a `CLAUDE.md` there is whatever the contributor
    wrote. Reading it would let anyone who can open a pull request put text into the system prompt
    of the model reviewing their own change, which is the injection this framework is mostly about
    refusing.
    """
    from in_lockstep.adapters.ai.review import AiReview
    from in_lockstep.adapters.ai.triage import AiTriage

    # `getattr` with a default rather than an attribute access, because these two do not subclass
    # `AiStrategy` today and so carry no flag at all. That is a stronger exclusion than the flag
    # being False, and this spelling keeps asserting the property rather than the current class
    # tree — it stays honest if review is ever moved onto the shared base.
    assert getattr(AiReview, "reads_house_rules", False) is False
    assert getattr(AiTriage, "reads_house_rules", False) is False


def test_nothing_else_quietly_opts_in() -> None:
    """A whole-tree check, because the risk is not that somebody argues for turning this on where
    the checkout is untrusted — it is that a new strategy inherits it from the wrong base and
    nobody notices. The allowed set is written out; adding to it should take an argument."""

    def descendants(cls: type) -> set[type]:
        out: set[type] = set()
        for sub in cls.__subclasses__():
            out.add(sub)
            out |= descendants(sub)
        return out

    import in_lockstep.adapters.ai  # noqa: F401  (import for the side effect of registering subclasses)

    opted_in = {c.__name__ for c in descendants(AiStrategy) if c.__dict__.get("reads_house_rules")}
    assert opted_in == {"ImplementStrategy", "FixStrategy"}, (
        f"{opted_in} declare `reads_house_rules`. Only verbs whose CI trigger runs on the default "
        f"branch may read repository-authored text into a system prompt — see instructions.py."
    )


def test_the_rules_land_after_the_guardrails_and_never_before(tmp_path: Path) -> None:
    """Position is the security property. A repository may tell a model how its tests are named;
    it may not displace the framework guardrail that says what the model may not do."""
    (tmp_path / "CLAUDE.md").write_text("HOUSE RULE MARKER")
    session = Oneshot(lambda ctx: None, repo_root=str(tmp_path))._session(object())
    system = session.layers.guardrail_texts() + session.layers.trailing_texts()
    rendered = "\n\n".join(system)

    assert "HOUSE RULE MARKER" in rendered
    first_guardrail = session.layers.guardrails[0][1].strip()[:40]
    assert rendered.index(first_guardrail) < rendered.index("HOUSE RULE MARKER")


def test_the_detected_names_and_the_read_names_come_from_one_list() -> None:
    """They were two literals, which is how the framework came to report finding a file it never
    opened. One list means a name added to detection is a name that gets read."""
    import inspect

    from in_lockstep import lockstep as facade

    assert "AGENT_INSTRUCTION_FILES" in inspect.getsource(facade._detect_facts)
