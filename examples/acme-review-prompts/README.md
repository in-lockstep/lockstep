# acme-review-prompts — a prompt pack

The cheapest kind of extension pack: markdown, a corpus that measures it, and one `__init__.py`
holding a docstring. `examples/acme-standards/` is the same idea for the *organisation* layer;
this is the one for prose.

## The difference from a standards package

A standards package applies itself. `Lockstep.detect()` discovers `in_lockstep.standards` entry
points and runs them, because standards can only tighten and the real risk is a repository
forgetting one.

This pack does not. `in_lockstep.extensions` is a discovery group: installing this offers the
prompts, and nothing is in force until a line in `.lockstep/lockstep.py` says so.

```bash
uv add acme-review-prompts
in-lockstep pack ls                              # offered, not in force
in-lockstep pack describe acme-review-prompts    # what it holds, before you trust it
```

```python
# .lockstep/lockstep.py — the line that actually installs it
from in_lockstep.adapters.ai import AiReview, Review
from in_lockstep.packs import pack
from in_lockstep.prompts.review import LENSES, SecurityReviewPrompt, review_layers

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

Two things about that snippet are the design rather than the style.

`review_layers().plus(...)` **appends**, so the shipped baseline stays ahead of the house
guardrail. `in-lockstep show-prompt security --projection` prints the result, and
`pack describe` reports whether `guardrail:baseline` still leads.

The guardrail is labelled `acme-review-prompts/house` rather than `house`, because a projection is
read to answer "whose rule is this" and two packs contributing `house` would otherwise be
indistinguishable in the one artifact meant to tell them apart.

## Why there is no Python in it

`pack describe` reports `imports: none` for this pack — derived by walking the AST of every `.py`
it ships, not promised by its kind. That means installing it puts no code of its own in your
import graph.

Adding a `Prompt` subclass here would be perfectly legal and would flip that field to `modules`.
Which is the point: `imports` is a fact on every pack rather than a tier, because a tier would
have to lie about the pack that ships one small class.

## Ship the corpus with the prose

`corpus/` holds the cases that measure this lens, so a repository can run them before trusting it
and after changing it. Two cases here, and the second is the one that matters: a lens that always
finds something is a lens nobody can act on.

The rubric halves need a judge. Until one has answered, they are *outstanding* rather than passed
— which is what `in-lockstep eval` reports, and why a pack's own numbers are a starting point
rather than a verdict.
