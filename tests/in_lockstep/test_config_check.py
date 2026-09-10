"""A change to configuration says so, or a commit says why it is going in unexercised.

GATE-CFG-4. The gap these cover is not that the trusted-ref rule is wrong -- it is right, and
`GATE-CFG-1` holds it -- but that its consequence reached nobody. #418 changed this repository's
`Validate` binding, passed its container job against main's binding, and turned main red within
the hour; #420 reverted it and failed its container job against the same stale configuration.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from in_lockstep.cli import main
from in_lockstep.config_ref import ACKNOWLEDGEMENT, acknowledgement, changed_paths
from in_lockstep.loader import TRUSTED_REF_FILES, unexercised

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "lockstep-config.yml"


def _git(root: Path, *args: str) -> str:
    """Always `-C`. A call that inherits the working directory passes for whoever wrote it and
    fails wherever the framework runs the suite from, which CLAUDE.md records at length."""
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with `main` and a branch off it. `main` is PINNED: `git init` takes whatever
    `init.defaultBranch` says, which is `main` on a laptop that set it and `master` on the runner,
    and every command below naming `main` would fail on CI only."""
    root = tmp_path / "repo"
    (root / ".lockstep").mkdir(parents=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "branch", "-M", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / ".lockstep" / "lockstep.py").write_text("lockstep = object()\n")
    (root / "src.py").write_text("x = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "change")
    return root


def _commit(root: Path, path: str, body: str, message: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


def test_gate_cfg_4_a_change_to_the_lifecycle_module_is_reported_as_unexercised(repo: Path) -> None:
    """GATE-CFG-4. The claim: a change CI loaded no part of says so, naming what was loaded."""
    _commit(repo, ".lockstep/lockstep.py", "lockstep = 'rebound'\n", "feat: rebind Validate")

    found = unexercised(repo, base="main")

    assert found is not None
    assert found.paths == (".lockstep/lockstep.py",)
    assert found.cleared is False
    assert found.ref.reason == "base branch (not the ref under review)"


def test_gate_cfg_4_a_change_touching_no_configuration_reports_nothing(repo: Path) -> None:
    """The negative control. Without it every one of these passes over a function that always
    finds something, which is the vacuous shape that has bitten this repository twice."""
    _commit(repo, "src.py", "x = 2\n", "feat: ordinary work")

    assert unexercised(repo, base="main") is None


def test_gate_cfg_4_run_output_under_the_config_directory_is_not_a_configuration_change(
    repo: Path,
) -> None:
    """`.lockstep/` holds two kinds of thing. A cassette is what a run produced; CI not having
    loaded it is a fact about nothing, and reporting it would make this check noise."""
    _commit(repo, ".lockstep/cassettes/review.json", "{}\n", "chore: a recording")

    assert unexercised(repo, base="main") is None


def test_gate_cfg_4_a_commit_trailer_with_a_reason_clears_it(repo: Path) -> None:
    """The way out, and the only one. Red on a change whose author has done nothing wrong has to
    be clearable by that author, or the check teaches people to ignore red."""
    _commit(
        repo,
        ".lockstep/lockstep.py",
        "lockstep = 'rebound'\n",
        f"feat: rebind Validate\n\n{ACKNOWLEDGEMENT}: first run of this is main, and I will watch it.",
    )

    found = unexercised(repo, base="main")

    assert found is not None
    assert found.cleared is True
    assert found.acknowledged == "first run of this is main, and I will watch it."


def test_gate_cfg_4_a_trailer_with_no_reason_does_not_clear_it(repo: Path) -> None:
    """An empty acknowledgement is the trailer as a formality. The point is the sentence."""
    _commit(
        repo,
        ".lockstep/lockstep.py",
        "lockstep = 'rebound'\n",
        f"feat: rebind Validate\n\n{ACKNOWLEDGEMENT}:",
    )

    found = unexercised(repo, base="main")

    assert found is not None
    assert found.cleared is False


def test_gate_cfg_4_a_trailer_on_an_earlier_commit_in_the_range_still_clears_it(repo: Path) -> None:
    """The range, not the tip. Somebody acknowledges once and then keeps working."""
    _commit(
        repo,
        ".lockstep/lockstep.py",
        "lockstep = 'rebound'\n",
        f"feat: rebind Validate\n\n{ACKNOWLEDGEMENT}: said once.",
    )
    _commit(repo, "src.py", "x = 3\n", "fix: a follow-up with no trailer")

    found = unexercised(repo, base="main")
    assert found is not None
    assert found.acknowledged == "said once."


def test_a_trailer_on_the_base_branch_does_not_clear_a_later_change(repo: Path) -> None:
    """An acknowledgement is about the change carrying it. One left on `main` months ago must not
    clear every configuration change made after it."""
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "src.py", "x = 9\n", f"chore: old work\n\n{ACKNOWLEDGEMENT}: from another change.")
    _git(repo, "checkout", "-q", "change")
    _git(repo, "rebase", "-q", "main")
    _commit(repo, ".lockstep/lockstep.py", "lockstep = 'rebound'\n", "feat: rebind Validate")

    assert acknowledgement(repo, "main") == ""
    found = unexercised(repo, base="main")
    assert found is not None and found.cleared is False


def test_the_diff_is_against_the_merge_base_not_the_moving_base_branch(repo: Path) -> None:
    """Three dots. `main..HEAD` would report a file somebody else changed on `main` as this
    change's, and the report would name a path the author never touched."""
    _commit(repo, "src.py", "x = 2\n", "feat: ordinary work")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, ".lockstep/lockstep.py", "lockstep = 'somebody else'\n", "feat: their rebind")
    _git(repo, "checkout", "-q", "change")

    assert changed_paths(repo, "main") == ("src.py",)
    assert unexercised(repo, base="main") is None


