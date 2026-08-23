"""Writing and verifying generated output."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .plan import MANIFEST_PATH, CompilePlan


@dataclass
class WriteReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated or self.removed)


@dataclass
class CheckReport:
    missing: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    orphaned: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.missing or self.modified or self.orphaned)


def _home(root: Path) -> Path:
    """Where this repository keeps its pipeline — `.lockstep/` when the definitions live there."""
    from ..spec.load import find_home

    return find_home(root)[0]


def _manifest_path(root: Path) -> Path:
    """The compile manifest, wherever this repository keeps its pipeline."""
    return _home(root) / MANIFEST_PATH


def previously_generated(root: Path) -> set[str]:
    """Files the last compile owned — the compiler prunes what it no longer generates."""
    path = _manifest_path(root)
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return set(data.get("files", {}))


def _ejected(root: Path) -> set[str]:
    """Files the user has taken ownership of; the compiler must not touch them."""
    import yaml

    path = _home(root) / ".pipeline/ejected.yaml"
    if not path.is_file():
        return set()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(entry) for entry in (data.get("files") or [])}


def write_plan(root: Path, plan: CompilePlan, *, prune: bool = True) -> WriteReport:
    report = WriteReport()
    ejected = _ejected(root)
    # Capture the previous file set *before* writing, because the new compile manifest is itself one
    # of the files we are about to write over it.
    previous = previously_generated(root)

    for relative, content in sorted(plan.files.items()):
        if relative in ejected:
            continue
        target = root / relative
        if target.is_file():
            if target.read_text(encoding="utf-8") == content:
                report.unchanged.append(relative)
                continue
            report.updated.append(relative)
        else:
            report.created.append(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    if prune:
        for relative in sorted(previous - set(plan.files) - ejected):
            target = root / relative
            if target.is_file():
                target.unlink()
                report.removed.append(relative)
    return report


def check_plan(root: Path, plan: CompilePlan) -> CheckReport:
    """The drift gate: committed output must equal a fresh compile of spec + overlays + pins."""
    report = CheckReport()
    ejected = _ejected(root)

    for relative, content in sorted(plan.files.items()):
        if relative in ejected:
            continue
        target = root / relative
        if not target.is_file():
            report.missing.append(relative)
        elif target.read_text(encoding="utf-8") != content:
            report.modified.append(relative)

    for relative in sorted(previously_generated(root) - set(plan.files) - ejected):
        if (root / relative).is_file():
            report.orphaned.append(relative)
    return report
