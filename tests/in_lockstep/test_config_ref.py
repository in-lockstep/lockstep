"""GATE-CFG-1/2 — configuration does not come from the change under review."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from in_lockstep.config_ref import ConfigRef, UntrustedConfig, read_config, resolve


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()

    def run(*args: str) -> None:
        subprocess.run(args, cwd=root, capture_output=True, check=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.test")
    run("git", "config", "user.name", "t")
    (root / "lockstep.py").write_text("POLICY = 'strict'\n")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "base")
    run("git", "branch", "-M", "main")
    return root


def test_gate_cfg_1_a_modified_config_in_the_head_tree_has_no_effect(tmp_path: Path) -> None:
    """The fork simulation: the change under review rewrites its own constraints, and is ignored."""
    root = _repo(tmp_path)

    def run(*args: str) -> None:
        subprocess.run(args, cwd=root, capture_output=True, check=True)

    run("git", "checkout", "-q", "-b", "attacker")
    (root / "lockstep.py").write_text("POLICY = 'wide-open'\n")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "loosen everything")

    loaded = read_config(root, "lockstep.py", ConfigRef.base("main"))
    assert loaded == "POLICY = 'strict'\n"
    assert "wide-open" not in (loaded or ""), (
        "config must come from the trusted ref, not the ref being reviewed"
    )


def test_loading_config_from_the_reviewed_ref_is_refused(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    with pytest.raises(UntrustedConfig, match="rewrite its own constraints"):
        read_config(root, "lockstep.py", ConfigRef.under_review("attacker"))


def test_a_review_without_a_base_has_no_trusted_source(tmp_path: Path) -> None:
    with pytest.raises(UntrustedConfig, match="needs a base ref"):
        resolve(reviewing=True)


def test_local_development_reads_the_working_tree(tmp_path: Path) -> None:
    """Not reviewing anything means the working tree IS the subject; nothing to protect against."""
    root = _repo(tmp_path)
    (root / "lockstep.py").write_text("POLICY = 'local edit'\n")
    assert read_config(root, "lockstep.py", resolve()) == "POLICY = 'local edit'\n"
