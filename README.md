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

## Status

Phase 2 — step-type coverage. See `docs/status.md` for what is implemented and what is deferred.

## Development

```bash
make check      # format, lint, typecheck, test
```
