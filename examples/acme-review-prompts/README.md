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

## Enhancing a shipped lens instead of replacing it

`prompts/security.md` above is a whole body, and `OurSecurity` swaps it in. `prompts/style.md` is
the other kind of file a pack can ship: three paragraphs that ride *after* the shipped body, under
its emphasis heading, with the shipped prose and the shipped key both kept.

```python
# .lockstep/lockstep.py — keep the framework's security lens, add Acme's phrasing to it
from in_lockstep.adapters.ai import AiReview, Review
from in_lockstep.packs import pack
from in_lockstep.prompts.review import LENSES, Lens, SecurityReviewPrompt

acme = pack("acme-review-prompts")

lockstep.bind(
    Review,
    AiReview(lenses={**LENSES, "security": Lens(SecurityReviewPrompt, emphasis=acme.emphasis("style"))}),
)
```

No subclass, and no new name: the key stays `security`, so `review.security` in the ledger, the
sticky comment's marker and any `Improvable` declared against it all survive the upgrade. A pack
that wants to add a lens of its own names it something nobody had (`a11y`), never a shipped name
with a different body — publishing an enhancement under a shipped name silently forks every
consumer's review history.

`style.md` is not a lens, and `pack try` does not treat it as one: a `prompts/<aspect>.md` is
measured only when `corpus/review/<aspect>-reviewer/` sits beside it.

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

The rubric halves need a judge. Until one has answered, they are *outstanding* rather than passed,
which is what `in-lockstep eval run` reports offline. `eval run --judge` puts each rubric to the
bound `Judge` (`lockstep.bind(Judge, AiJudge())`, routed by `lockstep.models.route("judge", ...)`)
and keeps every verdict in a `<case>.verdicts.jsonl` sidecar beside the case, so a rubric judged
once is replayed rather than paid for again. A pack's own numbers are still a starting point
rather than a verdict.
