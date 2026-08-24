"""Prompt layers the compiler ships.

Where every pipeline writes the same thing, the framework should write it once. Five example
pipelines independently authored the same four safety rules and only three of them remembered the
one about treating input as data rather than instructions — which is the failure mode of leaving an
invariant to authors: it is not that they write it badly, it is that one of them forgets.

Three kinds live here, and nothing else ever should:

- **`guardrails/baseline.md`** — the constraints that hold for every agent in every pipeline. It is
  prepended to every agent's guardrails, ahead of the spec's own, and cannot be excluded. It carries
  no `enforce:` block: permissions and tool denials vary per pipeline and stay in the spec.
- **`skills/*.md`** — the format of something the framework itself parses. A pipeline restating one
  of these by hand keeps a copy that drifts the moment the parser changes, silently, in the
  direction of runs that fail for reasons no diff explains.

- **`pipelines/*/`** — whole pipelines, so that adopting this framework does not begin with writing
  one. These are unlike the other two in the way that matters: the baseline guardrail is *injected*
  into every agent whether or not anybody asked, because it is an invariant. A shipped pipeline is
  **inherited**, which means opt-in by name, overlayable step by step, and replaceable by one you
  write yourself that sits beside it. Nothing here is forced on anybody, and that is the whole
  reason it is safe to ship an opinion this large.

A context is never shipped. The framework cannot know your application, and the moment it ships
something that pretends to, it has made the mistake `docs/layers.md` exists to name. A shipped
pipeline is held to the same rule: it may carry agents, guardrails and steps, and it may not carry
knowledge of a codebase it has never seen.
"""

from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
from pathlib import Path

from ..spec.model import Fragment
from ..spec.parse import parse_fragment, read_source

HERE = Path(__file__).parent
BASELINE = "baseline"


@lru_cache(maxsize=1)
def _load() -> dict[str, dict[str, Fragment]]:
    loaded: dict[str, dict[str, Fragment]] = {}
    for subdir in ("guardrails", "skills"):
        bucket: dict[str, Fragment] = {}
        for path in sorted((HERE / subdir).glob("*.md")):
            # Stamped `lockstep:` so provenance headers distinguish a shipped layer from one in the
            # repository, and so the drift gate notices when a compiler upgrade changes its text.
            src = replace(read_source(path, HERE), rel=f"lockstep:{subdir}/{path.name}")
            fragment = parse_fragment(src, subdir.rstrip("s"))
            bucket[fragment.name] = fragment
        loaded[subdir] = bucket
    return loaded


def guardrails() -> dict[str, Fragment]:
    return dict(_load()["guardrails"])


def skills() -> dict[str, Fragment]:
    return dict(_load()["skills"])


def baseline() -> Fragment:
    """The guardrail every agent inherits, whatever the spec says."""
    return _load()["guardrails"][BASELINE]


# How a manifest names a shipped pipeline: `inherits: {standard: lockstep:standard}`. The same
# prefix the provenance headers use, so a reader who has seen one has seen the other.
PIPELINE_SCHEME = "lockstep:"
PIPELINES = HERE / "pipelines"


def pipelines() -> dict[str, Path]:
    """The pipelines this compiler ships, by the name a manifest inherits them under."""
    if not PIPELINES.is_dir():
        return {}
    return {
        path.name: path
        for path in sorted(PIPELINES.iterdir())
        if path.is_dir() and (path / "pipeline.yaml").is_file()
    }


def is_shipped(source: str) -> bool:
    return source.startswith(PIPELINE_SCHEME)


def shipped_name(source: str) -> str:
    return source.removeprefix(PIPELINE_SCHEME).strip()


def shipped_pipeline(source: str) -> Path | None:
    """Where a `lockstep:` source lives on disk, or nothing when no such pipeline ships."""
    return pipelines().get(shipped_name(source))
