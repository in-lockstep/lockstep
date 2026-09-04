"""A trial measures a pack for nothing, and refuses to report what it did not measure.

The states carry the whole argument. `decided` is the only one that feeds a pass rate;
`unrecorded` means the author's cassette holds no answer for that case, which is an absence of
evidence rather than a failure; `outstanding` means a judge has not answered; `not exercised`
means a corpus family nothing here can drive. A trial that collapsed any of those into `failed`
or into `passed` would produce a number that reads like a measurement and is not one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from in_lockstep.ai.invoker import AiInvoker
from in_lockstep.ai.pricing import CostTable, Rate
from in_lockstep.ai.replay import Cassette, RecordingProvider, ReplayProvider
from in_lockstep.cli import main
from in_lockstep.core.spend import Budget, Spend
from in_lockstep.llm.interface import LLMProvider
from in_lockstep.llm.types import LLMInput, LLMOutput, TokenUsage
from in_lockstep.packs import GROUP, Pack
from in_lockstep.privileged.egress import EgressPolicy, UnsandboxedEgress
from in_lockstep.privileged.redact import Redact
from in_lockstep.trial import DECIDED, NOT_EXERCISED, OUTSTANDING, UNRECORDED, render, run

MODEL = "claude-sonnet-4-6"
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "acme-review-prompts" / "acme_review_prompts"

FOUND = json.dumps(
    {
        "findings": [{"path": "app.py", "line": 3, "summary": "module-global SESSION outlives the request"}],
        "verdict": "changes requested",
    }
)
CLEAN = json.dumps({"findings": [], "verdict": "looks fine"})


class Scripted(LLMProvider):
    """Answers whatever it is told to, once per call, in order."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[LLMInput] = []

    def name(self) -> str:
        return "scripted"

    async def generate(self, input: LLMInput) -> LLMOutput:
        self.calls.append(input)
        content = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return LLMOutput(content=content, usage=TokenUsage(input_tokens=100, output_tokens=20))


def _invoker_factory(provider: LLMProvider, *, egress: Any = None):
    """An invoker over a stub provider.

    `egress` defaults to `EgressPolicy.detect()`, which enforces nothing here — and that is the
    point for a replay: a cassette puts no byte on the wire, the invoker knows it
    (`AiInvoker.transmits`), and the untrusted-content trigger is suppressed accordingly. A
    recording pass really does transmit, so it passes the documented opt-out instead.
    """
    table = CostTable()
    table.add(MODEL, Rate(input_per_m=1.0, output_per_m=2.0))

    def build(ctx):
        return AiInvoker(
            provider,
            model=MODEL,
            cost_table=table,
            spend=Spend(budget=Budget(usd=5.0)),
            redact=Redact(),
            egress=egress or EgressPolicy.detect(),
        )

    return build


def _pack(
    root: Path, *, cases: dict[str, dict], family: str = "review", lens: str = "security-reviewer"
) -> Pack:
    module = root / "trial_pack"
    module.mkdir(exist_ok=True)
    (module / "pack.toml").write_text('[pack]\nkind = "prompt"\nsummary = "For measuring"\n')
    (module / "__init__.py").write_text('"""Prose only."""\n')
    (module / "cassettes").mkdir(exist_ok=True)
    corpus = module / "corpus" / family / lens
    corpus.mkdir(parents=True, exist_ok=True)
    for name, case in cases.items():
        (corpus / f"{name}.json").write_text(json.dumps(case))
    return Pack(name="trial-pack", module="trial_pack", distribution="trial-pack", root=module)


DIFF = "+SESSION = Session(bind=engine)\n+def handler(request):\n+    return SESSION.query(User).all()\n"

CASE_DETERMINISTIC = {"name": "finds-it", "input": {"diff": DIFF}, "expect": {"contains": ["SESSION"]}}
CASE_RUBRIC = {"name": "judged", "input": {"diff": DIFF}, "expect": {"rubric": "names the mechanism"}}


def _record(pack: Pack, provider: Scripted) -> Cassette:
    """Produce the pack's cassette by running the trial once against a stub.

    This is the author's half of the workflow, with a stub standing in for the provider: a trial
    can only replay what somebody recorded, and the recording is made by the same composition that
    will later replay it. Recording against a stub tests the harness; it makes no claim about a
    model, which is why this cassette lives in a tmpdir and never in the repository.
    """
    tape = Cassette.load(pack.file("cassettes/trial.json"))
    recording = RecordingProvider(provider, tape, Redact())
    run(pack, invoker_factory=_invoker_factory(recording, egress=UnsandboxedEgress()))
    tape.save()
    return tape


