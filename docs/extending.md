# Extending

Extension is ordinary subclassing plus binding. There is no plugin manifest and no registration
DSL, because the container is already the registration mechanism.

## A different adapter

A verb is an interface; anything satisfying it can serve it.

```python
from in_lockstep import Capability, Outcome, Test, Verb
from in_lockstep.core.types import TestReport

class ToxTest:
    verb = Verb.TEST
    capabilities = frozenset({Capability.EXECUTES_CODE, Capability.READS_REPO})

    async def invoke(self, ctx, request: Test) -> Outcome[TestReport]:
        ...

lockstep.bind(Test, ToxTest())
```

`Test` is the request type: workflows do `ctx.do(Test(...))`, and the request's type is what the
binding serves — so the same `Test` names both what a workflow asks for and what your adapter
receives.

`capabilities` is the part people skip and should not. Policy keys off it without knowing anything
about your adapter: whether egress enforcement is mandatory, whether an approval gate applies,
whether retry may re-invoke it. Declaring `EXECUTES_CODE` is how the framework knows to run you
out of process, away from anything holding credentials.

Note `REACHES_NETWORK` exists separately from `WRITES_FILES`. "Read-only" describes mutation, not
transmission — a fetch tool mutates nothing and is still an egress channel.

## A verb of your own

The shipped verbs are not a closed set. A verb is a name — a routing key and a telemetry label —
so declaring one is constructing it, and the request it serves is an ordinary frozen dataclass:

```python
from dataclasses import dataclass

from in_lockstep import Capability, Outcome, Status, Verb

BENCHMARK = Verb("benchmark")

@dataclass(frozen=True)
class Benchmark:
    """The Benchmark request. Workflows do `ctx.do(Benchmark(...))`; a binding decides what runs it."""
    iterations: int = 100

class PyperfBenchmark:
    verb = BENCHMARK
    capabilities = frozenset({Capability.EXECUTES_CODE})

    async def invoke(self, ctx, request) -> Outcome[dict]:
        ...

lockstep.bind(Benchmark, PyperfBenchmark())
outcome = await ctx.do(Benchmark(iterations=1000))
```

`Verb` used to be a closed enum, which made binding a new interface possible and *mislabelled*:
the adapter had to borrow a shipped member, so a benchmark reported itself as `run` in every span,
metric dimension and step id, and shared a strategy namespace with something unrelated. Now the
span says `benchmark`.

Verbs are interned and case-normalised, so `Verb("test") is Verb.TEST` and identity comparisons
keep working. The consequence worth knowing: a typo produces a *distinct* verb rather than
silently aliasing an existing one. `in-lockstep ls` prints any verb that is defined and unbound,
which is the shape that mistake takes — nothing is printed when there are none.

Nothing about a custom verb is second-class. Middleware sees it, `Spend` charges against it,
`capabilities_for` reads its adapter, and the kill switch stops it. What it does *not* get is a
shipped strategy or model route, because those are keyed by verb and the framework has none for a
verb it has never heard of — declare them yourself, or pass the model explicitly.

## A house prompt

Prompts are classes; their bodies are markdown files.

```python
class OurSecurityReview(SecurityReviewPrompt):
    version = "team-3"
    emphasis = "SQLAlchemy 2.x session discipline; no bare excepts"
```

Subclassing and setting `emphasis` keeps the shipped body and adds to it. To replace the prose
entirely, point at your own file:

```python
class OurReview(ReviewPrompt):
    version = "team-1"
    body = Body.from_file("prompts/our-review.md")
```

Writing one is half the job. Installing it goes through `bind`, like every other extension —
a prompt is not a separate kind of thing with a separate registration mechanism:

```python
from in_lockstep.adapters.ai import AiReview, Review
from in_lockstep.prompts.review import LENSES

lockstep.bind(
    Review,
    AiReview(lenses={**LENSES, "security": OurSecurityReview}),
)
```

Spreading `LENSES` keeps the three lenses you did not override. Passing a bare dict replaces the
map entirely, which is what you want when your team ships its own set of aspects — and an unknown
aspect then reports the lenses *this adapter* has, not the ones that happen to ship.

