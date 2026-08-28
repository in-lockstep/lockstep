"""The command line, which had no tests at all until this file.

`cli.py` is the largest module in the package and the only one every user touches. It is also the
composition root: which `Lockstep` gets built, whether the repository's own module is loaded, and
which bindings survive are all decided here. None of that was asserted, and the gap showed —
`review`, the one command that spends money, built its own `Lockstep` from scratch and silently
discarded every binding, budget, policy contribution and model route the module declared.

`--dry-run` and `--offline` exist precisely so this file can be written without a key or a cent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from in_lockstep.cli import main

MODULE = '''
from in_lockstep import Lockstep, Policy
from in_lockstep.core.spend import Budget

lockstep = Lockstep.detect()
lockstep.budget = Budget(usd={budget})
lockstep.contribute(Policy(name="repo", source="test", max_turns={turns}))
lockstep.models.route("review", "{model}")
'''


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory with its own lockstep.py, loaded from the working tree.

    Not a git repository, deliberately: with no ref to resolve, `config_ref` treats the working
    tree as the trusted source, which is the local-development path and the one under test here.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    return tmp_path


def _write(
    repo: Path,
    *,
    budget: float = 5.00,
    turns: int = 9,
    model: str = "anthropic:claude-haiku-4-5",
) -> None:
    (repo / "lockstep.py").write_text(MODULE.format(budget=budget, turns=turns, model=model))


def test_review_loads_the_repositorys_own_module(repo: Path) -> None:
    """The headline: `lockstep.py` is the configuration, including for the command that spends."""
    _write(repo)
    result = CliRunner().invoke(main, ["review", "--dry-run", "--base", "HEAD"])
    assert result.exit_code == 0, result.output
    # The module routes review at a model the CLI's own default would not have chosen. A route
    # nothing reads is how `Models.route` shipped: written by the config, consumed by nobody.
    assert "haiku" in (repo / ".in-lockstep/ledger/review-security.json").read_text()


def test_an_untyped_model_flag_does_not_outrank_a_declared_route(repo: Path) -> None:
    _write(repo, model="google:gemini-2.5-flash")
    CliRunner().invoke(main, ["review", "--dry-run", "--base", "HEAD"])
    assert "gemini-2.5-flash" in (repo / ".in-lockstep/ledger/review-security.json").read_text()


def test_an_explicit_model_flag_does_outrank_it(repo: Path) -> None:
    """An override the user actually typed is an override; a default is not."""
    _write(repo, model="google:gemini-2.5-flash")
    CliRunner().invoke(
        main, ["review", "--dry-run", "--base", "HEAD", "--model", "anthropic:claude-opus-4-6"]
    )
    assert "opus" in (repo / ".in-lockstep/ledger/review-security.json").read_text()


def test_no_module_still_runs_on_detected_defaults(repo: Path) -> None:
    """A repository without a lockstep.py is supported, not an error."""
    result = CliRunner().invoke(main, ["review", "--dry-run", "--base", "HEAD"])
    assert result.exit_code == 0, result.output


def test_telemetry_says_when_the_cli_cannot_see_the_chain(repo: Path) -> None:
    """`spans 0` for a run that emitted spans elsewhere is a wrong number, not a missing one."""
    (repo / "lockstep.py").write_text(
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.middleware import otel\n"
        "lockstep = Lockstep.detect()\n"
        "lockstep.middleware += [otel()]\n"
    )
    result = CliRunner().invoke(main, ["review", "--dry-run", "--base", "HEAD"])
    assert "the CLI is not in that chain" in result.output
    assert "spans     0" not in result.output


def test_ls_reads_the_same_module_review_does(repo: Path) -> None:
    """`ls` answers "what will actually run". It is wrong the moment a command disagrees with it."""
    _write(repo, turns=3)
    result = CliRunner().invoke(main, ["ls"])
    assert result.exit_code == 0, result.output
    assert "repo" in result.output


def test_run_refuses_an_unregistered_workflow_by_name(repo: Path) -> None:
    result = CliRunner().invoke(main, ["run", "nope"])
    assert result.exit_code != 0
    assert "selfcheck" in result.output
