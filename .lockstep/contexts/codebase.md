---
name: codebase
description: What this repository is, how it is laid out, and what its checks already catch
---

This repository is **lockstep**: a compiler that turns markdown pipeline specs into GitHub Agentic
Workflows. It is not an application. Almost everything here either produces YAML and markdown for
other repositories to run, or runs inside a workflow that a compile produced.

## What is already caught before a review

Every pull request runs `ruff format`, `ruff check`, `mypy --strict` over `src` and
`packages/pipeline-exec/src`, and the full suite under a 90% coverage floor. A finding those tools
would have made is one the author was told about first, and repeating it costs the review its
credibility.

`tests/test_contract.py` parses every `pipeline-exec` invocation the compiler emits against the real
CLI, and every action input against the real `action.yml`. A renamed flag or a dropped input already
fails the build.

## The layout, and what a change to each part implies

| Path | What it is |
|---|---|
| `src/lockstep/` | the compiler. Distribution `in-lockstep`, import name `lockstep` |
| `src/lockstep/emit/` | everything that produces output. The highest-consequence directory here |
| `src/lockstep/library/pipelines/` | the five pipelines shipped to adopters, inherited rather than copied |
| `packages/pipeline-exec/` | the runtime. Distribution `in-lockstep-exec`, import name `pipeline_exec` |
| `actions/` | composite actions every compiled workflow calls, referenced by commit |
| `tests/golden/` | a committed tree of expected compiler output |
| `examples/` | five worked pipelines, compiled and committed |
| `.github/workflows/` | generated, **except** `ci.yml` and the three release workflows |

Three consequences worth checking a diff against:

**A change under `src/lockstep/emit/` that leaves `tests/golden/` untouched** is either a change
that provably cannot alter output, or a change whose output nobody looked at. Both happen; they are
worth telling apart, and the diff usually says which.

**A change to `actions/` or to `packages/pipeline-exec/` is a change to something already
published.** Consumers pin `actions-v0.1.0` by commit and the executor image by digest, so the
change reaches them only when a new tag is cut — but the compiler and the runtime are versioned
together, and a runtime change that the compiler does not emit a matching invocation for is a break
that shows up in somebody's scheduled run rather than here.

**`src/lockstep/library/` is shipped source.** It is held to rules the rest is not: no scripts, no
`capabilities:` block, models and budgets published as bands rather than fixed values, and no
knowledge of any particular codebase. A library file that hardcodes a model, or that assumes a
repository's layout, breaks every adopter rather than this one.

## The circularity

`.lockstep/` compiles the workflows that gate this repository, including a `/review` compiled by the
compiler being reviewed. So [`ci.yml`](.github/workflows/ci.yml) is **hand-written, permanently**,
and no compiler change can rewrite it: the trusted workflow checks the generated one, and the
generated one does not check itself. A pull request that makes a generated workflow gate less is
only a real change if `ci.yml` still catches what the gate stopped catching.

## The enforcement floor

Much of what this framework is for lives in what the compiled output refuses to allow. Agents get
`permissions: read-all`; writes happen through gh-aw safe outputs or a deterministic step, never
from a prompt; secrets are named per job and never `inherit`; egress is an allow-list; deterministic
steps run with `--cap-drop=ALL --security-opt=no-new-privileges`; the floor is re-asserted after
overlays so no layer can weaken it.

**A diff that widens any of those is the most consequential kind of change this repository
accepts**, whether or not it looks like much. The semantic diff classifies such a change as a
security-surface delta, and it is right to.

## Conventions that are deliberate, and are not findings

- Errors are raised as typed `LockstepError` subclasses carrying a stable code (`DOC015`, `OVL404`),
  never returned as sentinels. The code is part of the interface — reports and tests match on it.
- Comments explain *why*, at length, and frequently exceed the code they sit above. That is the
  house style, and a review asking for less of it is asking for the wrong thing.
- Tests are named as sentences describing the behaviour they pin.
- Documentation under `docs/` is written as prose with an argument, not as reference tables.
- The compiler prefers refusing at compile time over emitting something that fails at 2am. A new
  code path that degrades gracefully where the surrounding code would have raised is worth a
  question.

## What this repository knows about that most do not

The output is workflows that run language models against other people's repositories. When
reasoning about a change, the blast radius is not this process — it is every consumer whose pipeline
recompiles with the changed compiler and every agent that runs under the resulting permissions.
