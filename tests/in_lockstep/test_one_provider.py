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
        if not {"offline", "record"} <= params or node.name == "_one_provider":
            # The guard itself takes both, by construction. Excluding it by name rather than by
            # some cleverer rule, so the exclusion is one line a reader can see and disbelieve.
            continue
        calls = {
            child.func.id
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        if "_one_provider" not in calls:
            missing.append(node.name)
    assert not missing, f"{missing} take --offline and --record and never refuse the pair"