The map is copied at construction, in both directions: a later mutation of `LENSES` cannot reach
an adapter you already bound, and an adapter cannot leak a lens back into the shipped map.

Read what you bound, before it runs:

```bash
in-lockstep show-prompt security          # what a run would send, from the bound adapter
in-lockstep show-prompt security --diff   # what you changed, against the shipped body
in-lockstep show-prompt security --shipped
```

`ls` prints the same thing in summary — every prompt each AI binding composes, starred where it is
not the shipped one, with the guardrail chain underneath so a stack that does not open with the
framework's baseline is visible without a second command.

The body stays a `.md` deliberately. The people who write review prompts are frequently not Python
programmers, and prompt text in a string literal has escaping hazards that prose in a file does
not. It is also a security property: a prompt change proposed by the improvement loop is data
rather than executable code entering your module's import graph.

Bumping `version` matters for a different reason than it looks. Eval identity is a *content hash*
of the composed prompt, not the declared version — so a measurement is correct whether or not you
remember to bump it. `version` is the human label that travels alongside.

## House guardrails

Every AI adapter composes its prompt from layers — guardrails first, then the body, then skills —
and every one takes the stack as `layers=`, the same seam `prompts=`/`lenses=` are. To add your
own "do not" rules to a verb, extend the shipped stack and hand it to the adapter:

```python
from pathlib import Path

from in_lockstep.prompts.implement import implement_layers

house = implement_layers().plus(
    guardrails=(("acme/house", Path("prompts/house-guardrails.md").read_text()),),
)
```

```python
lockstep.bind(Implement, Oneshot(layers=house))
```

`plus` *appends*: your guardrail lands after the framework's baseline and ahead of the body, so
extending the stack cannot quietly drop the shipped constraints, and the position — guardrails
before everything — stays the security property the composer guarantees. Replacing the stack
wholesale is constructing a fresh `PromptLayers`, which is the visible, greppable spelling of
that decision. `emphasis` on a prompt subclass still exists and is the right place for style
guidance; a guardrail is for constraints, and the difference is where it sits — emphasis rides
after the body, guardrails before it.

## Middleware

Cross-cutting behaviour — tracing, budgets, retries, approval — is a middleware chain around every
`ctx.do`. There is no before/after registration API, because `next` is an explicit parameter and
that gives you before, after, around and instead from one hook:

```python
from in_lockstep.core.middleware import ActionCall, Next, capabilities_for
from in_lockstep.core.outcome import Outcome
from in_lockstep.core.verbs import Capability

class FridayFreeze:
    async def __call__(self, ctx: object, call: ActionCall, next: Next) -> Outcome[object]:
        if _is_friday() and Capability.WRITES_FILES in capabilities_for(ctx, call):
            return Outcome.blocked_by("policy.friday_freeze")
        return await next()          # <- no arguments

lockstep.middleware += [FridayFreeze()]
```

Two things in there are easy to get wrong, and both fail quietly rather than loudly.

**`next()` takes no arguments.** The context and the call are already closed over by `compose`, so
`await next(ctx, call)` — the obvious guess — raises `TypeError`. Returning without awaiting it is
*instead*; awaiting it and inspecting the `Outcome` is *after*; wrapping it in a `try` is *around*.

**Capabilities come from `capabilities_for(ctx, call)`, not from the call.** An `ActionCall` names
an *interface*; capabilities belong to whatever is bound to serve it, which is the entire point of
binding. `capabilities_of(call)` type-checks, returns an empty set, and therefore fails **open** —
your gate silently permits everything. That is the one mistake in this section worth memorising.

Order is outermost-first: `middleware[0]` sees the call before `middleware[1]` and sees the
`Outcome` after it. Each layer is one ordinary frame in a traceback, because the chain is folded
by plain closures rather than decorators, so a `pdb` breakpoint lands where you expect.

Two constraints worth knowing before you write one.

**Middleware runs once per `ctx.do`, not once per model turn.** A long agentic loop is a single
`ActionCall`, so a ceiling you enforce here is checked before the loop starts and after it ends,
and never in between. That is why the spend check and the deadline live *inside* `AiInvoker`,
re-evaluated every turn, rather than being middleware. If what you are writing needs to interrupt
a loop in progress, middleware is the wrong layer.

