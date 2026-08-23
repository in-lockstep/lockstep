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

## Start here

**[Getting started](docs/getting-started.md)** walks through building a real pipeline against a live
public API, and explains each part of the framework as you meet it — including what happens once the
output is hosted on GitHub: how changes get reviewed, where output is stored, and how reports survive
long enough to show a trend.

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

**[Extending the framework](docs/extending.md)** covers the two extension points — third-party
builtins in `pipeline-exec`, and your own composite actions — worked through a pipeline that fixes
bugs from an issue tracker and opens pull requests.

**[Implementing an issue by review](docs/implementing-issues.md)** builds a pipeline that turns an
issue into a pull request, and lets reviewers revise the plan or the code with ordinary PR comments
plus a slash command.

**[Publishing a report to GitHub Pages](docs/publishing-reports.md)** builds a triage-report
pipeline, and uses it to look closely at how context, guardrails and skills shape what an agent
produces.

**[Reviewing pull requests on request](docs/reviewing-pull-requests.md)** builds a `/review security
intent` bot — one review per aspect, revised in place, and silent when nothing has changed.

**[Adding a pipeline to a repository you already have](docs/adopting.md)** covers adoption into an
existing project with its own CI, and the security model for pull requests from forks.

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
