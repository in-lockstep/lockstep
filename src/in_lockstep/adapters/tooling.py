"""Where the repository's tools are, resolved for the repository rather than for this process.

The deterministic adapters bind to what a repository already has: its pytest, its ruff, its
`make`. Which interpreter runs pytest used to be `sys.executable`, on the reasoning that the
interpreter running this process is the one whose environment was set up for the repository.
That is true under `uv run in-lockstep` inside the checkout, which is how the suite exercises it,
and false for every installed copy: `uv tool install` gives the tool an isolated interpreter that
holds `in-lockstep` and its dependencies and nothing else, so `<tool-python> -m pytest` found no
pytest and `ruff` found nothing on the sandbox's PATH. Two first-time users saw "ruff is not
installed" and "test failed" about repositories that had both (#167).

So the order is the repository's own environment first, this process when it lives inside the
repository, PATH, and this process again only when PATH has nothing at all. Nothing is guessed past
that: a tool that is nowhere is a refusal that names every place it looked, which `doctor` reports
before a run and `ls` shows beside the binding.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from ..core.types import VENV_BIN, Resolution

__all__ = ["REPOSITORY_VENV", "VENV_BIN", "binary", "interpreter"]

#: The `how` of a tool found in the repository's own environment, which is the one answer an
#: adapter substitutes for a bare name at run time: PATH would find the same binary the sandbox's
#: PATH does, but the venv is on nobody's PATH unless it is activated.
REPOSITORY_VENV = "the repository's .venv"

_WINDOWS = os.name == "nt"
_EXE = ".exe" if _WINDOWS else ""
# `VENV_BIN` lives in `core.types`, where detection can name the same layout; it is re-exported
# here because this is the module that looks there.


def interpreter(root: str | None, sandbox: object) -> Resolution:
    """The Python that runs the repository's suite.

    A containerized run resolves the name inside the image, where this host's filesystem says
    nothing about what exists, so the plain name travels and nothing is probed.
    """
    if _containerized(sandbox):
        return Resolution("python", "python", "resolved inside the container image")
    tried: list[str] = []
    top = _absolute(root)
    if top is not None:
        candidate = top.joinpath(*VENV_BIN, f"python{_EXE}")
        if _runnable(candidate):
            return _python(str(candidate), REPOSITORY_VENV, tried)
        tried.append(str(candidate))
        # `uv run` in the checkout, tox, a virtualenv made somewhere under the tree: the process
        # is inside the repository, so its interpreter is the repository's. Judged by where the
        # interpreter file sits, not where its symlink points: a venv's python is a link to the
        # base interpreter outside the tree, and resolving it made this branch never fire.
        if sys.executable and _within(sys.executable, top):
            return _python(sys.executable, "this process, which runs inside the repository", tried)
        if sys.executable:
            tried.append(f"{sys.executable} (this process, outside the repository)")
    for name in ("python", "python3"):
        found = shutil.which(name)
        tried.append(f"{name} on PATH")
        if found:
            return _python(found, f"{name} on PATH", tried)
    # Last, because it is the tool's own interpreter, which an installed copy has and the
    # repository's pytest is not in. Better than nothing only when PATH has nothing.
    if sys.executable:
        return _python(sys.executable, "this process, because PATH has no python at all", tried)
    return Resolution("python", None, "nowhere", tuple(tried))


def binary(
    name: str, root: str | None, sandbox: object, *, beside: str | None = None, probe: tuple[str, ...] = ()
) -> Resolution:
    """A tool binary such as `ruff`, `make` or `npm`: the repository's .venv, then beside the
    interpreter the suite runs on, then PATH.

    A name with a directory in it (`./gradlew`, `scripts/test.sh`, `/usr/bin/make`) is the
    caller's own path and is not looked for anywhere else: a relative one is relative to the
    working directory the sandbox runs in, which is not necessarily this process's, so it is
    reported as bound rather than probed. `probe` is the argv tail that proves the tool works,
    for `doctor`; empty for a tool that may not answer `--version` politely.
    """
    if _containerized(sandbox):
        return Resolution(name, name, "resolved inside the container image")
    if os.sep in name or (os.altsep and os.altsep in name):
        if os.path.isabs(name):
            if _runnable(Path(name)):
                return Resolution(name, name, "as bound", (), probe=(name, *probe) if probe else ())
            return Resolution(name, None, "nowhere", (name,))
        return Resolution(name, name, "as bound, relative to the working directory the command runs in")
    tried: list[str] = []
    top = _absolute(root)
    if top is not None:
        candidate = top.joinpath(*VENV_BIN, f"{name}{_EXE}")
        if _runnable(candidate):
            return Resolution(
                name, str(candidate), REPOSITORY_VENV, tuple(tried), probe=_with(candidate, probe)
            )
        tried.append(str(candidate))
    if beside:
        candidate = Path(beside).parent / f"{name}{_EXE}"
        if _runnable(candidate):
            return Resolution(
                name,
                str(candidate),
                "beside the interpreter the suite runs on",
                tuple(tried),
                probe=_with(candidate, probe),
            )
        tried.append(str(candidate))
    found = shutil.which(name)
    tried.append(f"{name} on PATH")
    if found:
        return Resolution(name, found, f"{name} on PATH", tuple(tried), probe=_with(Path(found), probe))
    return Resolution(name, None, "nowhere", tuple(tried))


def _python(path: str, how: str, tried: list[str]) -> Resolution:
    # `-I`: isolated, so the probe imports pytest from the interpreter's own site-packages and
    # not from a `pytest.py` sitting in whatever directory `doctor` was run in.
    return Resolution("python", path, how, tuple(tried), probe=(path, "-I", "-c", "import pytest"))


def _with(path: Path, probe: tuple[str, ...]) -> tuple[str, ...]:
    return (str(path), *probe) if probe else ()


def _containerized(sandbox: object) -> bool:
    runtime = getattr(sandbox, "runtime", None)
    return bool(getattr(sandbox, "image", "")) and callable(runtime) and bool(runtime())


def _absolute(root: str | None) -> Path | None:
    """The root as an absolute path, so a relative `cwd=` binding yields an argv the sandbox can
    exec from any working directory, and one `ls` prints that a person can open."""
    if not root:
        return None
    try:
        return Path(root).resolve()
    except OSError:  # pragma: no cover - a root that cannot be resolved is not the repository
        return None


def _runnable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _within(executable: str, top: Path) -> bool:
    """Whether the interpreter file lives under the root. Its directory is resolved (so a root
    reached through a symlink such as macOS's /tmp still matches) and its name is not: a venv's
    python is itself a symlink to the base interpreter, and following it would say every venv
    lives outside the tree."""
    where = Path(executable)
    try:
        return (where.parent.resolve() / where.name).is_relative_to(top)
    except OSError:  # pragma: no cover
        return False
