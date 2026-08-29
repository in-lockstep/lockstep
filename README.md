# in-lockstep

An agentic SDLC framework for Python. Your lifecycle is executable code — not a manifest, not a
spec, and not something that generates a pipeline. The module you write is the thing that runs.

```bash
uv tool install 'in-lockstep[anthropic]'
in-lockstep init
```

```python
# .lockstep/lockstep.py — the whole configuration
from in_lockstep import Lockstep
from in_lockstep.adapters import PytestTest, RuffValidate, Test, Validate
from in_lockstep.middleware import CostBudget, otel

lockstep = Lockstep.detect()
lockstep.bind(Test, PytestTest(args=["-q"]))
lockstep.bind(Validate, RuffValidate())
lockstep.middleware += [otel(), CostBudget(usd=2.00)]
```

## Why code rather than configuration

A change to your review policy becomes a diff in a pull request, with blame, history and rollback.
You extend a verb by subclassing, override behaviour by rebinding, and compose cross-cutting
concerns as middleware — because those are language features, and reinventing them in YAML
produces a worse version of each.

The cost is that a container is harder to read than a manifest, which is what `in-lockstep ls` is
for: it prints what will actually run.

## Commands

```bash
in-lockstep run <workflow>       # run it; --recover resumes an interrupted run
in-lockstep review --base ...    # review a change, one lens at a time
in-lockstep show-prompt <lens>   # what the model is told, offline, no key
in-lockstep ls                   # the resolved container, middleware and policy
in-lockstep doctor               # are the controls actually in place?
in-lockstep report --by model    # what the ledger adds up to: runs, failures, spend
in-lockstep history --explain X  # one run's record, every field, in words
in-lockstep eval report          # the corpus, offline
in-lockstep apply --from-artifact # the privileged half of the two-job trampoline
```

## Working without keys or spend

`--dry-run` proves the wiring. `in-lockstep review --offline` works on a clean install with no
key and no recording of your own: a cassette ships, recorded from a real model call against a real
merged pull request. Replays are deterministic and free. Cassettes sit at the `LLMInput`/`LLMOutput` seam, so one recorded against a provider
replays against a different one, and they capture tool IO as well as model IO. This is the
debugging story, the testing story and the eval story at once.

## Two things worth knowing before running it unattended

**Model invocation happens in your process.** That is what makes the framework a library rather
than a service, and it removes an execution substrate that was also an egress firewall, an
out-of-process spend ceiling and a privilege split between the process holding a key and the
process able to write. [`docs/controls-crosswalk.md`](docs/controls-crosswalk.md) accounts for
every one of those: what replaced it, what is weaker, and the one that was lost rather than
replaced. `in-lockstep doctor` checks the same list.

**Configuration is loaded from a trusted ref.** `.lockstep/lockstep.py` defines every binding, policy and
protected path. Under review, loading it from the branch being reviewed would let a change rewrite
the constraints that apply to reviewing it — so it comes from the base ref instead.

## Documentation

- [Getting started](docs/getting-started.md)
- [Extending](docs/extending.md) — adapters, prompts, organisation standards, strategies
- [Trampoline contract](docs/trampoline.md) — what a CI file owes the framework, on any host
- [Controls crosswalk](docs/controls-crosswalk.md) — what in-process invocation costs, honestly
- [Design](design/in-lockstep-design.md) and [ADR 0001](design/adr/0001-pivot-to-runnable-framework.md)
- [Exit gates](design/gates.md)

## Installing

One distribution. Provider SDKs are extras: `in-lockstep[anthropic]`, `[openai]`, `[google]`,
`[bedrock]`, `[all]`. A bare install pulls none of them, because cold start matters in CI.

## History

Through `0.1.x` this project was a compiler that lowered a markdown spec into GitHub Agentic
Workflows. `1.0` is a different product under the same name; the compiler line is archived at
`compiler-v0.1.x`. [ADR 0001](design/adr/0001-pivot-to-runnable-framework.md) records why, and
what the change cost.

## Development

```bash
make check     # format, lint, typecheck, test
```
