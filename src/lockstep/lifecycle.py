"""Pin resolution and ejection.

Both exist to make one thing true: what runs is what was reviewed. Pinning turns a movable tag into
a commit nobody can change underneath you. Ejection is the escape hatch for the rare file the spec
cannot express — recorded, snapshotted, and tracked, so a fork is a decision rather than a drift.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .emit.builtins import EXTERNAL_ACTIONS
from .emit.context import PINS_PATH
from .errors import LockstepError
from .spec.load import MANIFEST_NAME, find_home
from .spec.model import INHERITED_DIR, LOCKSTEP_DIR, Spec


def fetch(spec: Spec, root: Path) -> list[str]:
    """Materialize everything this repository inherits, at the commit its lock file records.

    A local path is copied rather than cloned, which is what makes developing an upstream and a
    consumer side by side bearable. It is also unpinnable, so `doctor` says so.
    """
    home = _home(root)
    pins = load_pins(root).get("inherits", {}) or {}
    notes: list[str] = []

    for alias, source in sorted(spec.manifest.inherits.items()):
        destination = home / INHERITED_DIR / alias
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)

        if not source.startswith("github.com/"):
            local = (root / source).resolve()
            if not (local / MANIFEST_NAME).is_file() and not (local / LOCKSTEP_DIR).is_dir():
                raise PinError(f"{alias}: {local} is not a pipeline")
            origin, _ = find_home(local)
            shutil.copytree(origin, destination, ignore=shutil.ignore_patterns(".git", ".github"))
            notes.append(f"{alias}: copied from {source} (local, not pinned)")
            continue

        entry = pins.get(alias) or {}
        sha = str(entry.get("sha") or "")
        if not sha:
            raise PinError(
                f"{alias}: no commit recorded for {source}",
                hint="run `lockstep pin` first — fetching a moving ref would defeat the lock file",
            )
        _clone_at(entry.get("repo", ""), sha, destination)
        notes.append(f"{alias}: {entry.get('repo')}@{sha[:12]}")

    return notes


def _clone_at(repo: str, sha: str, destination: Path) -> None:
    """Fetch exactly one commit. Not a branch that happens to point at it today."""
    url = f"https://github.com/{repo}.git"
    destination.mkdir(parents=True, exist_ok=True)
    steps = (
        ["git", "init", "--quiet"],
        ["git", "remote", "add", "origin", url],
        ["git", "fetch", "--quiet", "--depth", "1", "origin", sha],
        ["git", "checkout", "--quiet", "FETCH_HEAD"],
    )
    for args in steps:
        result = subprocess.run(args, cwd=destination, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise PinError(
                f"could not fetch {repo}@{sha[:12]}: {result.stderr.strip()[:200]}",
                hint="the commit must exist and be reachable with the token this job holds",
            )
    shutil.rmtree(destination / ".git", ignore_errors=True)


EJECTED_PATH = ".pipeline/ejected.yaml"
EJECT_BASE = ".pipeline/eject-base"


def _home(root: Path) -> Path:
    """Where this repository keeps its pipeline.

    Compiler state belongs with the definitions it describes: in a repository that adopted a
    pipeline into `.lockstep/`, the ejection registry goes there too rather than leaving a second
    `.pipeline/` directory at the root of somebody else's project.
    """
    return find_home(root)[0]


class PinError(LockstepError):
    code = "LS300"


class EjectError(LockstepError):
    code = "LS400"


# --- pinning ---------------------------------------------------------------


def resolve_ref(repo: str, ref: str) -> str:
    """Resolve a tag *or a branch* to the commit it currently points at.

    Branches matter for inheritance in a way they do not for capabilities: a canary consumer tracks
    `@main` so an upstream change that breaks a real overlay surfaces before the tag is cut.
    """
    owner_repo = "/".join(repo.split("/")[:2])
    url = f"https://github.com/{owner_repo}.git"
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", "--heads", "--refs", url, ref],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PinError(f"could not reach {url}: {exc}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise PinError(
            f"no tag or branch {ref!r} in {owner_repo}",
            hint="check the ref in pipeline.yaml, or pass --sha to pin by hand",
        )
    return result.stdout.split()[0]


def _pins_path(root: Path) -> Path:
    return _home(root) / PINS_PATH


def load_pins(root: Path) -> dict[str, Any]:
    path = _pins_path(root)
    if path.is_file():
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return loaded
    return {"capabilities": {}, "external": {}}


def write_pins(root: Path, data: dict[str, Any]) -> Path:
    path = _pins_path(root)
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
                resolved = resolve_ref(repo, tag)
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
            entry["sha"] = resolve_ref(action, tag)
        except PinError as error:
            unresolved.append(f"{action}: {error.message}")
        else:
            notes.append(f"{action}@{tag} -> {entry['sha'][:12]}")

    # Inherited pipelines pin exactly like capabilities do, and for the same reason: a tag someone
    # retags is the supply-chain event this whole mechanism exists to catch.
    inherits = data.setdefault("inherits", {})
    for alias, source in sorted(spec.manifest.inherits.items()):
        if not source.startswith("github.com/"):
            notes.append(f"{alias}: local path, not pinned")
            inherits.pop(alias, None)
            continue
        repo, _, ref = source.removeprefix("github.com/").partition("@")
        if not ref:
            unresolved.append(f"{alias}: {source} names no ref; add `@<tag-or-branch>`")
            continue
        entry = inherits.setdefault(alias, {})
        if entry.get("repo") not in (None, repo) or entry.get("ref") not in (None, ref):
            entry.pop("sha", None)
        entry["repo"], entry["ref"] = repo, ref
        if offline:
            notes.append(f"{alias}: left as-is (offline)")
            continue
        try:
            resolved = resolve_ref(repo, ref)
        except PinError as error:
            unresolved.append(f"{alias}: {error.message}")
        else:
            if entry.get("sha") and entry["sha"] != resolved:
                notes.append(f"{alias}: {ref} moved {entry['sha'][:12]} -> {resolved[:12]}")
            entry["sha"] = resolved
            notes.append(f"{alias}: {repo}@{ref} -> {resolved[:12]}")
    for alias in [a for a in inherits if a not in spec.manifest.inherits]:
        inherits.pop(alias)
        notes.append(f"{alias}: no longer inherited; dropped from the lock")

    package, _, version = spec.manifest.capabilities.exec.partition("==")
    entry = capabilities.setdefault("exec", {})
    entry["package"] = package or "pipeline-exec"
    if version:
        entry["version"] = version

    # The manifest says where; the lock records what was found there. Writing the manifest's value
    # in rather than keeping whatever was here is what makes a registry change take effect — and
    # what makes a stale digest a hard error at the next compile rather than a silent pull from
    # wherever the image used to live.
    image = spec.manifest.capabilities.exec_image
    if not image:
        unresolved.append(
            "exec image: set capabilities.exec-image in pipeline.yaml to where it is published, "
            "e.g. `quay.io/<owner>/pipeline-exec`"
        )
    else:
        if entry.get("image") and entry["image"] != image:
            entry.pop("digest", None)
            notes.append(f"exec image moved to {image}; its digest was dropped and must be resolved")
        entry["image"] = image

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
    current = resolve_ref(entry["repo"], entry["tag"])
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
        path = _home(root) / EJECTED_PATH
        if not path.is_file():
            return cls(files=[])
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(files=[str(entry) for entry in (data.get("files") or [])])

    def save(self, root: Path) -> Path:
        path = _home(root) / EJECTED_PATH
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

    base = _home(root) / EJECT_BASE / relative
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
    base = _home(root) / EJECT_BASE / relative
    if base.is_file():
        base.unlink()


def stale_ejections(root: Path, generated: dict[str, str]) -> list[str]:
    """Ejected files whose generation has moved on since they were forked."""
    stale = []
    for relative in Ejection.load(root).files:
        base = _home(root) / EJECT_BASE / relative
        fresh = generated.get(relative)
        if fresh is not None and base.is_file() and base.read_text(encoding="utf-8") != fresh:
            stale.append(relative)
    return stale
