# Lockstep

Compiles pipeline definitions — commands, agents, guardrails, skills, contexts, profiles — into
GitHub Agentic Workflows.

The markdown spec stays the single source of truth. Lockstep lowers it onto GitHub-native
primitives: orchestration becomes plain Actions YAML (jobs, `needs:`, matrix fan-out), and each
agent becomes a [gh-aw](https://github.github.com/gh-aw/) agentic workflow. A drift gate recompiles
on every pull request, so committed output can never silently diverge from the spec.

```
spec (commands/ agents/ guardrails/ skills/ contexts/ profiles/ mcp/ + pipeline.yaml + overlays/)
        │  lockstep compile
        ▼
.github/workflows/<command>.yml      orchestrators (plain Actions)
.github/workflows/aw-<agent>.md      agentic workflow sources
.github/workflows/shared/*.md        flattened prompt layers
        │  gh aw compile
        ▼
.github/workflows/aw-<agent>.lock.yml   what actually runs
```

## Usage

```bash
lockstep compile                    # generate workflows
lockstep compile --check            # drift gate: verify committed output matches the spec
lockstep compile --semantic-diff    # report security and cost surface deltas
lockstep show-surface               # every GitHub-target decision in one document
```

## Packages

This is a uv workspace holding two distributions:

| Package | Role | Who installs it |
|---|---|---|
| `lockstep` | the compiler, lint, drift gate | developers, as a dev dependency |
| [`pipeline-exec`](packages/pipeline-exec) | fan-out, sharding, coverage gates, validation | the generated pipeline repo, and nothing else |

They share a repository because the compiler emits `pipeline-exec` invocations as literal text:
`tests/test_contract.py` parses every emitted invocation against the real CLI, so a renamed flag
fails a build rather than a scheduled run.

## Commands

```bash
lockstep init --name=my-pipeline    # scaffold a working pipeline
lockstep pin                        # resolve capability tags to commits
lockstep compile                    # generate the workflows
lockstep compile --check            # drift gate: committed output must match the spec
lockstep lint                       # is the spec well built?
lockstep doctor                     # will GitHub accept it?
lockstep show-surface               # every target decision in one document
lockstep eject <file>               # take ownership of one generated file
```

## Status

All seven phases of the design are implemented. See [docs/status.md](docs/status.md) for what each
covers and what remains open — chiefly round-trip evals across both backends, which need
`pipeline-framework`.

## Development

```bash
make check      # format, lint, typecheck, test
```
