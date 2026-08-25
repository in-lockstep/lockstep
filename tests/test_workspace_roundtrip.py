"""What `save` publishes must come back where `restore` puts it.

The two actions are a pair and neither is meaningful alone: `save` uploads an artifact rooted at the
common ancestor of what it matched, and `restore` extracts unconditionally into `$OUTPUT_DIR`. They
agree only when the saved path *is* the output directory, and nothing in the compiler, the drift
gate or the shipped tests looked at whether that held.

It did not. An `/implement` run saving `outputs/change/src` got an artifact rooted at
`outputs/change`, so the tree came back as `outputs/src` — and `outputs/change`, which is what the
pull request is built from, held only the file a deterministic step had written there directly. The
run reported success and would have proposed a plan with no change attached.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ACTIONS = Path(__file__).resolve().parents[1] / "actions"


def slice_script(inputs: dict[str, str]) -> str:
    """The `save` action's path-resolution step, as Actions would have substituted it."""
    definition = yaml.safe_load((ACTIONS / "save" / "action.yml").read_text(encoding="utf-8"))
    declared = definition["inputs"]
    values = {name: str(spec.get("default", "")) for name, spec in declared.items()}
    values.update(inputs)
    step = next(s for s in definition["runs"]["steps"] if s.get("id") == "slice")
    script = step["run"]
    for name, value in values.items():
        script = script.replace("${{ inputs." + name + " }}", value)
    return script


def resolve(tmp_path: Path, paths: str) -> dict[str, str]:
    """Run the resolution step and read back what it wrote to $GITHUB_OUTPUT."""
    output = tmp_path / "step-output"
    output.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "GITHUB_OUTPUT": str(output),
        "OUTPUT_DIR": "outputs",
        "GITHUB_RUN_ID": "1",
        "GITHUB_JOB": "j",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    result = subprocess.run(
        ["bash", "-c", slice_script({"paths": paths})],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    resolved: dict[str, str] = {}
    lines = output.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        key, _, value = lines[index].partition("=")
        if value == "" and key.endswith("<<EOF"):
            key = key[: -len("<<EOF")]
            index += 1
            block = []
            while lines[index] != "EOF":
                block.append(lines[index])
                index += 1
            resolved[key] = "\n".join(block)
        else:
            resolved[key] = value
        index += 1
    return resolved


def common_ancestor(paths: list[str]) -> str:
    """Where an uploader would root the artifact: the deepest directory containing everything.

    An approximation of `actions/upload-artifact`, not a reimplementation of it — for a single file
    this returns the file rather than its directory. What the tests below assert is the property
    that matters either way: whether the root lands on the output directory or somewhere under it.
    """
    return os.path.commonpath([str(Path(p)) for p in paths])


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    generated = tmp_path / "outputs" / "change" / "src" / "lockstep" / "emit"
    generated.mkdir(parents=True)
    (generated / "context.py").write_text("VALUE = 1\n", encoding="utf-8")
    return tmp_path


def test_a_nested_save_still_roots_the_artifact_at_the_output_directory(workspace: Path) -> None:
    """The round-trip: root must be `outputs`, so `restore` into `outputs` rebuilds the same tree."""
    resolved = resolve(workspace, "outputs/change/src")
    matched = [
        str(path.relative_to(workspace))
        for entry in resolved["paths"].splitlines()
        for path in ((workspace / entry).rglob("*") if (workspace / entry).is_dir() else [workspace / entry])
        if path.is_file()
    ]
    assert matched, resolved

    root = common_ancestor(matched)
    assert root == "outputs", f"artifact would be rooted at {root!r}, so restore misplaces the tree"

    # What restore would rebuild, given entries relative to that root.
    restored = sorted("outputs/" + os.path.relpath(path, root) for path in matched)
    assert "outputs/change/src/lockstep/emit/context.py" in restored


def test_saving_the_output_directory_itself_is_unchanged(workspace: Path) -> None:
    """The common case has always worked, and must keep working."""
    resolved = resolve(workspace, "")
    assert resolved["empty"] == "false"
    assert "outputs" in resolved["paths"].splitlines()


def test_a_slice_with_nothing_in_it_is_still_empty(tmp_path: Path) -> None:
    """The marker must not make every slice look non-empty."""
    (tmp_path / "outputs" / "change" / "src").mkdir(parents=True)
    assert resolve(tmp_path, "outputs/change/src")["empty"] == "true"
