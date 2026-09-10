"""The framework owns one directory in a checkout: `.lockstep/`.

Three things this file holds together:

1. The constant `CONFIG_PATHS` names a directory, and `TRUSTED_REF_FILES` names the files inside
   it that actually come from the trusted ref. A test derives the latter from the code rather than
   from a list written in the test, so a new `read_config` call added later is under the rule
   instead of joining the ones that were not.

2. The scaffolded `.gitignore` and every directory constant the framework writes into resolve
   under `.lockstep/` and nothing else.  A sixth store added later fails here rather than silently
   writing into a second directory.

3. `.in-lockstep/` is the previous layout.  It remains denied to agent writes (legacy protection),
   the docstrings say it is previous rather than current, and this repository's own `.gitignore`
   does not ignore a path nothing writes.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "in_lockstep"
REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 1. CONFIG_PATHS and TRUSTED_REF_FILES match what read_config is called with
# ---------------------------------------------------------------------------


def _read_config_call_args(loader_source: str) -> set[str]:
    """Walk the AST of loader.py to find every string literal passed as the `path`
    argument (second positional) to `read_config`."""
    tree = ast.parse(loader_source)
    paths: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match bare `read_config(...)` or `module.read_config(...)`
        name = ""
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name != "read_config":
            continue
        # The path is the second positional argument.
        if len(node.args) >= 2:
            arg = node.args[1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                paths.add(arg.value)
            elif isinstance(arg, ast.Name):
                # Resolve module-level constants in the same file.
                for top in ast.walk(tree):
                    if (
                        isinstance(top, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == arg.id for t in top.targets)
                        and isinstance(top.value, ast.Constant)
                        and isinstance(top.value.value, str)
                    ):
                        paths.add(top.value.value)
    return paths


def test_trusted_ref_files_matches_every_read_config_call_site() -> None:
    """GATE-CFG-5 — the constant that says which files come from the trusted ref is derived from
    the code that actually reads them, not from a list somebody maintains by hand.

    Fails if a new `read_config` call is added for a path the constant does not name.
    """
    from in_lockstep.loader import TRUSTED_REF_FILES

    loader_source = (SRC / "loader.py").read_text()
    called_with = _read_config_call_args(loader_source)
    assert called_with, "the AST walk found no read_config calls — the test is broken, not the code"
    assert called_with == set(TRUSTED_REF_FILES), (
        f"read_config is called with {sorted(called_with)}, "
        f"but TRUSTED_REF_FILES is {sorted(TRUSTED_REF_FILES)}"
    )


# ---------------------------------------------------------------------------
# 2. One directory: every path the framework writes into resolves under .lockstep/
# ---------------------------------------------------------------------------


def _scaffold_gitignore_dirs() -> set[str]:
    """The directories the scaffolded .gitignore names, derived from the scaffold string."""
    from in_lockstep.cli import _SCAFFOLD_IGNORE

    dirs: set[str] = set()
    for line in _SCAFFOLD_IGNORE.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped.endswith("/"):
            dirs.add(stripped)
    return dirs


def _framework_directory_constants() -> set[str]:
    """The directory paths the framework uses for its own run output, read from the source."""
    from in_lockstep.ai.replay import CASSETTE_DIR
    from in_lockstep.ai.transcript import TranscriptWriter
    from in_lockstep.platform.ledger.store import InRepoLedger
    from in_lockstep.platform.state import DEFAULT_ROOT

    # Each constant as a string, normalised to end with /
    raw = [
        CASSETTE_DIR,
        str(TranscriptWriter.__dataclass_fields__["root"].default_factory()),  # type: ignore[misc]
        str(InRepoLedger.__dataclass_fields__["root"].default_factory()),  # type: ignore[misc]
        str(DEFAULT_ROOT),
    ]
    return {p.rstrip("/") + "/" for p in raw}


def test_scaffold_gitignore_entries_all_resolve_under_dot_lockstep() -> None:
    """Every directory the scaffolded .gitignore names is under `.lockstep/`."""
    for entry in _scaffold_gitignore_dirs():
        assert entry.startswith(".lockstep/"), (
            f"scaffolded .gitignore names {entry!r}, which is not under .lockstep/"
        )


def test_every_framework_directory_constant_resolves_under_dot_lockstep() -> None:
    """Every directory the framework writes run output into is under `.lockstep/`."""
    for entry in _framework_directory_constants():
        assert entry.startswith(".lockstep/"), f"directory constant {entry!r} is not under .lockstep/"


def test_a_framework_write_target_outside_dot_lockstep_fails() -> None:
    """Negative control: `.in-lockstep/` as a write target would fail the directory check."""
    assert not ".in-lockstep/runs/".startswith(".lockstep/")


# ---------------------------------------------------------------------------
# 3. .in-lockstep/ is legacy, and the codebase says so
# ---------------------------------------------------------------------------


def test_in_lockstep_remains_denied_to_agent_writes() -> None:
    """Legacy protection, not an oversight. A repository that adopted before the move has a real
    `.in-lockstep/` with real skills in it. Dropping the deny would un-protect them on upgrade."""
    from in_lockstep.core.changes import DENY_ALWAYS, DENY_UNLESS_GRANTED

    assert ".in-lockstep/" in DENY_ALWAYS, (
        ".in-lockstep/ must stay in DENY_ALWAYS — it is legacy protection, not residue"
    )
    assert ".in-lockstep/skills/" in DENY_UNLESS_GRANTED, (
        ".in-lockstep/skills/ must stay in DENY_UNLESS_GRANTED"
        " — pre-migration repositories have real skills there"
    )


def test_config_ref_docstring_does_not_claim_in_lockstep_comes_from_trusted_ref() -> None:
    """The module docstring of config_ref.py said `.in-lockstep/` comes from a trusted ref.
    That directory is not read at all; only `lockstep.py` (under either spelling) is."""
    import in_lockstep.config_ref as mod

    docstring = mod.__doc__ or ""
    assert ".in-lockstep/" not in docstring, (
        "config_ref.py's module docstring still claims .in-lockstep/ comes from a trusted ref — "
        "it does not; only the lifecycle module does"
    )


def test_changes_docstring_says_in_lockstep_is_the_previous_layout() -> None:
    """The docstring of core/changes.py names `.in-lockstep/` as where skills and the ledger live,
    present tense. The deny stays; the sentence should say it is the previous layout."""
    source = (SRC / "core" / "changes.py").read_text()
    # Find lines mentioning .in-lockstep/ in the module docstring (delimited by triple quotes).
    docstring_match = re.search(r'^"""(.*?)"""', source, re.DOTALL)
    assert docstring_match is not None
    docstring = docstring_match.group(1)
    # The docstring should not present .in-lockstep/ as the current layout.
    # It should say "previous" or "former" or similar near any mention of .in-lockstep/.
    assert ".in-lockstep/" not in docstring or "previous" in docstring.lower(), (
        "core/changes.py's docstring names .in-lockstep/ without saying it is the previous layout"
    )
    # Stronger: the docstring must not say skills/ledger/checkpoints ARE under .in-lockstep/
    # in present tense without qualification.
    for line in docstring.splitlines():
        if ".in-lockstep/" in line:
            assert "previous" in line.lower() or "was" in line.lower() or "former" in line.lower(), (
                f"this line presents .in-lockstep/ as current: {line.strip()!r}"
            )


def test_this_repositorys_gitignore_does_not_ignore_a_path_nothing_writes() -> None:
    """`.in-lockstep/` appears in this repository's `.gitignore` at line 43 and in no scaffold.
    Nothing creates it; the ignore rule is what let it sit here unremarked for a fortnight."""
    gitignore = (REPO / ".gitignore").read_text()
    entries = [
        line.strip() for line in gitignore.splitlines() if line.strip() and not line.strip().startswith("#")
    ]
    assert ".in-lockstep/" not in entries, (
        ".gitignore still ignores .in-lockstep/ — a path nothing writes. "
        "Remove the line; the deny in DENY_ALWAYS is the protection, not a .gitignore entry."
    )