# -- the loop --------------------------------------------------------------------------


def test_a_recorded_case_replays_and_is_decided(tmp_path: Path) -> None:
    """The whole point: measured deterministically, offline, with no key and no spend."""
    pack = _pack(tmp_path, cases={"finds-it": CASE_DETERMINISTIC})
    tape = _record(pack, Scripted([FOUND]))

    trial = run(pack, invoker_factory=_invoker_factory(ReplayProvider(tape)))
    assert [r.state for r in trial.results] == [DECIDED]
    assert trial.results[0].passed is True
    assert trial.summary()["pass_rate"] == 1.0


def test_a_case_that_was_never_recorded_is_unrecorded_and_not_a_failure(tmp_path: Path) -> None:
    """A cassette holds what its author recorded. Counting a gap as a failure would let a pack
    look bad for an incomplete recording, and counting it as a pass would be worse."""
    pack = _pack(tmp_path, cases={"finds-it": CASE_DETERMINISTIC})
    _record(pack, Scripted([FOUND]))

    # A second case the author never recorded.
    corpus = pack.file("corpus/review/security-reviewer")
    (corpus / "unrecorded.json").write_text(
        json.dumps({"name": "unrecorded", "input": {"diff": "+x = 1\n"}, "expect": {"contains": ["x"]}})
    )
    tape = Cassette.load(pack.file("cassettes/trial.json"))
    trial = run(pack, invoker_factory=_invoker_factory(ReplayProvider(tape)))

    states = {r.case: r.state for r in trial.results}
    assert states == {"finds-it": DECIDED, "unrecorded": UNRECORDED}
    assert trial.summary()["pass_rate"] == 1.0, "the gap is excluded from the rate, not counted against it"
    assert trial.summary()["unrecorded"] == 1


def test_a_rubric_is_outstanding_rather_than_passed(tmp_path: Path) -> None:
    """`evaluation/`'s contract, which a trial does not get to soften."""
    pack = _pack(tmp_path, cases={"judged": CASE_RUBRIC})
    tape = _record(pack, Scripted([FOUND]))

    trial = run(pack, invoker_factory=_invoker_factory(ReplayProvider(tape)))
    assert [r.state for r in trial.results] == [OUTSTANDING]
    assert trial.summary()["pass_rate"] is None, "nothing was decided, so there is no rate"


def test_nothing_decided_is_none_and_never_zero(tmp_path: Path) -> None:
    """A rate over an empty denominator is the number this module exists to refuse to print."""
    pack = _pack(tmp_path, cases={"judged": CASE_RUBRIC})
    tape = _record(pack, Scripted([FOUND]))
    summary = run(pack, invoker_factory=_invoker_factory(ReplayProvider(tape))).summary()
    assert summary["pass_rate"] is None
    assert summary["decided"] == 0


def test_a_failing_case_is_reported_with_what_it_wanted(tmp_path: Path) -> None:
    pack = _pack(tmp_path, cases={"finds-it": CASE_DETERMINISTIC})
    tape = _record(pack, Scripted([CLEAN]))  # the model found nothing; the case wanted SESSION

    trial = run(pack, invoker_factory=_invoker_factory(ReplayProvider(tape)))
    assert trial.results[0].state == DECIDED
    assert trial.results[0].passed is False
    assert "SESSION" in trial.results[0].detail
    assert trial.summary()["pass_rate"] == 0.0, "a real zero, over a case that was actually decided"


def test_your_cases_are_counted_apart_from_the_packs(tmp_path: Path) -> None:
    """The number worth installing on is the one measured on your cases, which is the whole
    argument for measuring locally rather than reading a badge."""
    pack = _pack(tmp_path, cases={"finds-it": CASE_DETERMINISTIC})
    mine = tmp_path / "mine" / "review" / "security-reviewer"
    mine.mkdir(parents=True)
    (mine / "ours.json").write_text(json.dumps({**CASE_DETERMINISTIC, "name": "ours"}))

    tape = _record(pack, Scripted([FOUND]))
    trial = run(pack, extra=tmp_path / "mine", invoker_factory=_invoker_factory(ReplayProvider(tape)))

    assert trial.summary("pack")["cases"] == 1
    assert trial.summary("yours")["cases"] == 1
    assert {r.origin for r in trial.results} == {"pack", "yours"}


