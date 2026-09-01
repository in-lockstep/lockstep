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
| Review conversation as context | runs | what a reviewer said on the pull request — the thread, the verdicts, the notes pinned to a line — reaches the next `/fix` or `/implement` as untrusted context, and `/fix` can be asked for *from* the pull request: it resolves to the ticket that pull request was opened for |
| Backport | runs | deterministic `cherry-pick -x` staged for `apply --base`; `--resolve` lets a model merge conflicts, budget- and approval-gated |
| RFE | runs | `rfe --idea` drafts the ticket; a human reads it, and `--create` files it through `TicketSource` |
| Flaky-test adapter | planned | roadmap item 26 |
| GitHub | runs | SCM, issues, chat-ops gate, trampolines |
| GitLab | partial | `GitLabScm`/`GitLabIssues` and host-aware `init` ship; no live dogfooded pipeline yet |
| Keyless CI (federation) | runs | GitHub OIDC exchanged at Anthropic; no `ANTHROPIC_API_KEY` in secrets |
| Org standards as a package | runs | `in_lockstep.standards` entry points at `Tier.PLUGIN`; worked example in `examples/acme-standards` |
| Extension packs | runs | `in_lockstep.extensions` entry points that **offer** rather than apply; `pack describe` derives a receipt, `add` records what you accepted, `pack try` measures it for `$0` |
| Pack catalog | runs | a static `index.toml` in a git repo; `market add`/`search`/`lint`, receipts re-derived locally and refused when they disagree |
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

## Extensions that travel

Config-as-code makes extension ordinary — subclass a prompt, write a strategy, declare a verb — and
for a long time it made extension *local*: what one team wrote could not reach another. An
extension pack is that same code as an installable distribution, and the rule it turns on is short.

**Installing a pack offers it. A line you wrote is what puts it in force.** `in_lockstep.standards`
packages apply themselves, because they can only tighten and the risk is forgetting one. An
extension hands a model write and execute tools and spends money, so its arrival is a diff:
`in-lockstep ls` will not mention a pack until `lockstep.py` names it.

What you can know before you trust one is derived from its code rather than claimed in a file:

```bash
in-lockstep pack describe acme-tdd-pro   # capabilities, imports, guardrails, evidence — no key
in-lockstep pack try acme-tdd-pro --corpus ./our-cases   # measured on YOUR cases, replaying, $0
in-lockstep add acme-tdd-pro             # records what you accepted; prints the lines to paste
```

`add` never writes `.lockstep/lockstep.py` and never installs anything. It records the receipt you
accepted at `.lockstep/packs/<name>.json`, and `doctor` re-derives against it: `DOC170` fails when a
pack may do more than you agreed to, which is the upgrade that would otherwise arrive quietly.

[`docs/extending.md`](docs/extending.md) is the how; [`design/extension-packs.md`](design/extension-packs.md)
is why each refusal is where it is.

## Commands

```bash
in-lockstep run <workflow>       # run it; --recover resumes an interrupted run
in-lockstep review --base ...    # review a change, one lens at a time
in-lockstep implement --ticket X # read a ticket, stage a change; writes nothing itself
in-lockstep backport --target .. # replay merged commits onto a release line; model only on conflict
in-lockstep triage --ticket X    # classify a ticket; cheap enough for a local model
in-lockstep rfe --idea "..."     # draft a ticket from a rough idea; --create files it
in-lockstep show-prompt <lens>   # what the model is told, offline, no key
in-lockstep ls                   # the resolved container, middleware, standards and policy
in-lockstep pack ls              # installed extension packs — offered, not yet in force
in-lockstep market add <url>     # register a catalog; https only, and committed
in-lockstep search <query>       # packs across the catalogs this repository reads
in-lockstep add <pack>           # accept one: re-derive, record, print the lines to paste
in-lockstep pack try <pack>      # measure it on your cases, replaying a cassette, for $0
in-lockstep pack describe        # the receipt: what is bound, what it may do, what proves it
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
[extending](docs/extending.md) for adapters, prompts, strategies and packs, and the
[trampoline contract](docs/trampoline.md) for what a CI file owes the framework on any host.

**Distributing an extension?** [`design/extension-packs.md`](design/extension-packs.md) is the
whole mechanism and the argument for each refusal in it; the worked examples are a prompt pack
([`examples/acme-review-prompts`](examples/acme-review-prompts/)) and a catalog
([`examples/lockstep-index`](examples/lockstep-index/)).

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
