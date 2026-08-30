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

## What ships today

Six core workflows are the goal. This table is what actually runs, kept honest by a test that
reads it: a row claiming **runs** must name a verb that ships, and a **planned** row must not —
so implementing one without flipping its row fails CI, and so does deleting a feature the table
still advertises.

| Capability | Status | What that means |
|---|---|---|
| Code Review | runs | `review --aspect security`, per-lens prompts, cassette-replayable offline |
| Implement | runs | oneshot and TDD strategies; `/implement` on an issue end to end via the three-job trampoline |
| Bug Fix | runs | `fix` verb; a failed run opens an `ai-generated` issue an agent can pick up, attempts bounded |
| Triage | runs | `triage` from a ticket, `$0` on a local model |
| Backport | runs | deterministic `cherry-pick -x` staged for `apply --base`; `--resolve` lets a model merge conflicts, budget- and approval-gated |
| RFE | planned | roadmap item 25 — rides the triage vertical |
| Flaky-test adapter | planned | roadmap item 26 |
| GitHub | runs | SCM, issues, chat-ops gate, trampolines |
| GitLab | partial | `GitLabScm`/`GitLabIssues` and host-aware `init` ship; no live dogfooded pipeline yet |
| Keyless CI (federation) | runs | GitHub OIDC exchanged at Anthropic; no `ANTHROPIC_API_KEY` in secrets |
| Org standards as a package | runs | `in_lockstep.standards` entry points at `Tier.PLUGIN`; worked example in `examples/acme-standards` |
| Spend controls | runs | per-run predictive budget, rolling daily ceiling, org-limit attestation |
| Ledger + tamper-evidence | runs | orphan-branch records; `report`/`doctor` flag a rewritten history |
| Shared ledger store | planned | `compare_and_set` is declared and refused at `LOCAL` scope; fan-out barriers need `SHARED` |

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
in-lockstep implement --ticket X # read a ticket, stage a change; writes nothing itself
in-lockstep backport --target .. # replay merged commits onto a release line; model only on conflict
in-lockstep triage --ticket X    # classify a ticket; cheap enough for a local model
in-lockstep show-prompt <lens>   # what the model is told, offline, no key
in-lockstep ls                   # the resolved container, middleware, standards and policy
in-lockstep doctor               # are the controls actually in place?
in-lockstep report --by model    # what the ledger adds up to — and whether it was rewritten
in-lockstep history --explain X  # one run's record, every field, in words
in-lockstep egress-manifest      # the hosts a run may dial, for the proxy that enforces it
in-lockstep gate --actor ...     # is this person allowed to fire a chat-ops trigger
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

**Evaluating?** The matrix above says what runs; [getting started](docs/getting-started.md)
shows every command with the output it actually prints, and costs nothing to follow —
`--offline` and `--dry-run` need no key.

**Adopting?** The [cookbook](docs/cookbook.md) is ten recipes of twenty lines or fewer — keyless
CI, org standards as a package, the daily ceiling, chat-ops TDD. Then
[extending](docs/extending.md) for adapters, prompts and strategies, and the
[trampoline contract](docs/trampoline.md) for what a CI file owes the framework on any host.

**Auditing?** The [controls crosswalk](docs/controls-crosswalk.md) is the honest accounting of
what in-process invocation costs — what replaced each substrate control, what is weaker, what
was lost — and [exit gates](design/gates.md) tracks every claimed control against the test that
holds it, including the ones that are `unit only` or `unmet`.

**Why is it like this?** The essays: [design](design/in-lockstep-design.md) and
[ADR 0001](design/adr/0001-pivot-to-runnable-framework.md). Long, and deliberately below the
fold rather than deleted — they are where the decisions above stop being assertions.

Contributions: [CONTRIBUTING.md](CONTRIBUTING.md) ends with a wanted list.

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