def test_a_family_a_trial_cannot_drive_is_counted_not_dropped(tmp_path: Path) -> None:
    """A pass rate over cases nobody ran is the same failure with better manners."""
    pack = _pack(
        tmp_path,
        cases={"a-bug": {"name": "a-bug", "input": {}, "expect": {"contains": ["x"]}}},
        family="fix",
        lens="bug-analyst",
    )
    trial = run(pack, invoker_factory=_invoker_factory(Scripted([FOUND])))

    assert [r.state for r in trial.results] == [NOT_EXERCISED]
    assert trial.summary()["not_exercised"] == 1
    assert trial.summary()["pass_rate"] is None
    assert "fix" in trial.results[0].detail


def test_the_rendering_says_why_there_is_no_rate(tmp_path: Path) -> None:
    """A blank where a number should be is read as zero. This says which absence it is."""
    pack = _pack(tmp_path, cases={"judged": CASE_RUBRIC})
    tape = _record(pack, Scripted([FOUND]))
    rendered = "\n".join(
        render(
            run(pack, invoker_factory=_invoker_factory(ReplayProvider(tape))),
            pack="trial-pack",
            recording=False,
        )
    )
    assert "no key, no spend" in rendered
    assert "nothing was decided" in rendered
    assert "outstanding" in rendered


# -- the CLI -------------------------------------------------------------------------------


def _install(monkeypatch: pytest.MonkeyPatch, pack: Pack) -> None:
    import importlib.metadata

    entry = type(
        "Entry",
        (),
        {
            "name": pack.name,
            "value": pack.module,
            "root": pack.root,
            "dist": type("D", (), {"name": pack.distribution, "version": "1.0.0"})(),
        },
    )()
    monkeypatch.setattr(
        importlib.metadata, "entry_points", lambda *, group: [entry] if group == GROUP else []
    )


def test_a_pack_with_no_cassette_says_somebody_has_to_pay_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honest message, and the reason the catalog's fourth criterion exists. Measuring for
    nothing is only possible because somebody once measured for money."""
    monkeypatch.chdir(tmp_path)
    for name in [k for k in __import__("os").environ if k.startswith("GITHUB_")]:
        monkeypatch.delenv(name, raising=False)
    _install(monkeypatch, _pack(tmp_path, cases={"finds-it": CASE_DETERMINISTIC}))

    result = CliRunner().invoke(main, ["pack", "try", "trial-pack"])
    assert result.exit_code != 0
    assert "ships no cassette" in result.output
    assert "--record" in result.output


def test_the_example_pack_cannot_be_measured_yet_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`examples/acme-review-prompts` ships prose and cases and no recording, which is exactly the
    state `market lint` reports about it. The two commands agree because they are reading the same
    fact rather than two descriptions of it."""
    monkeypatch.chdir(tmp_path)
    for name in [k for k in __import__("os").environ if k.startswith("GITHUB_")]:
        monkeypatch.delenv(name, raising=False)
    _install(
        monkeypatch,
        Pack(
            name="acme-review-prompts",
            module="acme_review_prompts",
            distribution="acme-review-prompts",
            root=EXAMPLE,
        ),
    )

    result = CliRunner().invoke(main, ["pack", "try", "acme-review-prompts"])
    assert result.exit_code != 0
    assert "ships no cassette" in result.output
    assert "fault in it" in result.output, "a missing recording is a fact about the pack, not a verdict"


# -- which lenses a trial composes (issue 202) ----------------------------------------------


def _with_prompts(pack: Pack, **bodies: str) -> Pack:
    """Write `prompts/<name>.md` into a pack, the way a pack author ships one."""
    prompts = pack.file("prompts")
    assert prompts is not None
    prompts.mkdir(exist_ok=True)
    for name, text in bodies.items():
        (prompts / f"{name}.md").write_text(f"---\nname: {name}\ndescription: a lens\n---\n\n{text}\n")
    return pack


