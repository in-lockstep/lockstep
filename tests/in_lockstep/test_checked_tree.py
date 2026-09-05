"""Every Python file in this repository is checked by this repository. Discharges `GATE-CFG-3`.

`make check` ran `ruff check src tests` and `mypy src`, so `.lockstep/lockstep.py` — 771 lines
binding every adapter, setting the path tiers and the egress policy, the file this project's own
design calls the most security-sensitive one it has — was covered by neither. Pointing mypy at it
found a function defined twice in about a second. Ruff could not: the first copy is called before
the second is defined, so `F811` correctly stays silent, and only a type checker names it.

O10 says a capability that cannot be dogfooded here is one we are asking adopters to trust on our
word, and the capability in question is *your lifecycle module is checked*. #242 had just made
everything `init` scaffolds pass `mypy --strict`; this repository's own module was not held to it.

The assertions are a ratchet rather than a spot check. A file added outside the checked paths, or
a path quietly dropped from a target, has to turn this red — otherwise the gap simply reopens
somewhere else and nothing says so.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"

# Directories that are not this repository's source: a virtualenv, caches, git's own storage, and
# anything a test wrote under a temporary root that happens to live here.
NOT_OURS = {".venv", "venv", "__pycache__", ".git", ".mypy_cache", ".ruff_cache", ".pytest_cache"}

# Paths a tool knowingly does not reach, per tool, each with the reason. An exemption is a line a
# reviewer sees, which is the property a silent gap never had — and it is per tool because the two
# catch different things: ruff reaches the test suite and mypy does not.
EXEMPT: dict[str, dict[str, str]] = {
    "ruff check": {
        "examples": (
            "The worked examples. ruff passes on all of them today, so this is a scope line rather "
            "than a tolerated failure: they are checked in #247 together with the typecheck half, "
            "because splitting them would leave the harder half unowned."
        ),
    },
    "mypy": {
        "examples": (
            "Two of the four worked examples are named `lockstep.py`, and mypy refuses more than "
            'one module of a given name in one invocation — `Duplicate module named "lockstep"`, '
            "and it stops before checking anything. So `examples` cannot be appended to the "
            "typecheck target; it needs an invocation per file. They do carry real type errors and "
            "an adopter reads them, which is an O8 problem rather than a reason to leave them out. "
            "Filed as #247."
        ),
        "tests": (
            "693 errors across 53 files today. Type-checking the suite is its own project, not a "
            "line on a Makefile target. Recorded here rather than left implicit because "
            "`pyproject.toml` carries a `tests.*` mypy override that reads as though tests were "
            "checked, and nothing exercises it: `mypy src` never looks at them. Filed as #248."
        ),
    },
}

TARGET = re.compile(r"^\t+uv run (ruff (?:check|format)|mypy) ([^\n|]+)$", re.M)


def _targets() -> dict[str, list[str]]:
    """What each tool is pointed at, read off the Makefile rather than restated here."""
    found: dict[str, list[str]] = {}
    for tool, args in TARGET.findall(MAKEFILE.read_text()):
        paths = [a for a in args.split() if not a.startswith("-")]
        found.setdefault(tool, []).extend(paths)
    return found


def _python_files() -> list[Path]:
    return sorted(p for p in ROOT.rglob("*.py") if not (set(p.relative_to(ROOT).parts) & NOT_OURS))


def _covered_by(path: Path, roots: list[str]) -> bool:
    """Whether a checked path reaches this file.

    A dot-directory is reached only when it is named, never through an ancestor. mypy's recursive
    walk skips them, so `mypy examples` silently misses `examples/*/.lockstep/lockstep.py` — the
    exact files an adopter reads — while looking like it covered them. A target that passes over
    a file it never opened is the failure mode this whole module exists for, so the rule is
    encoded here rather than trusted.
    """
    relative = path.relative_to(ROOT)
    for root in roots:
        base = Path(root)
        if base == Path("."):
            # A bare `.` reaches nothing hidden, for the same reason.
            if not any(part.startswith(".") for part in relative.parts[:-1]):
                return True
            continue
        if not relative.is_relative_to(base):
            continue
        hidden = [p for p in relative.parts[:-1] if p.startswith(".")]
        if all(p in base.parts for p in hidden):
            return True
    return False


def test_the_repository_has_python_outside_src_and_tests():
    """A positive control. Every assertion below is over a file walk, and a walk that stopped
    matching would make all of them pass over nothing."""
    outside = [
        p
        for p in _python_files()
        if not p.relative_to(ROOT).is_relative_to("src") and not p.relative_to(ROOT).is_relative_to("tests")
    ]
    assert outside, "the walk found no Python outside src/ and tests/, which cannot be right"


@pytest.mark.parametrize("tool", ["ruff check", "mypy"])
def test_every_python_file_is_checked_or_exempt(tool):
    """`GATE-CFG-3`. The ratchet: a new file outside the checked paths fails here.

    Both tools, because they catch different things and this repository has now been bitten by
    exactly that difference — ruff passed on the duplicate definition that mypy named at once.
    """
    roots = _targets()[tool]
    unchecked = [
        str(p.relative_to(ROOT))
        for p in _python_files()
        if not _covered_by(p, roots) and p.relative_to(ROOT).parts[0] not in EXEMPT[tool]
    ]
    assert not unchecked, (
        f"`{tool}` does not reach {unchecked}. Add the path to the Makefile target, or add an "
        f"entry to EXEMPT in {Path(__file__).name} saying why this repository does not check it."
    )


@pytest.mark.parametrize("tool", ["ruff check", "ruff format", "mypy"])
def test_the_lifecycle_module_is_named_and_not_merely_implied(tool):
    """`.lockstep` has to appear in the target itself.

    Both tools skip dot-directories when walking, so a parent path does not reach
    `.lockstep/lockstep.py`. A target that named a parent would look like it covered the module
    and would check nothing — the failure this gate is about, reintroduced by the fix for it.
    """
    assert ".lockstep" in _targets()[tool], (
        f"the {tool} target must name `.lockstep` directly; a parent path does not reach into a "
        f"dot-directory, so it would pass without opening the file"
    )


def test_every_exemption_states_a_reason():
    """An exemption with no reason is a gap with a name.

    The path has to exist too: an exemption for a directory somebody deleted is a hole waiting for
    a directory of that name to come back.
    """
    for tool, paths in EXEMPT.items():
        for path, reason in paths.items():
            assert (ROOT / path).exists(), f"{tool} exempts {path}, which does not exist"
            assert len(reason) > 40, f"{tool} exempts {path} without a reason worth reading"
