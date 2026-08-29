# Extending

Extension is ordinary subclassing plus binding. There is no plugin manifest and no registration
DSL, because the container is already the registration mechanism.

## A different adapter

A verb is an interface; anything satisfying it can serve it.

```python
class ToxTest:
    verb = Verb.TEST
    capabilities = frozenset({Capability.EXECUTES_CODE, Capability.READS_REPO})

    async def invoke(self, ctx, spec: TestSpec) -> Outcome[TestReport]:
        ...

lockstep.bind(Test, ToxTest())
```

`capabilities` is the part people skip and should not. Policy keys off it without knowing anything
about your adapter: whether egress enforcement is mandatory, whether an approval gate applies,
whether retry may re-invoke it. Declaring `EXECUTES_CODE` is how the framework knows to run you
out of process, away from anything holding credentials.

Note `REACHES_NETWORK` exists separately from `WRITES_FILES`. "Read-only" describes mutation, not
transmission — a fetch tool mutates nothing and is still an egress channel.

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
    AiReview(invoker_factory, lenses={**LENSES, "security": OurSecurityReview}),
)
```

Spreading `LENSES` keeps the three lenses you did not override. Passing a bare dict replaces the
map entirely, which is what you want when your team ships its own set of aspects — and an unknown
aspect then reports the lenses *this adapter* has, not the ones that happen to ship.

The map is copied at construction, in both directions: a later mutation of `LENSES` cannot reach
an adapter you already bound, and an adapter cannot leak a lens back into the shipped map.

The body stays a `.md` deliberately. The people who write review prompts are frequently not Python
programmers, and prompt text in a string literal has escaping hazards that prose in a file does
not. It is also a security property: a prompt change proposed by the improvement loop is data
rather than executable code entering your module's import graph.

Bumping `version` matters for a different reason than it looks. Eval identity is a *content hash*
of the composed prompt, not the declared version — so a measurement is correct whether or not you
remember to bump it. `version` is the human label that travels alongside.

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

## Organisation standards

Bindings resolve repository-above-organisation, which is right for adapters and wrong for
standards. Standards go on the policy stack instead:

```python
lockstep.contribute(Policy(name="acme-floor", network="deny-all", max_turns=8))
```

Contributions append and only tighten. `deny-all` egress is an irreversible floor; ceilings take
the lowest of several rather than the last read; tool denies union; the strictest scan wins. There
is no removal API.

Be clear about what that buys: **visibility of removal, not impossibility.** A repository can
delete the line that contributes your standard, and a middleware chain cannot bound code that
never calls `ctx.do`. Enforcement that must survive a hostile repository owner lives in a required
CI check — `in-lockstep doctor --strict` — and in provider billing limits, not in a library.

## A strategy

A binding chooses which adapter serves a verb; a strategy chooses how it goes about the work.

```python
strategies.register("review/ours", Verb.REVIEW, factory=OurReviewStrategy)
strategies.default(Verb.REVIEW, "review/ours")
```

`strategy_id` is part of the eval subject key, so strategies are measured against each other on
the same ground truth. Ship fixtures with a new strategy: ten unmeasured strategies are worse than
one measured.

If your strategy holds a path grant, register it `privileged=True`. Selection can be driven by
ticket labels, and anyone can write a ticket label.
