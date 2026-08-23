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

A compiled pipeline references three things, and only the first is a dev dependency:

| Unit | Role | Where it runs |
|---|---|---|
| `lockstep` | the compiler, lint, drift gate | your machine, and the drift gate |
| [`actions/`](actions) | the composite actions every workflow calls | the runner, as `uses:` pinned to a commit |
| [`packages/pipeline-exec`](packages/pipeline-exec) | fan-out, sharding, coverage gates, executors | the runner, as the job `container:` |

`actions/` and `pipeline-exec` share this repository with the compiler because the compiler emits
references to both as literal text: `tests/test_contract.py` parses every emitted invocation against
the real CLI and every input against the real action, so a renamed flag fails a build rather than a
scheduled run. A pipeline points at wherever you published them:

```yaml
capabilities:
  actions: github.com/<owner>/<repo>@v1.0.0    # resolved to a commit by `lockstep pin`
  exec-image: quay.io/<owner>/pipeline-exec    # any registry; resolved to a digest
```

> [!IMPORTANT]
> **Neither has been published anywhere.** `pipeline-fw/pipeline-actions` and its executor image do
> not exist — the examples in this repository pin both to forty zeros, so they compile, lint and
> simulate, and **cannot run on a real runner**. `lockstep doctor` reports it as `DOC015` and
> `lockstep compile` says so on every run. Publishing them, then `lockstep pin`, is what makes a
> pipeline here deployable.

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

**[What goes where](docs/layers.md)** is the rule that keeps application knowledge out of the
framework: which tier code belongs to, and which of the three prompt layers a piece of prose belongs
in.

**[Sharing pipelines across an organization](docs/sharing.md)** is the design for one team owning the
standards and many repositories inheriting them — pinned, reviewable, and impossible to quietly
weaken. Proposed, not built.

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