**Some actions must not be re-invoked.** An action declaring `Capability.SPENDS_BUDGET` re-runs a
whole agentic loop and re-pays every turn already spent, so anything that might call `next()` twice
should refuse it — inherit `RefusesBudgetedActions` and check, as `Retry` does.

What you cannot do here is redaction, egress or residency. Those are privileged: they run outside
this chain because `--no-middleware` exists, and a debugging flag must not be able to switch off
the thing keeping credentials out of a committed record.

## Packs — an extension that travels

Everything above is an extension you wrote in your own repository. A pack is the same thing
packaged so another repository can install it: an ordinary Python distribution declaring an
`in_lockstep.extensions` entry point.

```toml
[project.entry-points."in_lockstep.extensions"]
acme-review-prompts = "acme_review_prompts"
```

**Installing a pack offers it. It does not apply it.** That is the one difference from
`in_lockstep.standards` above, and it is deliberate: a standards package may only tighten, so
applying it automatically is safe and forgetting it is the real risk — while an extension hands a
model write and execute tools and pays for a model call. Nothing is in force until a line in your
`lockstep.py` says so, and that file loads from a trusted ref, which is what keeps which strategy
runs from becoming a string a ticket body could eventually reach.

```bash
in-lockstep pack ls                              # offered, not in force
in-lockstep pack describe acme-review-prompts    # what it holds, before you trust it
in-lockstep add acme-review-prompts              # accept it, and print the lines to paste
```

`add` re-derives the receipt from the code that is installed, compares it with what this repository
accepted before, records the result at `.lockstep/packs/<name>.json` — commit that file, it is the
acknowledgement — and prints the lines. It does not write `lockstep.py` and it does not install
anything: putting a stranger's code on your machine belongs in your dependency diff.

A capability the pack did not previously hold is refused until `--accept` says so, because more
agency is the change that should cost a decision. `doctor` re-derives against the record afterwards:
`DOC170` fails on widened capabilities, `DOC171` warns when a bound prompt no longer opens with the
shipped baseline, `DOC172` warns when a pack is installed unpinned.

```python
from in_lockstep.packs import pack

acme = pack("acme-review-prompts")

class OurSecurity(SecurityReviewPrompt):
    version = "acme-1"
    body = acme.body("prompts/security.md")

lockstep.bind(
    Review,
    AiReview(
        lenses={**LENSES, "security": OurSecurity},
        layers=review_layers().plus(guardrails=acme.guardrails("house")),
    ),
)
```

A pack's guardrail is labelled `<pack>/<name>` in the projection, because a projection is read to
answer "whose rule is this" and two packs contributing `house` would otherwise be
indistinguishable in the one artifact meant to tell them apart.

### Measuring one before you trust it

Everything else about a pack can be checked; none of it says whether the pack is any **good**. The
honest answer to that is a measurement you make on your own cases.

```bash
in-lockstep pack try acme-review-prompts --corpus ./our-cases
```

It replays the pack's cassette — no key, no spend — runs its corpus and yours, and counts them
apart, because the number worth installing on is the one measured on your cases.

Read the states, not only the rate:

| State | Means |
|---|---|
| `decided` | a machine settled it; only these feed the pass rate |
| `outstanding` | a rubric, and no judge has answered it |
| `unrecorded` | the cassette holds no answer for this case — an absence of evidence, never a failure |
| `unexercised` | a corpus family a trial cannot drive yet (it drives `review`) |

When nothing was decided there is no pass rate, and the output says which absence that is rather
than printing a zero.

**Somebody has to pay once.** A trial replays what was recorded, so a pack with no cassette cannot
be measured for nothing — which is what the project catalog's fourth criterion is about, and why
`pack try` says so rather than reporting an empty result. The author records it with
`in-lockstep pack try <pack> --record`, once, against a real model, and commits the cassette into
the pack. Recording transmits, so it is subject to the same egress rules as any other real call;
replaying transmits nothing, and the invoker knows it.

