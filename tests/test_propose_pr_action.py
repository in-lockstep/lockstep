"""The action that turns generated files into a pull request, run as shell against a real repo.

Every other test in this suite reads what the compiler *emitted*. This one runs what the runner
actually executes, because the defect it guards was invisible to emission: the workflow was valid,
the action was valid, and the two together produced a branch name git refuses. That is the shape of
every defect in #18 — valid, and not working.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ACTION = Path(__file__).resolve().parents[1] / "actions" / "propose-pr" / "action.yml"

# What `_emit_run_step` in the orchestrator passes. `base` and `reuse-branch` are absent, so they
# take the action's declared defaults — which is the whole point of the distinction under test.
COMPILER_PASSES = {
    "source": "outputs/change",
    "destination": ".",
    # The command declares `branch` with `default: ""`, and a parameter default that is empty is not
    # appended to the expansion chain. So the expression resolves to the empty string, and Actions
    # hands the action an empty input rather than no input at all.
    "branch": "",
    "title": "Implement #30",
    "labels": "lockstep,needs-review",
    "issue-from": "outputs/issue.json",
}


def action_script(overrides: dict[str, str]) -> str:
    """The `propose` step's shell, with the expressions Actions would have substituted."""
    definition = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    declared = definition["inputs"]
    values = {name: str(spec.get("default", "")) for name, spec in declared.items()}
    values.update(overrides)

    step = next(s for s in definition["runs"]["steps"] if s.get("id") == "propose")
    script = step["run"]
    for name, value in values.items():
        script = script.replace("${{ inputs." + name + " }}", value)
    script = script.replace("${{ github.token }}", "unused-in-this-test")

    leftover = re.findall(r"\$\{\{[^}]*\}\}", script)
    assert not leftover, f"the test did not substitute {leftover}"
    return script


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A checkout with an origin to push to, and a generated tree waiting to be proposed."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)

    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args: str) -> None:
        subprocess.run(args, cwd=repo, check=True, capture_output=True)

    run("git", "init", "-q", "-b", "main", ".")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (repo / "README.md").write_text("existing\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "initial")
    run("git", "remote", "add", "origin", str(origin))
    run("git", "push", "-q", "origin", "main")

    generated = repo / "outputs" / "change" / "src"
    generated.mkdir(parents=True)
    (generated / "answer.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "outputs" / "issue.json").write_text('{"key": "#30"}\n', encoding="utf-8")

    # `gh pr create` is the only thing here that would reach the network. Stubbed rather than
    # skipped: the branch it is handed is exactly what this test exists to check.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" >> "$GH_CALLS"\n'
        'case "$*" in *"pr create"*) echo "https://example.invalid/pr/1";; *) : ;; esac\n',
        encoding="utf-8",
    )
    gh.chmod(0o755)
    return repo


def propose(workspace: Path, overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PATH": f"{workspace.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(workspace.parent / "step-output"),
        "GITHUB_STEP_SUMMARY": str(workspace.parent / "step-summary"),
        "GH_CALLS": str(workspace.parent / "gh-calls"),
        "GITHUB_RUN_ID": "32792379720",
        "GITHUB_REF_NAME": "main",
        "GITHUB_WORKFLOW": "implement",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "in-lockstep/lockstep",
    }
    for path in ("step-output", "step-summary", "gh-calls"):
        (workspace.parent / path).write_text("", encoding="utf-8")
    script = action_script({**COMPILER_PASSES, **(overrides or {})})
    return subprocess.run(["bash", "-c", script], cwd=workspace, env=env, capture_output=True, text=True)


def pushed_branches(workspace: Path) -> list[str]:
    out = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        cwd=workspace.parent / "origin.git",
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


def test_an_empty_branch_input_falls_back_to_the_declared_default(workspace: Path) -> None:
    """`branch: ""` is what the compiler passes, and it must not become the branch name.

    Without the fallback this builds `-32792379720`, and `git checkout -b` rejects it:
    "fatal: '-32792379720' is not a valid branch name". The run that hit it had already paid for
    four agents.
    """
    result = propose(workspace)
    assert result.returncode == 0, result.stderr

    branches = [name for name in pushed_branches(workspace) if name != "main"]
    assert branches == ["pipeline/generated-32792379720"]

    subprocess.run(
        ["git", "check-ref-format", "--branch", branches[0]],
        check=True,
        capture_output=True,
    )


def test_an_explicit_branch_prefix_still_wins(workspace: Path) -> None:
    result = propose(workspace, {"branch": "pipeline/implement"})
    assert result.returncode == 0, result.stderr
    assert "pipeline/implement-32792379720" in pushed_branches(workspace)


def test_the_generated_files_land_where_the_destination_says(workspace: Path) -> None:
    result = propose(workspace)
    assert result.returncode == 0, result.stderr

    show = subprocess.run(
        ["git", "show", "--stat", "--format=%B", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "src/answer.py" in show.stdout
    # The commit has to carry the tracker key or the work is untraceable, which the action refuses.
    assert "Issue: #30" in show.stdout


def test_the_pipeline_working_directory_does_not_land_in_the_pull_request(workspace: Path) -> None:
    """`destination: .` means `git add -A .`, which sees `outputs/` sitting right there.

    Every intermediate the run produced — the fetched issue, the plan, the scan reports, and a
    second copy of the generated tree — is untracked in the same checkout. Whether they end up in
    the proposal is decided entirely by whether the repository ignores the output directory.
    """
    result = propose(workspace)
    assert result.returncode == 0, result.stderr

    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "src/answer.py" in committed
    assert not [path for path in committed if path.startswith("outputs/")], committed


def test_a_run_that_generated_nothing_opens_no_pull_request(workspace: Path) -> None:
    for path in sorted((workspace / "outputs" / "change").rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()

    result = propose(workspace)
    assert result.returncode == 0, result.stderr
    assert "nothing was generated" in result.stdout
    assert pushed_branches(workspace) == ["main"]
