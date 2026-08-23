"""Skip-on-cached-output, lowered onto GitHub.

The local runtime skips a step when its output file exists. CI has no persistent workspace, so the
compiled semantics are content-addressed instead: a step is skipped when an entry exists for the
exact tuple of (normalized step definition, script content, upstream outputs, profile fingerprint,
runtime inputs). That is strictly safer than file-exists — a changed script or a changed upstream
output re-runs the step automatically, which the local runtime only approximates.

Two layers, in order: a run-scoped named artifact (survives the 7-day `actions/cache` eviction), then
`actions/cache` itself. `force` skips the *restore* but still publishes, so a forced refresh with
unchanged inputs actually lands — an immutable cache key alone would silently drop it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..spec.model import Command, Step, StepKind
from ..util.hashing import sha_obj, short
from .context import EmitContext

KEY_SCHEMA = "ls-v1"
STEP_DEFS_DIR = ".pipeline/step-defs"

# `--output=<file>` participates in caching. `--output-dir=` deliberately does not: a directory
# always exists, so treating it as a skip signal would skip work that never ran.
OUTPUT_FLAG = re.compile(r"--output=(\S+)")
INPUT_EXPRESSION = re.compile(r"\$\{\{\s*inputs\.[A-Za-z0-9_]+\s*\}\}")


@dataclass
class CacheSpec:
    """Everything needed to emit one step's probe/save pair."""

    step_id: str
    outputs: list[str]
    key_inputs: list[str] = field(default_factory=list)
    key_extra: list[str] = field(default_factory=list)
    key_prefix: str = ""
    fingerprint: str = ""

    @property
    def probe_id(self) -> str:
        return f"cache-{self.step_id}"

    @property
    def fingerprint_id(self) -> str:
        return f"fingerprint-{self.step_id}"

    @property
    def hit_condition(self) -> str:
        return "${{ steps." + self.probe_id + ".outputs.hit != 'true' }}"


def declared_outputs(step: Step, ctx: EmitContext, command: Command) -> list[str]:
    """Output paths this step declares, in a form the cache can address."""
    paths: list[str] = []
    if step.output:
        paths.append(ctx.expand(step.output, command))
    paths.extend(OUTPUT_FLAG.findall(ctx.expand(step.args.get("args", ""), command)))
    return paths


def step_definition(step: Step, command: Command) -> dict[str, Any]:
    """The normalized step definition whose hash joins the cache key.

    Editing a step's args in markdown must invalidate that step's cache, so the definition is
    serialized to a committed file and hashed as a key input like any other file.
    """
    return {
        "command": command.name,
        "id": step.id,
        "kind": step.kind.value,
        "target": step.target,
        "args": dict(sorted(step.args.items())),
        "output": step.output,
        "input": step.input,
        "context_files": list(step.context_files),
        "foreach": None
        if step.foreach is None
        else {
            "var": step.foreach.var,
            "source": step.foreach.source,
            "key_field": step.foreach.key_field,
        },
        "parallel": step.parallel,
        "pre": step.pre,
        "post": step.post,
        "on_failure": step.on_failure,
        "fingerprint": step.fingerprint,
    }


def step_def_path(command: Command, step: Step) -> str:
    return f"{STEP_DEFS_DIR}/{command.name}.{step.id}.json"


def render_step_def(step: Step, command: Command) -> str:
    return json.dumps(step_definition(step, command), indent=2, sort_keys=True) + "\n"


def cache_spec_for(
    step: Step,
    command: Command,
    ctx: EmitContext,
    upstream: dict[str, str],
) -> CacheSpec | None:
    """Build a cache spec, or None when the step declares nothing cacheable."""
    if step.foreach:
        # A foreach step must not be step-cached: every matrix leg would compute the same key, so
        # one leg publishing would make all legs skip on the next run. Per-item skipping is already
        # handled — better — by `fanout --only-missing`, which drops covered items before the matrix
        # exists and so never starts a runner for them.
        return None

    outputs = declared_outputs(step, ctx, command)
    if not outputs:
        return None

    key_inputs = [ctx.spec.repo_path(step_def_path(command, step))]
    if step.kind is StepKind.SCRIPT:
        key_inputs.append(ctx.spec.repo_path(step.target))

    # Invalidation cascades: if this step reads an earlier step's output, that output joins the key.
    expanded_args = ctx.expand(step.args.get("args", ""), command)
    haystack = f"{expanded_args} {ctx.expand(step.input, command)}"
    key_inputs.extend(path for path in upstream if path in haystack and path not in outputs)

    # Runtime-valued inputs change behaviour without changing any file, so they join the key too.
    key_extra = sorted(set(INPUT_EXPRESSION.findall(haystack)))
    if step.fingerprint:
        key_extra.append("${{ steps." + f"fingerprint-{step.id}" + ".outputs.value }}")

    profile_fingerprint = short(sha_obj(sorted(ctx.resolved_values().items())))
    return CacheSpec(
        step_id=step.id,
        outputs=outputs,
        key_inputs=sorted(set(key_inputs)),
        key_extra=key_extra,
        key_prefix=f"{KEY_SCHEMA}-{ctx.spec.manifest.name}-{command.name}-{step.id}-{profile_fingerprint}",
        fingerprint=ctx.expand(step.fingerprint, command),
    )


def emit_fingerprint(spec: CacheSpec, step: Step) -> dict[str, Any]:
    """Fingerprint a live target so a redeploy invalidates discovery.

    Repo files cannot describe the state of a deployed application: a staging redeploy that renames
    endpoints changes nothing on disk. A cheap fingerprint of the target closes that hole. Failure to
    fetch it fails the step — serving stale discovery output is the worse outcome.
    """
    return {
        "id": spec.fingerprint_id,
        "name": f"Fingerprint target for {step.label}",
        "run": (
            "set -euo pipefail\n"
            f'value="$({spec.fingerprint})"\n'
            'if [ -z "$value" ]; then\n'
            '  echo "fingerprint command produced no output; refusing to serve a cached result" >&2\n'
            "  exit 1\n"
            "fi\n"
            'echo "value=$value" >> "$GITHUB_OUTPUT"\n'
        ),
    }


def emit_probe(spec: CacheSpec, ctx: EmitContext) -> dict[str, Any]:
    with_block: dict[str, Any] = {
        "step": spec.step_id,
        "key-prefix": spec.key_prefix,
        "key-inputs": "\n".join(spec.key_inputs) + "\n",
        "outputs": "\n".join(spec.outputs) + "\n",
        "force": "${{ inputs.force }}",
        "force-steps": "${{ inputs.force_steps }}",
    }
    if spec.key_extra:
        with_block["key-extra"] = " ".join(spec.key_extra)
    return {"id": spec.probe_id, "uses": ctx.pins.action("step-cache"), "with": with_block}


def emit_save(spec: CacheSpec, ctx: EmitContext) -> dict[str, Any]:
    return {
        "name": f"Publish {spec.step_id} outputs",
        "uses": ctx.pins.action("step-cache/save"),
        "if": spec.hit_condition,
        "with": {
            "step": spec.step_id,
            "outputs": "\n".join(spec.outputs) + "\n",
            # Publish under the key the probe computed, so the two layers agree on identity.
            "key": "${{ steps." + spec.probe_id + ".outputs.key }}",
        },
    }
