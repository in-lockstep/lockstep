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

The body stays a `.md` deliberately. The people who write review prompts are frequently not Python
programmers, and prompt text in a string literal has escaping hazards that prose in a file does
not. It is also a security property: a prompt change proposed by the improvement loop is data
rather than executable code entering your module's import graph.

Bumping `version` matters for a different reason than it looks. Eval identity is a *content hash*
of the composed prompt, not the declared version — so a measurement is correct whether or not you
remember to bump it. `version` is the human label that travels alongside.

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
