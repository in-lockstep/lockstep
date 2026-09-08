# in-lockstep

An agentic SDLC framework, written in Python, for a repository in any language. Your lifecycle
is executable code: not a manifest, not a
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

## The mission

> Enable teams of software engineers to work together using a framework to keep AI usage
> disciplined and structured, enabling collaborative development work to proceed on the hosted SCM
> of their choice using the provider(s) and model(s) of their choice constrained by the
> process(es) and policy of their choice.

The thirteen objectives in [CLAUDE.md](CLAUDE.md) are that sentence made measurable, and
[design/objectives.md](design/objectives.md) is the ledger over them — joined to the gate ledger,
so an objective cannot read as met while a gate carrying it does not hold.

## What ships today

Six core workflows are the goal. This table is what actually runs, kept honest by a test that
reads it: a row claiming **runs** must name a verb that ships, and a **planned** row must not.
So implementing one without flipping its row fails CI, and so does deleting a feature the table
still advertises.

| Capability | Status | What that means |
|---|---|---|
| Code Review | runs | `review --aspect security`, repeatable so every lens named runs and is recorded; the required check here is one `review/all-lenses` run that fans out over every bound lens and posts one sticky comment per lens; cassette-replayable offline; `/review <lens>` on a pull request from `init --review`, resolved in Python against the lenses the module binds and posted from a job holding no key; a `Lens` a repository enhances, bounds and routes without a fork |
| Implement | runs | oneshot and TDD strategies; `/implement` on an issue end to end via the three-job trampoline; a session bound with `delegation=True` can hand one task to a nested session over a subset of its own tools, on the same spend and the same tape |
| Bug Fix | runs | `fix` verb; a failed run opens an `ai-generated` issue an agent can pick up, attempts bounded |
| Triage | runs | `triage` from a ticket, `$0` on a local model |
| Detected lifecycle | runs | `Lockstep.detect()` reads pyproject, package.json, the Makefile, `Cargo.toml`, `go.mod`, `pom.xml`, `build.gradle`, `Gemfile`/`Rakefile`, `composer.json`, `mix.exs`, `*.csproj`/`*.sln`, `CMakeLists.txt`, `Package.swift` and `BUILD.bazel`, and `detected_bindings` serves Test, Validate, Build and Run from what is actually there: pytest and ruff where they exist, then Makefile targets (`make test`, `make lint`, `make build`, `make run`), then package.json scripts, then what a native manifest guarantees (`cargo test`, `go build ./...`, `mix test`, `dotnet test`, `swift test`, `bazel test //...` inside a workspace, `./mvnw`/`./gradlew` only where the wrapper is committed) or a line in it declares (`composer test` from a `test` script, `bundle exec rake test` from a Rakefile that defines the task, `ctest` where `enable_testing()` was called). A repository with no `lockstep.py` runs on those bindings; `init` writes the same ones into the module it scaffolds. A target that is not in the file is not guessed — and a repository nothing here can serve is told what was looked for rather than left with a silent absence |
| Provisioned environment | runs | `provision` builds the repository's own environment from a file that exists (`uv sync --locked` for a `uv.lock`, `npm ci` for a `package-lock.json`, a requirements.txt into a venv of its own, or the Makefile's own `deps` target); `detected_bindings` binds it first and the scaffolded work jobs run it before `doctor`, with one line that is the same in every repository. A pyproject without a lock binds no Python provisioner, and nothing to provision is `not bound`, never a success |
| Harvesting history into cases | runs | `eval harvest` turns a recording into cases (real requests, real answers, expectations derived from them); `eval run` replays and settles them for nothing, and `eval run --judge` puts every rubric to the bound `Judge` as a recorded `judge/corpus` run, keeping each verdict in a sidecar beside its case. Measures everything below the model; re-testing a changed prompt, and judging a rubric, are real calls |
| Review conversation as context | runs | what a reviewer said on the pull request (the thread, the verdicts, the notes pinned to a line) reaches the next `/fix` or `/implement` as untrusted context, and `/fix` can be asked for *from* the pull request: it resolves to the ticket that pull request was opened for |
| Backport | runs | deterministic `cherry-pick -x` staged for `apply --base`; `--resolve` lets a model merge conflicts, budget- and approval-gated |
| RFE | runs | `rfe --idea` drafts the ticket; a human reads it, and `--create` files it through `TicketSource` |
| Flaky-test adapter | planned | a wanted contribution, sized in [CONTRIBUTING.md](CONTRIBUTING.md); `GATE-TESTGUARD-1` already refuses silencing a test without a ticket, and nothing yet detects or quarantines one |
| GitHub | runs | SCM, issues, chat-ops gate, trampolines |
| GitLab | partial | `GitLabScm`/`GitLabIssues` and `init --host gitlab` ship a file with the four verbs and a published review record, parity-tested against the GitHub trampolines in both directions; no pipeline has ever run it, and only an instance would move this row |
| Keyless CI (federation) | runs | GitHub OIDC exchanged at Anthropic; no `ANTHROPIC_API_KEY` in secrets |
| Org standards as a package | runs | `in_lockstep.standards` entry points at `Tier.PLUGIN`; worked example in `examples/acme-standards` |
| Extension packs | runs | `in_lockstep.extensions` entry points that **offer** rather than apply; `pack describe` derives a receipt, `add` records what you accepted, `pack try` measures it for `$0` |
| Pack catalog | runs | a static `index.toml` in a git repo; `market add`/`search`/`lint`, receipts re-derived locally and refused when they disagree |
| Spend controls | runs | per-run predictive budget, rolling daily ceiling, org-limit attestation |
| Metrics report | runs | `report` reads the ledger back: outcomes, effort, spend, turns per strategy, what it keeps finding. `--html` writes one self-contained page, `--scm` adds merge and issue timings. Every number carries its denominator, and a field nobody measured is a dash |
| Consistency across askers | runs | `report --by actor` splits the ledger by who asked: outcome mix, turns and spend per succeeded run, findings per run, and the spread between askers with every number carrying the runs it came from. Askers are stable pseudonyms unless `--names`; a run nobody is recorded as asking for is a `—` row, never an "unknown" bucket; a local run carries who ran it only where `lockstep.identity = GitAuthor()` opts in |
| Ledger + tamper-evidence | runs | orphan-branch records; `report`/`doctor` flag a rewritten history |
| Improvement loop | runs | `improve` reads the ledger for a finding that keeps coming back, drafts a change to the one declared `Improvable` body it is attributed to, measures the draft against the promoted corpus before opening anything — both arms over the same cases, every rubric put to the `judge` verb on both arms with verdicts kept beside the case and replayed, `—` where nobody judged — and stages the change with its scorecard; `run improve/propose` is the job that holds the write token, and it enforces the open-proposal ceiling where the proposal is opened. It refuses before its first model call unless a trend qualifies, the body is writable by grant, and a promoted case fails against it. `improve --explain` reads the ledger and says what would stop a proposal, opening nothing and spending nothing |
| Judge | runs | the `judge` verb: `AiJudge` bound to `Judge`, routed and priced like any verb; `improve` asks it one question per rubric per arm and `eval run --judge` asks it over the promoted corpus, every verdict recorded and replayed from its sidecar, `—` where nobody judged |
| Park and resume | runs | `ctx.park` and `ctx.human` stop a run at a person; the barrier record lives in the shared ledger, `ls --parked` lists what is waiting, and `in-lockstep resume` (the local form of `resume.yml`) applies the verdict and continues the run from any machine; `ctx.fan_out` runs machine branches at once under one joint ceiling |
| Shared ledger store | runs | `GitLedger(shared=True)` provides `compare_and_set` as a swap on the remote's own ref, so eight runners claiming one key produce one success; one repository's ledger, not a workspace's. The default construction stays `LOCAL` and refuses |

