"""Pin resolution and ejection.

Both exist to make one thing true: what runs is what was reviewed. Pinning turns a movable tag into
a commit nobody can change underneath you. Ejection is the escape hatch for the rare file the spec
cannot express — recorded, snapshotted, and tracked, so a fork is a decision rather than a drift.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .emit.builtins import EXTERNAL_ACTIONS
from .emit.context import PINS_PATH
from .errors import LockstepError
from .spec.model import Spec

EJECTED_PATH = ".pipeline/ejected.yaml"
EJECT_BASE = ".pipeline/eject-base"


class PinError(LockstepError):
    code = "LS300"


class EjectError(LockstepError):
    code = "LS400"


# --- pinning ---------------------------------------------------------------


def resolve_tag(repo: str, tag: str) -> str:
    """Resolve a tag to the commit it currently points at."""
    owner_repo = "/".join(repo.split("/")[:2])
    url = f"https://github.com/{owner_repo}.git"
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", url, tag],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PinError(f"could not reach {url}: {exc}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise PinError(
            f"no tag {tag!r} in {owner_repo}",
            hint="check capabilities.actions in pipeline.yaml, or pass --sha to pin by hand",
        )
    return result.stdout.split()[0]


def load_pins(root: Path) -> dict[str, Any]:
    path = root / PINS_PATH
    if path.is_file():
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return loaded
    return {"capabilities": {}, "external": {}}


def write_pins(root: Path, data: dict[str, Any]) -> Path:
    path = root / PINS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def pin(
    spec: Spec,
    root: Path,
    *,
    actions_sha: str = "",
    exec_digest: str = "",
    offline: bool = False,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Resolve the manifest's capability tags into the commits and digests that will actually run.

    Resolves everything it can and reports what it could not, rather than aborting on the first
    failure: a placeholder capability repository should not stop the third-party actions from being
    pinned. Returns (pins, notes, unresolved).
    """
    data = load_pins(root)
    capabilities = data.setdefault("capabilities", {})
    notes: list[str] = []
    unresolved: list[str] = []

    repo, _, tag = spec.manifest.capabilities.actions.partition("@")
    repo = repo.removeprefix("github.com/")
    if repo and tag:
        entry = capabilities.setdefault("actions", {})
        entry["repo"] = repo
        entry["tag"] = tag
        if actions_sha:
            entry["sha"] = actions_sha
            notes.append(f"actions: {repo}@{tag} -> {actions_sha[:12]} (supplied)")
        elif offline:
            notes.append("actions: left as-is (offline)")
        else:
            try:
                resolved = resolve_tag(repo, tag)
            except PinError as error:
                unresolved.append(f"actions: {error.message}")
            else:
                if entry.get("sha") and entry["sha"] != resolved:
                    notes.append(
                        f"actions: tag {tag} moved from {entry['sha'][:12]} to "
                        f"{resolved[:12]} — review before committing"
                    )
                entry["sha"] = resolved
                notes.append(f"actions: {repo}@{tag} -> {resolved[:12]}")

    # The compiler also emits third-party actions; leaving those floating would defeat the point.
    external = data.setdefault("external", {})
    for action, tag in sorted(EXTERNAL_ACTIONS.items()):
        entry = external.setdefault(action, {})
        entry["tag"] = tag
        if entry.get("sha"):
            continue
        if offline:
            notes.append(f"{action}: left unpinned (offline)")
            continue
        try:
            entry["sha"] = resolve_tag(action, tag)
        except PinError as error:
            unresolved.append(f"{action}: {error.message}")
        else:
            notes.append(f"{action}@{tag} -> {entry['sha'][:12]}")

    package, _, version = spec.manifest.capabilities.exec.partition("==")
    entry = capabilities.setdefault("exec", {})
    entry["package"] = package or "pipeline-exec"
    if version:
        entry["version"] = version
    entry.setdefault("image", "ghcr.io/pipeline-fw/exec")
    if exec_digest:
        entry["digest"] = exec_digest
        notes.append(f"exec image -> {exec_digest[:19]}")
    elif not entry.get("digest"):
        unresolved.append(
            "exec image: pass --exec-digest with the digest from "
            "`docker buildx imagetools inspect <image>:<tag>`"
        )

    return data, notes, unresolved


def check_pins_current(spec: Spec, root: Path) -> list[str]:
    """Detect a tag that has been moved to a different commit since it was pinned.

    A moved tag is the supply-chain event pinning exists to catch, so it is worth checking on every
    build rather than only at upgrade time.
    """
    data = load_pins(root)
    entry = (data.get("capabilities") or {}).get("actions") or {}
    if not (entry.get("repo") and entry.get("tag") and entry.get("sha")):
        return []
    current = resolve_tag(entry["repo"], entry["tag"])
    if current != entry["sha"]:
        return [
            f"{entry['repo']}@{entry['tag']} now resolves to {current[:12]}, "
            f"but this pipeline is pinned to {entry['sha'][:12]}"
        ]
    return []


# --- ejection --------------------------------------------------------------


@dataclass
class Ejection:
    files: list[str]

    @classmethod
    def load(cls, root: Path) -> Ejection:
        path = root / EJECTED_PATH
        if not path.is_file():
            return cls(files=[])
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(files=[str(entry) for entry in (data.get("files") or [])])

    def save(self, root: Path) -> Path:
        path = root / EJECTED_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Files this repository has taken ownership of. The compiler will not touch them, and\n"
            "# `lockstep compile --check` reports when their source has moved on without them.\n"
            + yaml.safe_dump({"files": sorted(self.files)}, sort_keys=False),
            encoding="utf-8",
        )
        return path


def eject(root: Path, relative: str, generated: str) -> Path:
    """Take ownership of one generated file, snapshotting the generation it forked from."""
    target = root / relative
    if not target.is_file():
        raise EjectError(f"{relative} is not a generated file in this repository")

    registry = Ejection.load(root)
    if relative in registry.files:
        raise EjectError(f"{relative} is already ejected")

    base = root / EJECT_BASE / relative
    base.parent.mkdir(parents=True, exist_ok=True)
    # The pristine generation is the merge base a later `uneject --merge` needs; without it the only
    # options would be keep-mine or take-theirs.
    base.write_text(generated, encoding="utf-8")

    registry.files.append(relative)
    registry.save(root)
    return base


def uneject(root: Path, relative: str) -> None:
    registry = Ejection.load(root)
    if relative not in registry.files:
        raise EjectError(f"{relative} is not ejected")
    registry.files.remove(relative)
    registry.save(root)
    base = root / EJECT_BASE / relative
    if base.is_file():
        base.unlink()


def stale_ejections(root: Path, generated: dict[str, str]) -> list[str]:
    """Ejected files whose generation has moved on since they were forked."""
    stale = []
    for relative in Ejection.load(root).files:
        base = root / EJECT_BASE / relative
        fresh = generated.get(relative)
        if fresh is not None and base.is_file() and base.read_text(encoding="utf-8") != fresh:
            stale.append(relative)
    return stale
