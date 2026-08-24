"""Scaffolding a new pipeline repository.

The scaffold is deliberately a *working* pipeline rather than a set of empty directories: it
compiles, it lints clean, and it demonstrates the one pattern that matters — a deterministic step
producing work, an agent fanned out over it, and a deterministic step consuming the results. Someone
reading it should be able to see where their own work goes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import LockstepError


class ScaffoldError(LockstepError):
    code = "LS500"


@dataclass(frozen=True)
class Adopt:
    """A repository that runs the pipelines this compiler ships, and authors none of them.

    The whole file set is a manifest and a profile. That is the point: adopting should not begin
    with writing a pipeline, and everything here stays true as the repository grows — the shipped
    pipelines are inherited, so they are overlayable step by step, and a pipeline written later
    sits beside them rather than replacing them.
    """

    name: str
    profile: str
    pipelines: tuple[str, ...]

    def files(self) -> dict[str, str]:
        return {
            "pipeline.yaml": self._manifest(),
            f"profiles/{self.profile}.md": self._profile(),
            ".gitignore": ".pipeline/inherited/\noutputs/\n",
            "README.md": self._readme(),
        }

    def _manifest(self) -> str:
        inherits = "\n".join(f"  {name}: lockstep:{name}" for name in self.pipelines)
        first = self.pipelines[0]
        return f"""spec: 1
name: {self.name}

# The pipelines this repository runs, and where they come from. `lockstep:` means they ship inside
# the compiler, so the version range below is what decides which ones you get — there is no second
# thing to pin. Inheriting them compiles them; nothing further is required to run one.
inherits:
{inherits}

# Nothing needs to go here to get started. This is where an inherited command is *tuned* — within
# the bands its author published, without forking it:
#
# commands:
#   {first}:
#     from: {first}
#     add-guardrails: [house-style]     # a guardrail of yours, on their agents
#     agents:
#       triage-analyst:
#         max-ai-credits: 40            # inside the band the pipeline published
#
# `overlays/` changes their steps. A command in `commands/` runs beside them.

capabilities:
  actions: github.com/in-lockstep/lockstep/actions@actions-v1.0.0
  exec: in-lockstep-exec==0.1.0
  exec-image: ghcr.io/in-lockstep/pipeline-exec
  compiler: in-lockstep>=0.1,<1.0
  gh-aw: v0.86.2

targets:
  github-agentic:
    profiles: [{self.profile}]

budgets:
  per_run_ai_credits: 200
  # Per agent workflow, per day, refused by gh-aw before the agent starts. Unset does not mean
  # unlimited — it means gh-aw's own default of 5000.
  per_agent_daily_ai_credits: 2000
"""

    def _profile(self) -> str:
        return f"""---
name: {self.profile}
github:
  secrets: [ANTHROPIC_API_KEY]
---

Where these pipelines run. Add the tracker credentials here if you read issues from Jira:
`JIRA_BASE_URL` and `JIRA_API_TOKEN` as secrets, and `issue_source: jira` as a value.
"""

    def _readme(self) -> str:
        listed = "\n".join(f"- `{name}`" for name in self.pipelines)
        return f"""# {self.name}

Runs the pipelines that ship with lockstep:

{listed}

```bash
lockstep pin        # resolve capability tags to commits
lockstep compile    # generate the workflows
lockstep lint       # check the spec
```

## Growing out of this

1. **Tune one.** A `commands:` entry in `pipeline.yaml` moves a model or a budget within the band
   the pipeline published, or adds a guardrail of yours to its agents. No fork, no copy.
2. **Change its steps.** An overlay in `overlays/` inserts, replaces or deletes a step by id.
   `lockstep compile --check` still holds the result to the spec.
3. **Write your own.** A command in `commands/` runs beside the inherited ones and can reuse their
   agents. Nothing has to be given up to add one.