def test_a_pack_is_measured_on_a_lens_it_invented(tmp_path: Path) -> None:
    """Issue 202. The loop walked the SHIPPED lens names, so a pack could only be measured on a
    lens it overrode. A pack shipping `prompts/a11y.md` was ignored, its cases resolved an aspect
    the adapter had never heard of, and `pack try` reported a working pack as broken — for exactly
    the pack kind the extension story is about."""
    from in_lockstep.cli import _trial_lenses

    pack = _with_prompts(
        _pack(tmp_path, cases={"finds-it": CASE_DETERMINISTIC}, lens="a11y-reviewer"),
        a11y="Read for contrast and focus order.",
    )
    lenses = _trial_lenses(pack)
    assert "a11y" in lenses, sorted(lenses)
    assert lenses["a11y"].aspect == "a11y", "a finding has to come back as review.a11y"


def test_a_prompt_fragment_with_no_cases_is_not_a_lens(tmp_path: Path) -> None:
    """The negative control, and not hypothetical: `examples/acme-review-prompts` ships
    `prompts/house.md` as a guardrail fragment beside `prompts/security.md` as a lens override.
    `guardrails()` reads fragments out of the same directory, so admitting every `.md` under
    `prompts/` would turn that pack's house rules into a phantom lens emitting `review.house`.

    The pairing with `corpus/review/<stem>-reviewer/` is what separates them, and `aspect_of`
    already documents it as a convention a pack author satisfies deliberately."""
    from in_lockstep.cli import _trial_lenses

    pack = _with_prompts(
        _pack(tmp_path, cases={"finds-it": CASE_DETERMINISTIC}),
        house="Prefer the boring option.",
    )
    lenses = _trial_lenses(pack)
    assert "house" not in lenses, sorted(lenses)


def test_a_pack_overriding_a_shipped_lens_still_substitutes_its_body(tmp_path: Path) -> None:
    """The behaviour that already worked, kept. A pack shipping `prompts/security.md` is measured
    through its own body inside the SHIPPED layer stack."""
    from in_lockstep.cli import _trial_lenses
    from in_lockstep.prompts.review import LENSES

    pack = _with_prompts(
        _pack(tmp_path, cases={"finds-it": CASE_DETERMINISTIC}),
        security="Look for session scope.",
    )
    lenses = _trial_lenses(pack)
    assert lenses["security"] is not LENSES["security"], "the pack's body must replace the shipped one"
    assert issubclass(lenses["security"], LENSES["security"])
    assert lenses["security"].aspect == "security"


def test_a_pack_with_no_prompts_at_all_measures_against_the_shipped_lenses(tmp_path: Path) -> None:
    """A corpus-only pack is a real pack: it says "here are cases, measure the framework's own
    lenses against them". Walking the pack's prompts must not lose that."""
    from in_lockstep.cli import _trial_lenses
    from in_lockstep.prompts.review import LENSES

    lenses = _trial_lenses(_pack(tmp_path, cases={"finds-it": CASE_DETERMINISTIC}))
    assert sorted(lenses) == sorted(LENSES)


def test_a_lens_a_pack_invented_is_measured_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GATE-PACK-5. The property `_trial_lenses` exists to serve, driven through `run` rather than
    asserted on the map it returns. Before the fix this case came back `errored` with `review.unknown_aspect`;
    the pack was fine and the harness could not see it.

    `pack try`'s whole argument is that a pack is measured before it is trusted, so a pack kind that
    cannot produce a number is a pack kind nobody should install."""
    pack = _with_prompts(
        _pack(tmp_path, cases={"finds-it": CASE_DETERMINISTIC}, lens="a11y-reviewer"),
        a11y="Read for contrast and focus order.",
    )
    # A pack's body is resolved through `importlib.resources` on its own module, which is how a
    # pip-installed pack works and what a tmpdir has to stand in for.
    monkeypatch.syspath_prepend(str(tmp_path))

    from in_lockstep.cli import _trial_lenses

    lenses = _trial_lenses(pack)
    tape = Cassette.load(pack.file("cassettes/trial.json"))
    run(
        pack,
        invoker_factory=_invoker_factory(
            RecordingProvider(Scripted([FOUND]), tape, Redact()), egress=UnsandboxedEgress()
        ),
        lenses=lenses,
    )
    tape.save()

    trial = run(pack, invoker_factory=_invoker_factory(ReplayProvider(tape)), lenses=lenses)
    assert [r.state for r in trial.results] == [DECIDED], [(r.state, r.notes) for r in trial.results]
    assert trial.results[0].passed is True
    assert trial.summary()["pass_rate"] == 1.0