The trial composes the pack's `prompts/<aspect>.md` inside the **shipped** layer stack, paired with
`corpus/review/<aspect>-reviewer/`. Your own guardrails are deliberately not applied: measuring a
pack through your configuration would measure your configuration, and two repositories would get
different numbers for the same pack with no way to tell why.

### Finding one: catalogs

A catalog is a static `index.toml` in a git repository — no service, no accounts, no ranking.

```bash
in-lockstep market add acme https://raw.githubusercontent.com/acme/index/main/index.toml
in-lockstep search tdd
```

`search` groups by source, because the difference matters: the project's catalog states entry
criteria and an organisation's internal tap states none — a pack published inside your company is
trusted by that fact, which is a different question and a better answer. A name two catalogs claim
is reported rather than resolved.

Registering a source writes `.lockstep/market.toml`, committed, because a catalog decides where
this repository looks for code. https only: a catalog says what to install, so it is fetched over a
channel that cannot be rewritten in transit.

An entry points at a receipt derived by `pack describe` and committed beside the index, so it
records what the author's *code* did rather than what the author wrote. `add` re-derives the same
receipt locally and refuses a pack that holds more than the catalog published — not behind
`--accept`, because that is not a decision to weigh, it is a listing that does not describe the
code you installed.

Publishing one is committing the file. `in-lockstep market lint index.toml` checks each entry
against the criteria the catalog claims to apply, which is what keeps a criterion from being a
sentence in a README — see [`examples/lockstep-index/`](../examples/lockstep-index/).

### What `pack describe` tells you before you install

Every field is read off something that already declares it, so a listing is a computation rather
than an author's prose. Two are worth reading first.

`imports` says what installing puts in your import graph: `none` when every `.py` the pack ships
holds a docstring and nothing else — derived by walking the AST, not promised by the pack's kind —
`modules` when there is real code, and `unknown` when the distribution could not be resolved to
files, which is not the same as inert. `pack ls` and `pack describe` never import a pack to answer
this; `describe` imports afterwards, and only when it has already reported there is something to
import.

`guardrails_intact` says whether the prompts it offers still open with the framework's baseline.
Replacing the stack wholesale is legal — see *House guardrails* above — and this is where it
becomes visible.

`pack.toml` carries a kind and a summary, and nothing else is accepted. An unknown key is refused
rather than ignored: the moment that file can carry a binding, a policy or a model route, it has
become the alternate configuration surface this framework does not have. The kind is also
cross-checked against what `describe` derived, so a pack that calls itself prose and ships a
strategy says so in its own receipt.

[`examples/acme-review-prompts/`](../examples/acme-review-prompts/) is the worked example — the
cheapest kind: markdown, a corpus that measures it, and one `__init__.py` holding a docstring.

## Organisation standards

Bindings resolve repository-above-organisation, which is right for adapters and wrong for
standards. Standards go on the policy stack instead:

```python
lockstep.contribute(Policy(name="acme-floor", network="deny-all", max_turns=8))
```

Contributions append and only tighten. `deny-all` egress is an irreversible floor; ceilings take
the lowest of several rather than the last read; tool denies union; the strictest scan wins. There
is no removal API.

At one repository, that line lives in `lockstep.py`. At two hundred, a line every repository has
to remember is drift by another name — so standards also travel as an **installable package**: an
`in_lockstep.standards` entry point whose function receives a facade that can `contribute` layers
(stamped with the plugin's source) and `bind` at `Tier.PLUGIN` (the repository's own binds still
win). `Lockstep.detect()` applies every installed one, in entry-point-name order, before your
module's own lines run, and `in-lockstep ls` prints what applied. The worked example is
[`examples/acme-standards/`](../examples/acme-standards/) — a `pyproject.toml` and one function
is the entire org layer.

Be clear about what that buys: **visibility of removal, not impossibility.** A repository can
delete the line that contributes your standard, and a middleware chain cannot bound code that
never calls `ctx.do`. Enforcement that must survive a hostile repository owner lives in a required
CI check — `in-lockstep doctor --strict` — and in provider billing limits, not in a library.