Each row above has a diagram behind it: the [runtime architecture](https://in-lockstep.github.io/lockstep/diagrams/runtime-architecture.html) and one control-flow page per shipped workflow, verb and strategy, indexed in [docs/diagrams](docs/diagrams/README.md). They are interactive pages on the site, and their sources are in the repository.

## Why code rather than configuration

A change to your review policy becomes a diff in a pull request, with blame, history and rollback.
You extend a verb by subclassing, override behaviour by rebinding, and compose cross-cutting
concerns as middleware. Those are language features, and reinventing them in YAML produces a worse
version of each.

The cost is that a container is harder to read than a manifest, which is what `in-lockstep ls` is
for: it prints what will actually run.

## Extensions that travel

Config-as-code makes extension ordinary (subclass a prompt, write a strategy, declare a verb), and
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
in-lockstep review --base ... --aspect security --aspect tests   # review a change; every lens named runs and is recorded
in-lockstep review --ask "/review tests" --pr 42   # the lens a comment named, resolved here; --comment-out writes the body
in-lockstep comment --pr 42 --body-file findings.md   # post what another job composed, from a job with no key
in-lockstep implement --ticket X # read a ticket, stage a change; writes nothing itself
in-lockstep backport --target .. # replay merged commits onto a release line; model only on conflict
in-lockstep triage --ticket X    # classify a ticket; cheap enough for a local model
in-lockstep rfe --idea "..."     # draft a ticket from a rough idea; --create files it
in-lockstep improve              # propose a prompt change the record supports; refuses before spending unless it can
in-lockstep show-prompt <lens>   # what the model is told, offline, no key
in-lockstep ls                   # the resolved container, middleware, standards and policy
in-lockstep provision            # the repository's own environment, from its lockfile; CI runs it first
in-lockstep pack ls              # installed extension packs — offered, not yet in force
in-lockstep market add <url>     # register a catalog; https only, and committed
in-lockstep search <query>       # packs across the catalogs this repository reads
in-lockstep add <pack>           # accept one: re-derive, record, print the lines to paste
in-lockstep pack try <pack>      # measure it on your cases, replaying a cassette, for $0
in-lockstep pack describe        # the receipt: what is bound, what it may do, what proves it
in-lockstep doctor               # are the controls actually in place?
in-lockstep report --by model    # what the ledger adds up to — and whether it was rewritten
in-lockstep report --by actor    # are people getting consistent results? pseudonymous; --names to name them
in-lockstep history --explain X  # one run's record, every field, in words
in-lockstep history --pull       # bring the remote's records onto the local branch; pushes nothing
in-lockstep report --around '#41' # the runs after a merged prompt change against the runs before; no verdict
in-lockstep eval run --judge     # put every rubric to the bound judge, recorded, verdicts kept beside the cases
in-lockstep ls --parked          # the runs waiting on a person, from the shared store
in-lockstep resume --run X --as approved --by NAME   # apply a person's verdict to a parked run and continue it
in-lockstep show-workflow implement   # the source of a shipped process, as imported, no key
in-lockstep egress-manifest      # the hosts a run may dial, for the proxy that enforces it
in-lockstep gate --actor ...     # is this person allowed to fire a chat-ops trigger
in-lockstep eval harvest --from  # turn a recording into cases you can measure against
in-lockstep eval run             # replay them and settle them, for nothing
in-lockstep apply --from-artifact # the privileged half of the two-job trampoline
```

## Working without keys or spend

`--dry-run` proves the wiring. `in-lockstep review --offline` works on a clean install with no
key, no module and no recording of your own: a cassette ships, recorded from a real model call
against a real merged pull request. Replays are deterministic and free, and neither needs a
budget. A run that cannot spend states a ceiling of zero rather than being asked for one.

Cassettes sit at the `LLMInput`/`LLMOutput` seam, so one recorded against a provider replays against
a different one, and they capture tool IO as well as model IO. This is the debugging story, the
testing story and the eval story at once.

## Two things worth knowing before running it unattended

**Model invocation happens in your process.** That is what makes the framework a library rather
than a service, and it removes an execution substrate that was also an egress firewall, an
out-of-process spend ceiling and a privilege split between the process holding a key and the
process able to write. [`docs/controls-crosswalk.md`](docs/controls-crosswalk.md) accounts for
every one of those: what replaced it, what is weaker, and the one that was lost rather than
replaced. `in-lockstep doctor` checks the same list.

**Configuration is loaded from a trusted ref.** `.lockstep/lockstep.py` defines every binding,
policy and protected path. Under review, loading it from the branch being reviewed would let a
change rewrite the constraints that apply to reviewing it, so it comes from the base ref instead.

## Documentation

**Evaluating?** The matrix above says what runs; [getting started](docs/getting-started.md)
shows every command with the output it actually prints, and costs nothing to follow. `--offline`
and `--dry-run` need no key.

**Adopting?** The [cookbook](docs/cookbook.md) is ten recipes of twenty lines or fewer: keyless
CI, org standards as a package, the daily ceiling, chat-ops TDD. Then
[extending](docs/extending.md) for adapters, prompts, strategies and packs, and the
[trampoline contract](docs/trampoline.md) for what a CI file owes the framework on any host.

**Distributing an extension?** [`design/extension-packs.md`](design/extension-packs.md) is the
whole mechanism and the argument for each refusal in it; the worked examples are a prompt pack
([`examples/acme-review-prompts`](examples/acme-review-prompts/)) and a catalog
([`examples/lockstep-index`](examples/lockstep-index/)).

**Auditing?** The [controls crosswalk](docs/controls-crosswalk.md) is the honest accounting of
what in-process invocation costs (what replaced each substrate control, what is weaker, what was
lost), and [exit gates](design/gates.md) tracks every claimed control against the test that holds
it, including the ones that are `unit only` or `unmet`.

**Looking for a picture?** [docs/diagrams](docs/diagrams/README.md) holds seventeen interactive pages: the runtime, every shipped workflow, verb and strategy, the security model, the ledger's path to origin, the learning loop, and the chat-ops job split.

**Why is it like this?** The essays: [design](design/in-lockstep-design.md) and
[ADR 0001](design/adr/0001-pivot-to-runnable-framework.md). Long, and deliberately below the
fold rather than deleted. They are where the decisions above stop being assertions.

Contributions: [CONTRIBUTING.md](CONTRIBUTING.md) ends with a wanted list.

## Installing

One distribution. Provider SDKs are extras: `in-lockstep[anthropic]`, `[openai]`, `[google]`,
`[bedrock]`, `[all]`. A bare install pulls none of them, because cold start matters in CI.

## History

Through `0.1.x` this project was a compiler that lowered a markdown spec into GitHub Agentic
Workflows. From `0.2.0` it is a different product under the same name: the framework this page
describes. It ships below 1.0 because the API is still moving. The compiler line is archived on
the [`compiler-v0.1.x`](https://github.com/in-lockstep/lockstep/tree/compiler-v0.1.x) branch.
[ADR 0001](design/adr/0001-pivot-to-runnable-framework.md) records why, and what the change cost.

## Development

```bash
make check     # format, lint, typecheck, test
```
