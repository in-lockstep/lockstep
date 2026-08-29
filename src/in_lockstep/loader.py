"""Loading a repository's lifecycle module.

Configuration is code, which means loading it is executing it — and that makes *where it comes
from* a security decision rather than a path question. Under review, the repository root is the
pull request's, so a naive load hands the change being reviewed the file that defines every
binding, policy contribution and path tier meant to constrain reviewing it.

So: the module comes from a trusted ref. When a review is in progress that is the base branch,
materialised to a temporary file; otherwise it is the working tree, because then the working tree
is the subject and there is nothing to defend against.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config_ref import ConfigRef, read_config, resolve

#: What the lifecycle module is called once imported. NOT `lockstep`.
#:
#: `sys.modules["lockstep"] = module` claimed a bare top-level name in the importing project's
#: namespace. Two things went wrong with that and both are silent. A project with its own
#: `lockstep` module got shadowed — or shadowed this one, depending on order. And project code
#: could `import lockstep` and reach the lifecycle definition, which is how framework types start
#: mixing into project code that never meant to depend on them.
#:
#: Namespaced under this package and marked private: it cannot collide, and the name says it is
#: not an import target.
MODULE_NAME = "in_lockstep._lifecycle"

#: Where the module lives. Under `.lockstep/` rather than at the repository root, because the root
#: is on `sys.path` for anything run from there — so a root `lockstep.py` is importable by the
#: project whether or not anyone intended it. A dot-directory is not a valid package name, so
#: nothing under it can be imported by accident.
MODULE_FILE = ".lockstep/lockstep.py"

#: Where it used to live. Kept only so the error can say what to do about it.
LEGACY_MODULE_FILE = "lockstep.py"


class NoLifecycle(Exception):
    """No lockstep.py, which is supported — the CLI falls back to detected defaults."""


def load(
    root: str | Path = ".",
    *,
    base: str = "",
    reviewing: bool = False,
) -> tuple[Any, ConfigRef]:
    """Import the lifecycle module, and say which ref it came from."""
    ref = resolve(base=base, reviewing=reviewing)
    source = read_config(root, MODULE_FILE, ref)
    path = MODULE_FILE

    if source is None:
        # The previous layout, LOADED rather than refused. Refusing was the first attempt and it
        # was wrong in both directions: it broke every existing repository on upgrade, and it
        # cannot be satisfied at all by the pull request that performs the move — configuration
        # comes from the base branch, so the move only exists on the branch being reviewed, and
        # the change would fail its own review forever.
        #
        # Ignoring it would still be the worst answer: running on detected defaults while a
        # working configuration sits unread means everything appears fine with none of the
        # repository's bindings. So it is read, and the ref says loudly where it came from.
        legacy = read_config(root, LEGACY_MODULE_FILE, ref)
        if legacy is None:
            raise NoLifecycle(
                f"no {MODULE_FILE} at {ref.reason}. Running on detected defaults; "
                f"`in-lockstep init` scaffolds one."
            )
        source, path = legacy, LEGACY_MODULE_FILE
        ref = replace(
            ref,
            reason=(
                f"{ref.reason} — via {LEGACY_MODULE_FILE} at the repository root, which is "
                f"DEPRECATED. `mkdir -p .lockstep && git mv {LEGACY_MODULE_FILE} {MODULE_FILE}`. "
                f"The root is on sys.path, so a module there is importable by your project."
            ),
        )

    if not ref.ref:
        return _import_from(Path(root) / path), ref

    # From a git ref: materialise, then import. The file is not in the working tree, and must not
    # be written into it — the working tree belongs to the change under review.
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, prefix="lockstep-trusted-") as handle:
        handle.write(source)
        temp = Path(handle.name)
    try:
        return _import_from(temp), ref
    finally:
        temp.unlink(missing_ok=True)


def _import_from(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise NoLifecycle(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def lockstep_from(module: Any) -> Any:
    """Pull the configured Lockstep out of a loaded module."""
    instance = getattr(module, "lockstep", None)
    if instance is None:
        raise NoLifecycle(
            f"{MODULE_FILE} defines no `lockstep`. The module is expected to construct one and bind to it."
        )
    return instance