## Models and providers

A verb is routed to a model, and the route is one visible line — `in-lockstep ls` prints it:

```python
lockstep.models.route(Verb.TRIAGE,    "local:qwen3-8b")           # cheap reading, on a laptop
lockstep.models.route(Verb.IMPLEMENT, "anthropic:claude-opus-4-6")
lockstep.models.route(Verb.REVIEW,    "anthropic:claude-sonnet-4-6")
```

A model id is `provider:model`. The shipped providers are `anthropic`, `local` (Ollama),
`bedrock`, `vertex` (Claude on GCP) and `gemini`. Bedrock, Vertex and Gemini authenticate through
their cloud's own credential chain — the AWS chain, GCP application-default credentials — so they
need no `*_API_KEY`; region and project come from the cloud's environment (`AWS_REGION`;
`GOOGLE_CLOUD_PROJECT` with `GOOGLE_CLOUD_LOCATION`). Their SDK is an optional extra, imported only
when you route to one (`in-lockstep[bedrock]`, `[google]`), so a repository that routes to none
pays nothing.

**A cloud provider's model id is the cloud's, not the Anthropic API's**, and the two namespaces do
not overlap: Bedrock names Claude `us.anthropic.claude-sonnet-4-6-v1:0` (or your site's inference
profile), Vertex uses an `@version` suffix. Because pricing keys on the id, a route to one is
unpriced until you say what it costs — and `doctor` refuses an unpriced route *before* the run
spends anything, which is where you find out:

```python
from in_lockstep.ai.pricing import CostTable, Rate, default_table

lockstep.models.route(Verb.REVIEW, "bedrock:us.anthropic.claude-sonnet-4-6-v1:0")
costs = default_table()
costs.add("us.anthropic.claude-sonnet-4-6-v1:0", Rate(3.0, 15.0))   # per million tokens
lockstep.bind(CostTable, costs)
```

To run against your own gateway, or to state a residency policy **in code** rather than infer it
from an environment variable, build the default registry, register into it, and hand it to the
factory:

```python
from in_lockstep.ai.bootstrap import default_registry, invoker_factory
from in_lockstep.llm.interface import DataPolicy, ProviderSettings

registry = default_registry()
registry.register(
    "house",
    lambda settings, creds: OpenAIProvider(settings, creds),
    settings=ProviderSettings(base_url="https://llm.internal.acme"),
    data_policy=DataPolicy.INTERNAL,          # the operator's claim, greppable, not an env var
    endpoint="https://llm.internal.acme",     # residency keys on where the bytes go
)
lockstep.bind(Review, AiReview(invoker_factory("house:acme-7b", registry=registry)))
```

`in-lockstep doctor` reads the routes and warns before a run spends anything if one names a
provider nothing registered or a model nothing prices — the failure happens where it costs
nothing, not at the first call.

## A strategy

The strategy IS the adapter. A binding does not choose a dispatcher configured by a string — it
names the approach itself:

```python
lockstep.workshop = Workshop(commands=Sandbox(image=IMAGE, require_container=True))
tdd = lockstep.use(TDD)             # or Oneshot, or your own class
```

`use` binds the strategy under the request type it serves and finishes constructing it from the
module: the resolved policy floor, the repo root, and the workshop's runner wrapped in a
`WorktreeRunner`. It completes what was left unset and overrides nothing, so
`lockstep.use(TDD(policy=InvokePolicy(max_turns=8)))` keeps the policy you named.

`bind` remains the primitive and the long spelling still works:

```python
lockstep.bind(Implement, TDD(commands=WorktreeRunner(sandbox, root), policy=...))
```

Prefer `use` anyway. The two arguments it fills in are the two a hand-written bind can silently
drop — without `InvokePolicy.under(policy.resolve(), ...)` the contributed policy floor is ignored,
and without the `WorktreeRunner` wrap the container bind-mounts your live tree. Neither omission
appears in `ls`.

Writing one is subclassing `AiStrategy` (which carries the constructor and the per-run session
assembly) and implementing `invoke(ctx, request)`. It imports from the package root:

```python
from in_lockstep.adapters.ai import AGENCY, AiStrategy
```

