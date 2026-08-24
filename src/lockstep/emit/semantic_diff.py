"""The semantic diff: what a change does to the security and cost surface.

Reviewers cannot meaningfully read thousands of lines of generated YAML, and the drift gate already
proves the output matches its inputs. What is left worth reading is this: permissions, triggers,
egress, MCP tool allow-lists, safe-output caps, secrets, pins and budgets. Deltas in the blocking
categories fail a required check until explicitly acknowledged.

**Acknowledgment is a commit trailer**, named per category:

    Security-Surface: sandbox, permissions

It is a trailer rather than a file because a delta exists only between a base and a head. Once the
change merges there is no delta left, so an in-tree acknowledgment would go stale the moment it
landed and the file would accumulate entries describing changes nobody can still see. A trailer is
scoped to the commit that moved the surface, survives a squash, and stays as the permanent answer to
"why does this pipeline have that permission" — which is the question somebody actually asks later.

There is no blanket form on purpose. `all` and `*` are refused: a gate that can be cleared without
reading it is a gate people clear without reading it. Acknowledging a category with no delta in it is
reported too — an acknowledgment copied forward from a previous change is how the habit decays.
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


# `Security-Surface: sandbox, permissions`. Case-insensitive on the key, because git trailers
# conventionally are, and a rejected acknowledgment over capitalization would be infuriating.
ACK_TRAILER = re.compile(
    r"^[ \t]*Security-Surface[ \t]*:[ \t]*(?P<categories>.+?)[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)
# Refused: an acknowledgment that names everything has stopped being a statement about this change.
BLANKET = {"all", "*", "any", "everything"}


class AcknowledgementError(ValueError):
    """An acknowledgment that would pass the gate without saying anything."""


# A git trailer lives in the **last paragraph**, and every line of that paragraph is one. Anywhere
# else is prose that happens to look like a trailer.
#
# That distinction is load-bearing rather than pedantic. The commit that introduced this mechanism
# explained the format with an indented example in its body, and a parser scanning the whole message
# read the example as a real acknowledgment — so a commit that merely *documents* the trailer, or
# quotes an earlier one, silently clears a gate. Matching git's own definition removes the whole
# class: prose can say anything.
TRAILER_LINE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*[ \t]*:[ \t]*\S")


def trailer_block(message: str) -> str:
    """The trailing run of paragraphs that consist entirely of trailers.

    Walking back rather than taking only the last paragraph, because people routinely separate
    `Co-Authored-By` from the rest with a blank line and losing the trailer above it would be a
    surprise nobody could debug. Walking stops at the first paragraph containing prose, which is
    what keeps an example inside the body from ever counting.
    """
    paragraphs = [block for block in re.split(r"\n[ \t]*\n", (message or "").strip()) if block.strip()]
    kept: list[str] = []
    for block in reversed(paragraphs):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines or not all(TRAILER_LINE.match(line) for line in lines):
            break
        kept.append(block)
    return "\n".join(reversed(kept))


def parse_acknowledgements(text: str) -> set[str]:
    """Categories acknowledged by `Security-Surface:` trailers in one commit message."""
    found: set[str] = set()
    for match in ACK_TRAILER.finditer(trailer_block(text)):
        for raw in match.group("categories").split(","):
            category = raw.strip().lower()
            if not category:
                continue
            if category in BLANKET:
                raise AcknowledgementError(
                    f"`Security-Surface: {category}` acknowledges everything, which is not a "
                    "statement about this change — name the categories the diff reported"
                )
            found.add(category)
    return found


def acknowledgements_since(root: Path, ref: str) -> set[str]:
    """Read the trailers from every commit between `ref` and HEAD.

    Every commit in the range, not just the tip: the commit that widens the surface is the one that
    should carry the reason, and it is rarely the last one written.
    """
    import subprocess

    result = subprocess.run(
        # NUL-separated, because a trailer is defined relative to the end of *one* message and
        # concatenating them would put the next commit's subject after the previous one's trailers.
        ["git", "log", "--format=%B%x00", f"{ref}..HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # No range to read — comparing against the working tree, a shallow clone, or a ref that is
        # not an ancestor. Nothing is acknowledged, which fails closed.
        return set()
    found: set[str] = set()
    for message in result.stdout.split("\0"):
        found |= parse_acknowledgements(message)
    return found


@dataclass
class Delta:
    category: str
    path: str
    detail: str

    @property
    def blocking(self) -> bool:
        return self.category in BLOCKING

    def render(self, *, acknowledged: bool = False) -> str:
        if not self.blocking:
            mark = " info"
        else:
            mark = "ack'd" if acknowledged else "BLOCK"
        return f"  [{mark}] {self.category}: {self.path} — {self.detail}"


@dataclass
class SemanticDiff:
    deltas: list[Delta] = field(default_factory=list)
    acknowledged: set[str] = field(default_factory=set)

    @property
    def blocking(self) -> list[Delta]:
        return [d for d in self.deltas if d.blocking]

    @property
    def unacknowledged(self) -> list[Delta]:
        """What still fails the gate: a blocking delta whose category nobody named."""
        return [d for d in self.blocking if d.category not in self.acknowledged]

    @property
    def stale_acknowledgements(self) -> list[str]:
        """Acknowledged categories the diff did not report.

        Not a failure — nothing unsafe merges because of one. But an acknowledgment carried forward
        from an earlier change is how the trailer stops meaning anything, and the only moment anyone
        would notice is here.
        """
        present = {d.category for d in self.blocking}
        return sorted(category for category in self.acknowledged if category not in present)

    def render(self) -> str:
        if not self.deltas:
            return "  no changes to the security or cost surface"
        lines = [
            d.render(acknowledged=d.category in self.acknowledged)
            for d in sorted(self.deltas, key=lambda d: (not d.blocking, d.path))
        ]
        return "\n".join(lines)


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
            # Workflow level *and* per job. Only the first was read for a long time, and the
            # consequence was not theoretical: the job that publishes the run ledger carries
            # `contents: write` while the workflow around it stays `contents: read`, and the diff
            # reported no permissions change at all. It surfaced only because that job also has a
            # container and so appeared in the sandbox map — a job granted write *without* one
            # would have passed this gate in silence, which is the exact change it exists to catch.
            surface["permissions"] = {
                "workflow": data.get("permissions"),
                "jobs": {
                    name: job.get("permissions")
                    for name, job in (data.get("jobs") or {}).items()
                    if isinstance(job, dict) and job.get("permissions") is not None
                },
            }
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
                result.deltas.append(Delta(category, path, describe(was, now)))
    return result


def describe(was: Any, now: Any) -> str:
    """What moved, rather than both states in full.

    A surface keyed by job name renders as two near-identical dicts, and a reviewer has to diff
    seven keys by eye to find the one that changed. This is the one output meant to be read by
    somebody who cannot read the generated YAML, so burying the delta in its own context defeats
    the point — and a security report people skim is one they stop reading.
    """
    if not (isinstance(was, dict) and isinstance(now, dict)):
        return f"{was!r} -> {now!r}"

    lines: list[str] = []
    for key in sorted(set(was) | set(now)):
        before_value, after_value = was.get(key), now.get(key)
        if before_value == after_value:
            continue
        if key not in was:
            lines.append(f"+{key}: {after_value!r}")
        elif key not in now:
            lines.append(f"-{key}: {before_value!r}")
        else:
            lines.append(f"~{key}: {before_value!r} -> {after_value!r}")
    # Nested one level: `permissions` is {"workflow": ..., "jobs": {...}}, and a change inside
    # `jobs` would otherwise render as the whole map again.
    return "; ".join(lines) if lines else f"{was!r} -> {now!r}"


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
    diff = diff_surfaces(surfaces(baseline), surfaces(plan.files))
    # From the same ref the comparison is against, so what counts as acknowledged is exactly what
    # this change added on top of the base.
    diff.acknowledged = acknowledgements_since(root, ref)
    return diff
