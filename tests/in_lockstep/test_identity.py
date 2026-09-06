"""An opt-in local identity, so the runs a team mostly makes can be compared (GATE-TEAM-2).

A run carried an identity only under CI or when a grant was needed, so every local run sat under
`—` in every report and in no spread. #164 named the remedy and refused to make it a default: a
report that named people who never chose to be named is the leaderboard the pseudonyms exist to
refuse. So it is one line a repository writes, `lockstep.identity = GitAuthor()`, and what the
line records is the configured git author -- configured, because an author git invents from a
hostname is a fact about a machine and not a claim a person made.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from in_lockstep import GitAuthor
from in_lockstep.cli import _provenance
from in_lockstep.lockstep import Lockstep
from in_lockstep.platform.identity import Identity


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A git repository that sees no global or system configuration.

    Without the redirect, `git config --get user.name` in a fresh repository answers with THIS
    machine's global identity, and the absence half below would be testing whoever ran the suite.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "no-system"))
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "EMAIL"):
        monkeypatch.delenv(var, raising=False)
    for var in [v for v in os.environ if v.startswith("GITHUB_")] + ["GITLAB_CI"]:
        monkeypatch.delenv(var, raising=False)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def _configure(root: Path, **keys: str) -> None:
    for key, value in keys.items():
        subprocess.run(["git", "config", key.replace("_", "."), value], cwd=root, check=True)


def test_gate_team_2_the_identity_is_recorded_in_gits_own_spelling(isolated: Path) -> None:
    """`Name <email>`, the way git writes it on a commit, so the ledger and the log name the
    person the same way."""
    _configure(isolated, user_name="Amy Example", user_email="amy@example.test")
    assert GitAuthor().claim(str(isolated)) == "Amy Example <amy@example.test>"


@pytest.mark.parametrize("configured", [{}, {"user_name": "Amy Example"}, {"user_email": "amy@example.test"}])
def test_gate_team_2_a_half_configured_author_is_nothing_not_a_guess(
    isolated: Path, configured: dict[str, str]
) -> None:
    """Both keys, the same two git requires before it will author a commit. Git would invent the
    missing half from the login and the hostname; an invented identity is not a claim anybody
    made, and the record must say nothing rather than that."""
    _configure(isolated, **configured)
    assert GitAuthor().claim(str(isolated)) == ""


def test_a_root_that_is_not_a_directory_claims_nothing(tmp_path: Path) -> None:
    assert GitAuthor().claim(str(tmp_path / "missing")) == ""


def test_gate_team_2_the_record_carries_the_identity_only_when_the_module_opted_in(isolated: Path) -> None:
    """The opt-in, at the seam the record is built. Nothing detects the line: a module that does
    not write it records exactly what it recorded before, and one that does records the claim
    under its own name, never as the host's."""
    _configure(isolated, user_name="Amy Example", user_email="amy@example.test")
    lockstep = Lockstep.detect(isolated)
    assert lockstep.identity is None, "the default is nobody; a name nobody chose is not recorded"
    assert "identity" not in _provenance(lockstep)

    lockstep.identity = GitAuthor()
    out = _provenance(lockstep)
    assert out["identity"] == "Amy Example <amy@example.test>"
    assert "ci_actor" not in out, "a local run: the host said nothing"


def test_an_identity_that_cannot_say_leaves_the_field_absent(isolated: Path) -> None:
    """Empty is absent, and absent is written nowhere. An empty string under `identity` would be
    an asker called nothing, which every report would then count."""
    lockstep = Lockstep.detect(isolated)
    lockstep.identity = GitAuthor()
    assert "identity" not in _provenance(lockstep)


def test_an_identity_is_whatever_can_make_the_claim(isolated: Path) -> None:
    """`Identity` is a protocol, so a team that names people some other way -- a badge, an SSO
    login read from a file -- binds its own without forking, the same way an adapter does."""

    class Badge:
        def claim(self, root: str) -> str:
            return "badge:1234"

    lockstep = Lockstep.detect(isolated)
    named: Identity = Badge()
    lockstep.identity = named
    assert _provenance(lockstep)["identity"] == "badge:1234"
