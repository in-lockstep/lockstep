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


# -- init: the first thing a new adopter runs ------------------------------------------------


def test_init_scaffolds_commands_that_exist(repo: Path) -> None:
    """The failure that shipped: the scaffold invoked `run review --base`, and neither exists.

    `run` accepts only `selfcheck` and declares no `--base`, so the workflow every new adopter
    committed failed on their first pull request. A scaffold is the one artifact that must not
    describe a CLI other than the one it ships with, because nobody reads it before running it.
    """
    import re

    import yaml

    assert CliRunner().invoke(main, ["init"]).exit_code == 0
    workflow = yaml.safe_load((repo / ".github/workflows/lockstep.yml").read_text())

    known = set(main.commands)
    invoked = set()
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            for verb in re.findall(r"in-lockstep ([a-z-]+)", step.get("run", "") or ""):
                invoked.add(verb)
    assert invoked, "the scaffold invokes no CLI command at all"
    assert invoked <= known, f"scaffold invokes {sorted(invoked - known)}, which do not exist"


def test_the_scaffolded_review_passes_only_options_review_declares(repo: Path) -> None:
    import re

    CliRunner().invoke(main, ["init"])
    text = (repo / ".github/workflows/lockstep.yml").read_text()
    declared = {o for p in main.commands["review"].params for o in p.opts}
    used = set(re.findall(r"(--[a-z-]+)", text.split("in-lockstep review")[1].split("env:")[0]))
    assert used <= declared, f"scaffold passes {sorted(used - declared)} to review"


def test_the_scaffold_uploads_a_path_something_writes(repo: Path) -> None:
    """It pointed at `.in-lockstep/out/`, which no code path in the package ever creates."""
    import yaml

    CliRunner().invoke(main, ["init"])
    workflow = yaml.safe_load((repo / ".github/workflows/lockstep.yml").read_text())
    paths = [
        s["with"]["path"]
        for j in workflow["jobs"].values()
        for s in j["steps"]
        if "upload-artifact" in str(s.get("uses", ""))
    ]
    assert paths == [".in-lockstep/"], paths


def test_the_scaffold_carries_a_timeout(repo: Path) -> None:
    """Without one the CI default is 360 minutes, and there is no other wall clock in the job."""
    import yaml

    CliRunner().invoke(main, ["init"])
    workflow = yaml.safe_load((repo / ".github/workflows/lockstep.yml").read_text())
    assert all("timeout-minutes" in j for j in workflow["jobs"].values())


def test_the_trampoline_is_independent_of_the_repository(tmp_path: Path, monkeypatch) -> None:
    """Q4's condition: byte-identical in an empty directory and a full repository.

    A compiler cannot pass this. It binds the workflow file only — `init`'s lockstep.py scaffold
    may detect the stack freely.
    """
    outputs = []
    for name, populate in (("empty", False), ("full", True)):
        target = tmp_path / name
        target.mkdir()
        if populate:
            (target / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")
            (target / "tests").mkdir()
        monkeypatch.chdir(target)
        CliRunner().invoke(main, ["init"])
        outputs.append((target / ".github/workflows/lockstep.yml").read_text())
    assert outputs[0] == outputs[1]


def test_apply_refuses_rather_than_exiting_zero_without_writing(repo: Path, tmp_path: Path) -> None:
    """A green `apply` job is read as "the change landed"."""
    import json

    artifact = tmp_path / "changeset.json"
    artifact.write_text(json.dumps({"changes": [{"path": "src/x.py", "body": "x = 1"}]}))

    ok = CliRunner().invoke(main, ["apply", "--from-artifact", str(artifact), "--dry-run"])
    assert ok.exit_code == 0
    assert "nothing was written" in ok.output

    refused = CliRunner().invoke(main, ["apply", "--from-artifact", str(artifact)])
    assert refused.exit_code != 0
    assert "does not write yet" in refused.output


def test_init_does_not_announce_a_job_it_did_not_write(repo: Path) -> None:
    """It described a two-job split while scaffolding one job. Prose drifts; this notices."""
    import yaml

    result = CliRunner().invoke(main, ["init"])
    workflow = yaml.safe_load((repo / ".github/workflows/lockstep.yml").read_text())
    for job in ("run", "apply"):
        if job not in workflow["jobs"]:
            assert f"`{job}` holds" not in result.output
