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


# -- the CI checkout, which is where this control does its only work ---------------------------


def _origin_repo(tmp_path: Path, name: str = "r") -> Path:
    import subprocess

    root = tmp_path / name
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "lockstep.py").write_text("lockstep = 'from the trusted ref'\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "one"],
        cwd=root,
        check=True,
    )
    return root


def test_a_detached_checkout_with_no_local_branch_still_finds_the_ref(tmp_path: Path) -> None:
    """The condition an `actions/checkout` working directory is actually in.

    It is a detached HEAD with `origin/main` and no local `main`, so `git show main:lockstep.py`
    fails for a reason that has nothing to do with the file. This control replaces gh-aw's
    workflow-file provenance and spent a CI run silently not applying because of it.
    """
    import subprocess

    origin = _origin_repo(tmp_path, "origin")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    subprocess.run(["git", "checkout", "-q", "--detach", "origin/main"], cwd=clone, check=False)
    subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=clone, check=True)
    for branch in ("main", "master"):
        subprocess.run(["git", "branch", "-D", branch], cwd=clone, capture_output=True)

    probe = subprocess.run(["git", "rev-parse", "--verify", "main"], cwd=clone, capture_output=True)
    assert probe.returncode != 0, "the fixture is not in the CI condition this test is about"
    source = read_config(clone, "lockstep.py", ConfigRef.base("main"))
    assert source is not None and "trusted ref" in source


def test_an_unresolvable_ref_refuses_rather_than_returning_none(tmp_path: Path) -> None:
    """Failing open is the defect. "Unreadable" and "absent" produced the same `None`.

    Both then fell through to detected defaults, and a review ran with none of the repository's
    bindings, policy or egress decisions — while the crosswalk recorded the control as replaced.
    """
    from in_lockstep.config_ref import UnresolvableConfigRef

    root = _origin_repo(tmp_path)
    with pytest.raises(UnresolvableConfigRef) as exc:
        read_config(root, "lockstep.py", ConfigRef.base("no-such-branch"))
    assert "does not name a commit" in str(exc.value)
    assert "fetch-depth" in str(exc.value), "the message has to say what to do about it"


def test_a_resolvable_ref_without_config_is_not_an_error(tmp_path: Path) -> None:
    """The adoption case: the first pull request is the one that ADDS lockstep.py.

    Distinguishing this from an unreadable ref is the whole point of the change — refusing here
    would make adopting the framework impossible.
    """
    import subprocess

    root = _origin_repo(tmp_path)
    (root / "lockstep.py").unlink()
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "remove"],
        cwd=root,
        check=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert read_config(root, "lockstep.py", ConfigRef.base(head)) is None


def test_an_already_qualified_ref_is_taken_as_written() -> None:
    """`origin/origin/main` is not a spelling of anything."""
    from in_lockstep.config_ref import _candidates

    assert _candidates("main") == ("main", "origin/main")
    assert _candidates("origin/main") == ("origin/main",)
    assert _candidates("refs/heads/main") == ("refs/heads/main",)