Declare `id`, which lands on the report so an eval subject and a ledger record can key on the
approach that ran, and `verb`. `capabilities` is the load-bearing frozenset every gate reads off
the bound object, and it is not optional: subclassing `AiStrategy` means being handed `write_file`,
`delete_file` and `run_script` and paying for a model call, so declaring less than `AGENCY` is
refused at class creation. `ApprovalGate`, the budget refusal and `Retry` all key on that set —
an undeclared strategy would be an ungated one, which is why this is an error and not a warning.

Declaring *more* is allowed, and is sometimes right: a set that could execute on some other
configuration must not read as harmless on this one.

Ship fixtures with a new strategy: ten unmeasured strategies are worse than one measured.

Which strategy runs is a bind-time code decision in `lockstep.py`, reviewed like any other line.
There is no request-time selection and no registry id, so nothing a ticket carries — a label, a
comment, a body — can steer a run toward an approach that holds a path grant. What used to be a
registry refusal (a privileged strategy unreachable from untrusted input) is now structural.

Binding can also happen at the call, when the workflow should say what serves a request right at
the execution site:

```python
outcome = await ctx.do(Implement(ticket=await tickets.get(ticket)), via=tdd)
```

`via=` is call-scoped: it never touches the container, so nothing leaks into later calls, and the
same capability-keyed middleware gates the supplied adapter exactly as it would a bound one. It is
still code choosing — `lockstep.py` loads from a trusted ref — and it is an *override* for a verb
the module binds, not a replacement: the startup refusals (`UngatedAgency`, the budget checks)
scan bound adapters, so keep the binding and pass the same instance.

### `Oneshot`

The scaffold's default: one session, one model, a tool set that can read, search, stage writes
and run a command. (`TDD` also ships — a test-first loop, below — but oneshot is the cheap
default.)

```bash
in-lockstep implement --ticket '#42' --approve --budget 2.00 --out .lockstep/change
in-lockstep apply-inline --from-artifact .lockstep/change
```

Two commands, not one, and deliberately: the session **stages** writes into a `ChangeSet` and
touches nothing. Applying it is a separate step that runs the same path guard a second time. On CI
those are two jobs, and only the second holds a write token.

Three things have to be true before it will start, and each is a control keyed off the adapter's
capability declaration rather than anything the strategy configures:

| It refuses with | Because | What to do |
|---|---|---|
| `ApprovalGate` … `UngatedAgency` | A model that can write and spend needs a human in the loop. | Add `ApprovalGate()` to your middleware, or pass `--approve` for an attended local run. |
| `egress.unenforced` | The tool set declares `EXECUTES_CODE`, which makes egress enforcement mandatory. | Run under a host that constrains egress with `IN_LOCKSTEP_EGRESS=enforced`, or `lockstep.bind(EgressPolicy, UnsandboxedEgress())`. |
| `UndeclaredBudget` | Something bound spends money and no ceiling was declared. | `lockstep.budget = Budget(usd=2.00)`, or `--budget`. |

The egress one is the surprise on a laptop, and the opt-out is a binding rather than a flag on
purpose: `UnsandboxedEgress` is named after what it does, so it greps and it reviews.

**`run_script` executes; it does not shell.** Commands arrive as an argv array — no pipes, no
globs, no `&&` — and `argv[0]` must be in an allowlist (`ALLOWED_COMMANDS`). They run through
whatever `CommandRunner` you supply; `--execute` supplies `Sandbox` wrapped in `WorktreeRunner`,
which drops every credential from the child environment, prefers a container with no network, and
runs the command in a **throwaway worktree of HEAD** rather than the live tree. `--no-execute`
withholds the runner while leaving the tool declared, so what policy sees does not change with the
flag.

The worktree is not just an accident of hygiene; it is the control. `Sandbox` bind-mounts its
working directory read-write, so without it a command from an allowlisted program — `python`,
`make`, `node` — could write `.git/hooks` or `.lockstep/lockstep.py` on the real repository, which
is the write path `ChangeGuard` governs for `write_file` but cannot see once a process is running.
Running in a discarded copy means those writes land nowhere that a later run will read. (One
consequence, shared with `TDD`: a linked worktree's `.git` is a gitlink outside a
container's mount, so a command that needs git will not resolve it inside a container — `pytest`,
`ruff` and `mypy` do not care.)

