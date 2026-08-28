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
from pathlib import Path
from typing import Any

from .config_ref import ConfigRef, read_config, resolve

MODULE_NAME = "lockstep"
MODULE_FILE = "lockstep.py"


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
    if source is None:
        raise NoLifecycle(
            f"no {MODULE_FILE} at {ref.reason}. Running on detected defaults; "
            f"`in-lockstep init` scaffolds one."
        )

    if not ref.ref:
        path = Path(root) / MODULE_FILE
        return _import_from(path), ref

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