"""


@dataclass(frozen=True)
class Scaffold:
    name: str
    profile: str

    def files(self) -> dict[str, str]:
        return {
            "pipeline.yaml": self._manifest(),
            f"commands/{self.name}.md": self._command(),
            "agents/summarizer.md": self._agent(),
            "guardrails/common.md": self._guardrail(),
            f"profiles/{self.profile}.md": self._profile(),
            "mcp/servers.json": '{\n  "servers": {}\n}\n',
            "scripts/collect-items.py": self._script(),
            "tests/test_collect_items.py": self._script_test(),
            "evals/summarizer/cases/one-item.json": self._eval_case(),
            ".gitignore": self._gitignore(),
            "README.md": self._readme(),
        }

    # --- spec ---

    def _manifest(self) -> str:
        return f"""spec: 1
name: {self.name}

capabilities:
  # Where the capabilities live. These are the two addresses you have to change: point them at
  # wherever you published the composite actions and the executor image, then `lockstep pin` to
  # resolve them into `.pipeline/pins.lock`. Any registry works for the image.
  actions: github.com/in-lockstep/lockstep/actions@actions-v1.0.0
  exec: in-lockstep-exec==0.1.0
  exec-image: ghcr.io/in-lockstep/pipeline-exec
  compiler: in-lockstep>=0.1,<1.0
  gh-aw: v0.86.2

targets:
  github-agentic:
    out: .github/workflows
    fuse-script-steps: true
    default-runs-on: ubuntu-24.04
    profiles: [{self.profile}]

budgets:
  # A scheduled pipeline without a budget is an unbounded bill.
  per_run_ai_credits: 200
"""

    def _command(self) -> str:
        return f"""---
name: {self.name}
description: Collect items, summarize each one, then report
parameters:
  - name: limit
    default: "10"
    description: How many items to collect
guardrails: [common]
github:
  triggers:
    workflow_dispatch: true
    # schedule: '0 3 * * 1-5'
---

## Steps

1. **Collect items** → script: scripts/collect-items.py
   - id: collect
   - args: --limit={{limit}} --output={{output_dir}}/items.json

2. **Summarize each item** → agent: summarizer
   - foreach: item in {{output_dir}}/items.json
   - output: {{output_dir}}/summaries
   - parallel: 3
   - min-success-rate: 0.9

3. **Report** → builtin: validate-schema
   - args: --dir={{output_dir}}/summaries --require=summary
"""

    def _agent(self) -> str:
        return """---
name: summarizer
description: Summarize one collected item
model: claude-sonnet-4-6
provider: vertex-claude
max_tool_turns: 0
guardrails: [common]
github:
  max-ai-credits: 20
---

You summarize a single item in two sentences: what it is, and why it matters.

Write nothing you cannot support from the item itself.

## Output

Return a JSON object with a single `summary` field.
"""

    def _guardrail(self) -> str:
        return """---
name: common
description: Baseline constraints every agent inherits
enforce:
  # The enforceable half of a guardrail: the compiler turns this into permissions the model cannot
  # exceed, rather than a request it might ignore.
  permissions: read-all
  deny-tools: [delete_*]
---

You MUST return valid JSON matching the requested schema.
You MUST NOT invent facts that are absent from your input.
NEVER include credentials, tokens, or personal data in your output.
"""

    def _profile(self) -> str:
        return f"""---
name: {self.profile}
description: The environment this pipeline runs against
github:
  # A GitHub Environment scopes these secrets and can require approval before a run uses them.
  environment: {self.profile}
  secrets: [API_TOKEN]
  vars: [API_URL]
---

api_url=${{API_URL}}
api_token=${{API_TOKEN}}
"""

    # --- code ---

    def _script(self) -> str:
        return '''#!/usr/bin/env python3
"""Collect the items this pipeline works on.

Replace the body with whatever your pipeline actually reads — an API, a queue, a repository. What
matters is the shape: a JSON array whose entries each carry a `key`, which becomes one matrix leg.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def collect(limit: int) -> list[dict[str, str]]:
    return [{"key": f"item-{n}", "title": f"Example item {n}"} for n in range(1, limit + 1)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    items = collect(args.limit)
    output.write_text(json.dumps(items, indent=2) + "\\n", encoding="utf-8")
    print(f"collected {len(items)} item(s) -> {output}")


if __name__ == "__main__":
    main()
'''

    def _script_test(self) -> str:
        return '''"""Every script step runs on every execution, so a regression here is silent."""

from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "collect_items", Path(__file__).parent.parent / "scripts" / "collect-items.py"
)
assert spec and spec.loader
collect_items = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect_items)


