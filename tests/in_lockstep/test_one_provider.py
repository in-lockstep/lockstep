"""Two flags that each choose a provider are a contradiction, not a precedence question.

`--offline --record` recorded nothing and reported success. The provider selection took the first
matching branch and `offline` came first, so `record` was discarded in silence: the person got a
replay of the cassette they were trying to overwrite, a green run, and no cassette.

That is an O4 failure with the worst possible shape. Recording is what a run does, and the one flag
that makes a recording could be dropped without a word — so somebody meaning to spend once and keep
the result got a $0 replay and found out when `eval harvest` gave them nothing.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from in_lockstep.cli import _one_provider, main

SRC = Path(__file__).resolve().parents[2] / "src" / "in_lockstep" / "cli.py"


def test_a_replay_cannot_also_be_a_recording() -> None:
    with pytest.raises(Exception, match="--offline") as refused:
        _one_provider(dry_run=False, offline=True, record=True)
    assert "--record" in str(refused.value)


def test_a_canned_answer_cannot_also_be_a_recording() -> None:
    """`--dry-run` never reaches a provider at all, so there is nothing to record."""
    with pytest.raises(Exception, match="--dry-run"):
        _one_provider(dry_run=True, offline=False, record=True)


def test_a_canned_answer_cannot_also_be_a_replay() -> None:
    with pytest.raises(Exception, match="--dry-run"):
        _one_provider(dry_run=True, offline=True, record=False)


def test_one_flag_alone_is_the_ordinary_case() -> None:
    """The negative control. Without it, a guard that refused everything would pass every
    assertion above."""
    _one_provider(dry_run=True, offline=False, record=False)
    _one_provider(dry_run=False, offline=True, record=False)
    _one_provider(dry_run=False, offline=False, record=True)
    _one_provider(dry_run=False, offline=False, record=False)


def test_review_refuses_the_pair_before_reading_a_key(tmp_path: Path, monkeypatch) -> None:
    """Through the live command, and before any credential work: the refusal must not arrive as a
    missing-key error, which is what a person would get if the guard sat lower down."""
    monkeypatch.chdir(tmp_path)
    for name in [k for k in os.environ if k.startswith("GITHUB_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = CliRunner().invoke(main, ["review", "--offline", "--record", "--diff", "x"])
    assert result.exit_code != 0
    assert "--offline" in result.output and "--record" in result.output, result.output
    assert "credential" not in result.output.lower(), result.output


def test_every_command_that_can_replay_and_record_refuses_the_pair() -> None:
    """The ratchet, so a sixth command cannot quietly reintroduce the defect.

    Any command taking both `offline` and `record` has the same contradiction available, and the
    original bug was five commands sharing one copied block. A structural check is what stops the
    sixth copy.
    """
    tree = ast.parse(SRC.read_text())
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        params = {a.arg for a in node.args.args + node.args.kwonlyargs}
        if not {"offline", "record"} <= params or node.name in {"_one_provider", "_recording"}:
            # The guard takes both by construction, and so does `_recording`, which resolves the
            # tri-state default and calls the guard on the way. Both excluded by name rather than
            # by some cleverer rule, so the exclusion is two names a reader can see and disbelieve
            # — and `_recording` is not taken on trust: the test below asserts it calls the guard,
            # so excluding it here cannot break the chain it is excluded for being part of.
            continue
        calls = {
            child.func.id
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        if not calls & {"_one_provider", "_recording"}:
            missing.append(node.name)
    assert not missing, f"{missing} take --offline and --record and never refuse the pair"


def test_the_default_resolver_reaches_the_guard() -> None:
    """The other link in the chain the test above now allows.

    Commands stopped calling `_one_provider` directly when recording became the default: they call
    `_recording`, which resolves "nobody chose" and refuses an explicit contradiction on the way.
    Accepting that name without checking where it leads would turn a ratchet into a spelling.
    """
    tree = ast.parse(SRC.read_text())
    resolver = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_recording"
    )
    calls = {
        child.func.id
        for child in ast.walk(resolver)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
    }
    assert "_one_provider" in calls


@pytest.mark.parametrize(
    ("dry_run", "offline", "record", "expected"),
    [
        # Nobody chose: a run that reaches a provider keeps what it gets.
        (False, False, None, True),
        # Nobody chose, but the run was already told to answer from somewhere that is not a
        # provider. There is nothing to keep, and refusing here would make the default a
        # contradiction somebody never typed — which is the whole reason this is tri-state.
        (False, True, None, False),
        (True, False, None, False),
        # Declined outright.
        (False, False, False, False),
        (False, True, False, False),
    ],
)
def test_what_a_run_keeps_when_nobody_said(dry_run, offline, record, expected):
    """`GATE-RECORD-4`. O4 says recording is not an option a run turns on."""
    from in_lockstep.cli import _recording

    assert _recording(dry_run=dry_run, offline=offline, record=record) is expected


@pytest.mark.parametrize(("dry_run", "offline"), [(True, False), (False, True)])
def test_an_explicit_record_still_collides_with_a_replay(dry_run, offline):
    """The half the default must not erode. `--offline --record` is a contradiction and stays one;
    what changed is only that not typing `--record` is no longer typing it."""
    from in_lockstep.cli import _recording

    with pytest.raises(click.ClickException) as raised:
        _recording(dry_run=dry_run, offline=offline, record=True)
    assert "--record" in str(raised.value)


def test_the_verbs_that_call_a_provider_record_without_being_asked():
    """`GATE-RECORD-4`, over the CLI surface rather than the resolver.

    Asserted as a property of the declared option — a `--record/--no-record` pair whose default is
    `None` — because that is what makes not typing anything different from typing `--no-record`.
    A plain `is_flag` default of `True` would have no way to express "declined" at all.
    """
    from in_lockstep.cli import main

    for name in ("review", "triage", "rfe", "backport", "implement", "run"):
        option = next(o for o in main.commands[name].params if o.name == "record")
        assert option.default is None, f"{name} --record defaults to {option.default!r}"
        assert option.secondary_opts == ["--no-record"], f"{name} cannot decline"


def test_pack_try_is_deliberately_not_in_that_list():
    """The control, and a real distinction rather than an oversight.

    `pack try` measures a pack by REPLAYING its cassettes for nothing; its own docstring calls
    `--record` "the other direction, and the only one that spends". O4 is about a run that calls a
    model, and this is a command whose default is to call none — so recording by default here
    would make a measurement tool start spending money, which is the opposite of what it is for.
    """
    from in_lockstep.cli import main

    option = next(o for o in main.commands["pack"].commands["try"].params if o.name == "record")
    assert option.default is False
    assert option.secondary_opts == []


def test_a_run_that_reached_no_provider_says_nothing_about_recording():
    """`GATE-RECORD-4`, the half about noise rather than about the default.

    Recording being what a run does means every deterministic run now carries a tape, and a tape
    with nothing in it has nothing to report. `run selfcheck` dispatches Test and calls no model;
    `kept 0 inference(s)` there describes something that did not happen.

    Both dispatch paths go through one function, because the rule was written into one of them and
    `selfcheck` kept printing the line — two writers of one decision, agreeing right up until one
    was edited.
    """
    from in_lockstep.cli import _report_what_was_kept

    class _Tape:
        path = "/tmp/nothing.json"

        def __init__(self, calls):
            self._calls = calls

        def calls(self):
            return self._calls

    class _Ctx:
        def __init__(self, tokens):
            self.spend = SimpleNamespace(charged=SimpleNamespace(total_tokens=tokens))

    # Nothing called, nothing spent, nobody asked: silent.
    assert _report_what_was_kept(_Tape(0), _Ctx(0), asked=False) is False
    # Something kept: reported.
    assert _report_what_was_kept(_Tape(3), _Ctx(900), asked=False) is True
    # Nothing kept but tokens spent — the recorder was bypassed, which is the one case where
    # `kept 0` is the most important line in the output. Silence here would hide it.
    assert _report_what_was_kept(_Tape(0), _Ctx(900), asked=False) is True
    # And somebody who typed `--record` asked a question. Answering it with silence tells them
    # neither where the tape went nor whether one exists, which is how a flag becomes a flag
    # people stop passing. The first version of this rule was quiet here and was wrong.
    assert _report_what_was_kept(_Tape(0), _Ctx(0), asked=True) is True
