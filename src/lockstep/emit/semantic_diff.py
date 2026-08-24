"""The semantic diff: what a change does to the security and cost surface.

Reviewers cannot meaningfully read thousands of lines of generated YAML, and the drift gate already
proves the output matches its inputs. What is left worth reading is this: permissions, triggers,
egress, MCP tool allow-lists, safe-output caps, secrets, pins and budgets. Deltas in the blocking
categories are meant to fail a required check until explicitly acknowledged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .plan import CompilePlan

SECRET_REF = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)")
USES_REF = re.compile(r"^\s*(?:- )?uses:\s*(\S+)", re.MULTILINE)
# `.` is DOTALL here, so the body group must be the only greedy construct: a `#`-comment group
# would swallow the whole file. YAML ignores the provenance comments natively, so it can stay.
FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n", re.DOTALL)

# Categories whose deltas should block a merge until a human acknowledges them.
BLOCKING = {
    "permissions",
    "triggers",
    "network",
    "mcp-tools",
    "secrets",
    "safe-output-caps",
    # What a deterministic step may do. Adding a capability back, or raising a limit, widens what
    # arbitrary code in a `script:` step can reach — the same class of change as widening egress.
    "sandbox",
}


@dataclass
class Delta:
    category: str
    path: str
    detail: str

    @property
    def blocking(self) -> bool:
        return self.category in BLOCKING

    def render(self) -> str:
        mark = "BLOCK" if self.blocking else " info"
        return f"  [{mark}] {self.category}: {self.path} — {self.detail}"


@dataclass
class SemanticDiff:
    deltas: list[Delta] = field(default_factory=list)

    @property
    def blocking(self) -> list[Delta]:
        return [d for d in self.deltas if d.blocking]

    def render(self) -> str:
        if not self.deltas:
            return "  no changes to the security or cost surface"
        return "\n".join(d.render() for d in sorted(self.deltas, key=lambda d: (not d.blocking, d.path)))


def _load_frontmatter(text: str) -> dict[str, Any]:
    match = FRONTMATTER.match(text)
    if not match:
        return {}
    loaded = yaml.safe_load(match.group(1))
    return loaded if isinstance(loaded, dict) else {}


def surface_of(path: str, text: str) -> dict[str, Any]:
    """Extract the reviewable surface from one generated file."""
    surface: dict[str, Any] = {
        "secrets": sorted(set(SECRET_REF.findall(text))),
        "uses": sorted(set(USES_REF.findall(text))),
    }
    if path.endswith(".yml"):
        data = yaml.safe_load(text) or {}
        if isinstance(data, dict):
            surface["permissions"] = data.get("permissions")
            surface["triggers"] = sorted((data.get(True) or data.get("on") or {}).keys())
            surface["sandbox"] = {
                name: (job.get("container") or {}).get("options")
                for name, job in (data.get("jobs") or {}).items()
                if isinstance(job, dict) and isinstance(job.get("container"), dict)
            }
    elif path.endswith(".md"):
        front = _load_frontmatter(text)
        surface["permissions"] = front.get("permissions")
        surface["network"] = sorted((front.get("network") or {}).get("allowed", []))
        surface["mcp-tools"] = {
            name: sorted(entry.get("allowed", [])) for name, entry in (front.get("mcp-servers") or {}).items()
        }
        surface["safe-output-caps"] = {
            name: (entry or {}).get("max")
            for name, entry in (front.get("safe-outputs") or {}).items()
            if isinstance(entry, dict)
        }
        surface["credits"] = front.get("max-ai-credits")
        surface["daily-credits"] = front.get("max-daily-ai-credits")
        surface["turns"] = front.get("max-turns")
        surface["engine"] = front.get("engine")
    return surface


def is_workflow(path: str) -> bool:
    """Orchestrators and agentic workflows only — prompt fragments and docs carry no surface."""
    name = path.rsplit("/", 1)[-1]
    if "/shared/" in path:
        return False
    return name.endswith(".yml") or name.startswith("aw-")


def surfaces(files: dict[str, str]) -> dict[str, dict[str, Any]]:
    return {path: surface_of(path, text) for path, text in files.items() if is_workflow(path)}


def diff_surfaces(old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]) -> SemanticDiff:
    result = SemanticDiff()
    for path in sorted(set(old) | set(new)):
        before, after = old.get(path), new.get(path)
        if before is None:
            result.deltas.append(Delta("new-file", path, "workflow added"))
            continue
        if after is None:
            result.deltas.append(Delta("removed-file", path, "workflow removed"))
            continue
        for category in sorted(set(before) | set(after)):
            was, now = before.get(category), after.get(category)
            if was != now:
                result.deltas.append(
                    Delta(category if category in BLOCKING else category, path, f"{was!r} -> {now!r}")
                )
    return result


def against_disk(root: Path, plan: CompilePlan) -> SemanticDiff:
    """Compare a fresh compile against what is currently in the working tree."""
    on_disk: dict[str, str] = {}
    for relative in plan.files:
        target = root / relative
        if target.is_file():
            on_disk[relative] = target.read_text(encoding="utf-8")
    return diff_surfaces(surfaces(on_disk), surfaces(plan.files))


def against_ref(root: Path, plan: CompilePlan, ref: str) -> SemanticDiff:
    """Compare a fresh compile against a git ref — normally the branch being merged into.

    Comparing against the working tree answers "did you forget to recompile", which the drift gate
    already covers. The question a reviewer needs answered is different: what does merging this
    change about the security and cost surface? That is only visible against the base.
    """
    import subprocess

    baseline: dict[str, str] = {}
    for relative in plan.files:
        result = subprocess.run(
            ["git", "show", f"{ref}:{relative}"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            baseline[relative] = result.stdout
    return diff_surfaces(surfaces(baseline), surfaces(plan.files))