def test_every_item_carries_the_key_the_matrix_needs():
    assert all("key" in item for item in collect_items.collect(3))


def test_keys_are_unique():
    keys = [item["key"] for item in collect_items.collect(5)]
    assert len(set(keys)) == len(keys)


def test_the_limit_is_honoured():
    assert len(collect_items.collect(2)) == 2
'''

    def _eval_case(self) -> str:
        # The first eval anybody reads, so it shows both halves of the contract: checks that mean
        # the same thing on every run, and a rubric for the part no substring match can settle.
        return """{
  "input": {
    "key": "item-1",
    "title": "Example item 1"
  },
  "expect": {
    "schema": ["summary"],
    "absent": ["TODO"],
    "rubric": "Two sentences: what the item is, and why it matters. Says nothing the item does not."
  }
}
"""

    # --- repo furniture ---

    def _gitignore(self) -> str:
        # Inherited definitions are resolved state, like a virtualenv: the lock file records which
        # commit, and `lockstep fetch` puts it back. Committing them would make every upstream bump
        # a diff of somebody else's repository.
        return "outputs/\n.pipeline/inherited/\n__pycache__/\n.venv/\n*.pyc\n"

    def _readme(self) -> str:
        return f"""# {self.name}

A pipeline compiled to GitHub Agentic Workflows by [Lockstep](https://github.com/tpouyer/lockstep).

The markdown under `commands/`, `agents/`, `guardrails/` and `profiles/` is the source of truth.
Everything under `.github/workflows/` is generated from it — edit the spec, not the YAML.

## Working on it

```bash
lockstep compile          # regenerate the workflows
lockstep lint             # is the spec well built?
lockstep doctor           # will GitHub accept it?
lockstep compile --check  # what CI runs: committed output must match the spec
```

## Before the first run

```bash
lockstep pin                                    # resolve capability tags to commits
gh secret set API_TOKEN --env {self.profile}    # see SECRETS.md for the full list
gh variable set API_URL --env {self.profile}
gh aw compile                                   # build the agentic workflows' lock files
```

## Customizing

Three tiers, in order of preference:

1. **Edit the spec.** Reordering steps, swapping a technique, changing a prompt.
2. **Add an overlay** in `overlays/github/` for something GitHub-specific the spec does not model.
3. **`lockstep eject <file>`** to take ownership of one generated file. Recorded and tracked, so the
   fork stays visible.
"""


def scaffold(
    root: Path, name: str, profile: str, *, force: bool = False, adopt: tuple[str, ...] = ()
) -> list[str]:
    """Write a new pipeline into `root`, refusing to overwrite anything already there.

    With `adopt`, writes a repository that inherits the shipped pipelines instead of authoring one.
    """
    if not name.replace("-", "").replace("_", "").isalnum():
        raise ScaffoldError(f"{name!r} is not a usable pipeline name", hint="use letters, digits and dashes")

    if adopt:
        from . import library

        available = library.pipelines()
        unknown = [pipeline for pipeline in adopt if pipeline not in available]
        if unknown:
            raise ScaffoldError(
                f"no shipped pipeline named {', '.join(unknown)}",
                hint="available: " + (", ".join(sorted(available)) or "(none)"),
            )
        files = Adopt(name=name, profile=profile, pipelines=tuple(adopt)).files()
    else:
        files = Scaffold(name=name, profile=profile).files()
    existing = [relative for relative in files if (root / relative).exists()]
    if existing and not force:
        raise ScaffoldError(
            f"{len(existing)} file(s) already exist: {', '.join(sorted(existing)[:3])}",
            hint="scaffold into an empty directory, or pass --force to overwrite",
        )

    for relative, content in sorted(files.items()):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return sorted(files)