def test_the_workflows_path_filter_is_the_same_list_the_framework_filters_on() -> None:
    """One fact written twice: a `paths:` in a YAML file nothing type-checks, and a tuple in
    Python nothing reads it from. Nothing but this would notice them parting, and a filter that
    fires on a path the framework ignores is a job that runs and reports nothing."""
    import yaml

    loaded = yaml.safe_load(WORKFLOW.read_text())
    # `on:` is the YAML 1.1 boolean `True` once parsed, which is why this is not `loaded["on"]`.
    assert tuple(loaded[True]["pull_request"]["paths"]) == TRUSTED_REF_FILES


def test_the_check_runs_on_a_workflow_holding_no_write_access_and_no_credential() -> None:
    """It executes nothing of the change's and needs nothing. A job that holds a token in order to
    report that something was not run is a job that has acquired reach it cannot justify."""
    import yaml

    loaded = yaml.safe_load(WORKFLOW.read_text())
    assert loaded["permissions"] == {"contents": "read"}
    job = loaded["jobs"]["exercised"]
    assert "permissions" not in job, "the workflow's own grant is already the narrowest"
    checkout = next(step for step in job["steps"] if "checkout" in str(step.get("uses", "")))
    assert checkout["with"]["fetch-depth"] == 0, "the base has to be a commit here"
    assert checkout["with"]["persist-credentials"] is False


def test_the_command_says_the_working_tree_is_the_subject_outside_ci(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A person on their own checkout IS the subject, so there is nothing to protect against and
    nothing to report. O3: the same command, a different true answer."""
    for name in ("GITHUB_ACTIONS", "GITLAB_CI"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(main, ["config"])

    assert result.exit_code == 0, result.output
    assert "local working tree" in result.output


def test_the_command_exits_non_zero_and_names_the_trailer_that_clears_it(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remedy travels with the refusal, because this one is red on a change whose author has
    done nothing wrong."""
    _commit(repo, ".lockstep/lockstep.py", "lockstep = 'rebound'\n", "feat: rebind Validate")
    for name in ("GITHUB_ACTIONS", "GITLAB_CI"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(main, ["config", "--base", "main"])

    assert result.exit_code == 1
    assert ".lockstep/lockstep.py" in result.output
    assert f"{ACKNOWLEDGEMENT}: <why" in result.output


def test_the_command_refuses_when_it_is_reviewing_and_has_no_base_to_compare_against(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused rather than answered green. `changed_paths` returns nothing for an unreadable base,
    so a silent fall-through here would be a passing check about a question nobody asked."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(main, ["config"])

    assert result.exit_code != 0
    assert "no base ref" in result.output