One thing to know before reading a session's transcript: the working tree `run_script` runs against
does **not** contain that session's staged writes — it is HEAD in a copy. It tells the model what
the existing behaviour is, not whether its change works. Verifying a change is what the `apply` half
is for, and `TDD` (above) is what runs the suite against the staged change directly.

`--max-turns` defaults to 40. That is a runaway backstop, not the budget: every turn re-sends the
accumulated history, so the thing that actually stops a long session is the per-turn spend check,
which refuses *before* the call that would cross the ceiling.

### `TDD`

Test-first, enforced by the strategy rather than requested in a prompt. It runs in two model steps
with a real `Test` run between them: it asks for a failing test, **materialises that test in a
throwaway worktree and runs the suite to confirm it is red**, then asks for the implementation and
runs the suite again to confirm green. A test that passes before anything was written stops the run
with `tdd.not_red`; an implementation that leaves the test failing comes back `tdd.not_green` rather
than opening a pull request that does not work.

This is where the `run_script` caveat above stops applying: oneshot's `run_script` sees the tree as
it was, but tdd's verdict comes from `ctx.do(Test(root=…))` against the *materialised*
change, so it reflects the code as proposed. Because it needs to run the suite, `TDD`
requires a `Test` verb bound and refuses up front (`tdd.no_test`) if none is — it will not degrade to
an untested oneshot. Bind it — `lockstep.bind(Implement, TDD(...))` — and `in-lockstep ls` prints
`Implement -> TDD`, so how implementing happens is one visible line. The CLI's `--strategy tdd`
does the same for a repository that has bound nothing.

## The path a project takes

The framework is built around one arc, and each stage is meant to reuse the last rather than
replace it.

**Young — a terminal.** One or two people, no CI to speak of. Processes are `@workflow` functions
in `.lockstep/lockstep.py` and you run them by hand:

```bash
in-lockstep run implement/from-ticket --arg ticket='#59' --approve --budget 2.00
```

`--approve` says you are the human watching. That is a real grant and a weak one, and the ledger
records it as `attended` so it is never confused with a stronger one.

**Growing — hosted triggers.** More people, and the work should start itself. Nothing about the
process changes: an event fires, and CI runs **the same command**.

```yaml
- run: in-lockstep run implement/from-ticket --arg ticket="#${ISSUE}" --approved-by "${ACTOR}"
```

`--approved-by` replaces `--approve` because nobody is watching, so the name *is* the grant and has
to be supplied. Both land on `RunContext.approval`, and `ApprovalGate` reads it from there — which
is what makes this a re-trigger rather than a rewrite. If the two were plumbed differently, moving
to CI would mean reimplementing the process in YAML, which is the failure this arc exists to avoid.

What the CI file adds is only what CI owns: the trigger, the job split, per-job permissions, and
which secret each job holds. One process cannot hold two token scopes, so the split has to live
there.

**Mature — your own verbs and strategies.** `Verb` is an open, interned value type, so
`Verb("benchmark")` is a first-class verb with its own telemetry label, step ids and strategy
namespace. Bind an interface to an adapter, register strategies for it, and `in-lockstep ls` prints
the result. See *A verb of your own* and *A strategy* above.

**Where this is honest about its limits.** The *shapes* are host-agnostic — `Scm`
(`platform/scm`), `TicketSource` (`platform/tickets`) and `LedgerStore` (`core/ports/`) are
protocols, and nothing in `core` knows what GitHub is. Both
hosts now have implementations: `GitHubScm`/`GitHubIssues` and `GitLabScm`/`GitLabIssues` ship, and
`hosted_scm()`/`hosted_tickets()` in `platform/hosted.py` pick the detected host's pair so a
scaffold module runs unedited on either. What GitLab still lacks is the *comment* trigger:
GitLab CI cannot fire a pipeline from an issue comment the way `issue_comment` does on GitHub, so
the write-capable flow there starts from a run-pipeline-with-variables instead — see
[`docs/trampoline.md`](trampoline.md). `in-lockstep gate` takes `--association` as an opaque
string for the same reason it always did: it works against whatever a host calls its access
levels, and on GitLab — which computes no `author_association` — the gate answers from CODEOWNERS
alone.

### Firing it from CI

The rule is one line long: **process goes in `.lockstep/lockstep.py`, CI invokes it.**

```python
@workflow(id="implement/from-ticket")
async def implement_from_ticket(ctx: RunContext, ticket: str, tickets: TicketSource) -> Outcome:
    return await ctx.do(Implement(ticket=await tickets.get(ticket)))
```

```bash
in-lockstep run implement/from-ticket --arg ticket='#59' --budget 2.00
```

The signature is the contract: `ticket` arrives from `--arg ticket=...`, and `tickets` is filled
from the container because its annotation names a bound port — the dispatcher resolves
`TicketSource` so the body never touches `ctx.container`.

`--arg name=value` is repeatable and values arrive as strings, which usefully bounds what a
CLI-runnable workflow can take: one needing a list of tickets takes a label or a path, not the
tickets. Every dispatched run leaves a ledger record carrying its arguments, so "which issue,
which actor" survives the run.

What belongs in the CI file is the trigger, the job split, per-job permissions, and which secret
each job gets. Those are the CI system's to grant and no Python can express them — one process
cannot hold two different token scopes, and keeping a provider key out of the job that can write is
the reason the trampoline has two jobs. Everything else is process, and process in YAML is process
with no tests.

`.github/workflows/implement.yml` is the worked example. A test enforces a budget on how many
lines of shell it may contain, and fails if it starts running `git commit`, `gh pr create` or
`gh issue comment` itself — each of those has a port behind it (`Scm.open_change`,
`TicketSource.comment`) and reaching for the command instead is how lifecycle logic gets back in.

### Firing it from a comment

`.github/workflows/implement.yml` runs a session when an authorized person comments `/implement`
on an issue, using that issue as the ticket. Three jobs, and the shape is the point:

```
gate      →  implement            →  propose
(no key)     (provider key,          (write token,
              contents: read)         no provider key)
```

An `issue_comment` event runs the workflow on the **default branch**, never a contributor's — the
same provenance property `.lockstep/lockstep.py` relies on. So the comment selects a command; it cannot
supply one.

Anyone who can see a repository can comment on it, so the comment is not the authorization. That
is `in-lockstep gate`:

```bash
in-lockstep gate --actor "$LOGIN" --association "$ASSOCIATION" --codeowners .github/CODEOWNERS
```

Exit 0 or 3. It passes an org `MEMBER`/`OWNER`, or anyone named in CODEOWNERS — two sources
answering different questions, since an outside collaborator can own a directory without being in
the org. Bots are refused whatever their association: a trigger a bot can fire is a loop, and this
one spends money on every lap. `COLLABORATOR` is *not* enough on its own; a collaborator who should
qualify is exactly the person CODEOWNERS names, and naming them is a decision somebody makes.

It is a Python function with tests rather than `grep` inside a YAML `if:`, because it is the whole
authorization and YAML review is not a control.

**The gate authorizes the asker, not the issue.** Anyone can file an issue, and a member typing
`/implement` on a drive-by hands that body to a model holding write tools. The ticket stays
`UNTRUSTED_EXTERNAL`, writes are staged, `ChangeGuard` checks them twice, and the result arrives as
a pull request a person reads. The gate bounds who can spend money; it does not bound what the text
says.

For unattended runs, `--approved-by "@login"` replaces `--approve` and records who asked in the
ledger — a grant nobody can be traced to is not much of a grant. Neither is an environment approval
in the system of record; if you want one, the `propose` job declares `environment: implement`, and
adding required reviewers to it in repository settings makes it real.

**What bounds the spend** is the actor gate, a per-issue concurrency group, and `--budget`. There
is no per-day ceiling — see `docs/controls-crosswalk.md`, which records that as a loss rather than
a replacement. A member who wants to spend $2 forty times can.
