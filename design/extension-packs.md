# Extension packs and the index

**Status:** Proposed · **Date:** 2026-08-31 · **Against:** `main` @ eea5137

Three questions from an adopter — *how do I change a shipped prompt, write my own strategy, add my
own verb* — all have answers today, and all three answers stop at the edge of the repository. What
is missing is not capability. It is that an extension one team writes cannot travel to another, and
that nothing tells a prospective adopter what an extension would do to them before they install it.

This document proposes the smallest distribution mechanism that does not give back the properties
the framework spent PRs #104, #114 and #116 acquiring, and records the three decisions taken while
drafting it.

It also corrects two places where `in-lockstep-design.md` no longer describes the code (§9).

---

## 1. The order everything has to fit into

Four sources may write a decision. They are applied in one order and they win in the opposite one,
and any distribution mechanism is a proposal about where in this stack a stranger's code may land.

| Applied | Source | Tier | May set |
|---|---|---|---|
| 1st | shipped defaults | `SHIPPED` | bodies, layer stacks, `LENSES`, default policy |
| 2nd | installed `in_lockstep.standards` packages | `PLUGIN` | tighten-only policy layers, plugin-tier bindings |
| 3rd | `.lockstep/lockstep.py` | `EXPLICIT` | anything; outranks every plugin regardless of order |
| 4th | the call site — `ctx.do(..., via=tdd)` | call-scoped | one run only; the same capability-keyed gates apply |

Policy is the exception and stays the exception: layers merge tighten-only, so order decides who is
printed, not who wins.

**The constraint this places on everything below.** A pack may write into band two or band three.
Nothing proposed here adds a fifth band, and nothing lets a third party land at `Tier.EXPLICIT` —
`Standards` already forces `Tier.PLUGIN` by offering no tier parameter, and that shape is copied
rather than widened.

---

## 2. What an adopter can do today, and what it costs them

### 2.1 Prompts

Subclassing works, `emphasis` works, `Body.from_file` works, and `PromptLayers.plus` appends house
guardrails after the framework's baseline rather than in place of it. `docs/extending.md` documents
all four.

The defect is in the one command whose entire job is answering "what was the model actually told":

```python
# cli.py, show_prompt_cmd
from .prompts.implement import PROMPTS, implement_layers
from .prompts.review import LENSES, review_layers
from .prompts.triage import TRIAGE_PROMPTS, triage_layers
```

`show-prompt` imports the **shipped** maps and never loads the module. Every override an adopter is
told to write is therefore invisible to it. A team that subclasses `SecurityReviewPrompt`, binds it
through `AiReview(lenses=...)`, and then runs `in-lockstep show-prompt security` is shown the prompt
they replaced. That is worth fixing on its own merits; it is also a precondition for trusting
anything that arrives from outside the repository, because a prompt you cannot render is a prompt
you cannot review.

### 2.2 Strategies

In good shape since #114, #115 and #116. `ImplementStrategy` and `FixStrategy` carry the verb, the
`AGENCY` frozenset and the three session hooks; `__init_subclass__` refuses a subclass that declares
less agency than `_session` hands it; `use()` completes the `WorktreeRunner` wrap and the
`InvokePolicy.under(...)` floor that a hand-written bind could silently omit.

Two gaps remain, both about travel rather than authoring:

- **`id` is a bare string in a flat namespace**, and it lands on the report and in the ledger. Two
  packs shipping `implement/tdd` produce ledger records that cannot be told apart.
- **Nothing says where a third party's fixtures live.** `docs/extending.md` ends the strategy
  section with *"ship fixtures with a new strategy: ten unmeasured strategies are worse than one
  measured"*, and `eval --corpus` already takes a path — but no convention connects the two.

### 2.3 Verbs

`Verb` has been open and interned since the enum was retired, and a declared verb is genuinely
first-class: middleware sees it, `Spend` charges against it, the ledger records it, the kill switch
stops it, and `ls` prints it when it is defined and unbound. Everything keyed on the **adapter**
comes free.

Everything keyed on the **verb** is owed by the author, and it is a set of five:

| Owed | Why it is not automatic |
|---|---|
| a strategy + request dataclass | the framework has no idea what the verb means |
| `models.route(...)` and a `Rate` | `doctor` refuses an unpriced route before a run spends |
| prompts and a layer stack | `<verb>_layers()` is per-verb by construction |
| a corpus family + cassettes | otherwise the verb is unmeasured, and unmeasurable offline |
| a `@workflow` | there is deliberately no CLI subcommand for a custom verb |

None of this is wrong. But five conventions that each author invents alone is the definition of
something that wants a container, and that container is the unit this document proposes.

---

## 3. Packs

A pack is an ordinary Python distribution. There is no manifest format for behaviour, because the
container is still the registration mechanism; `pack.toml` carries a name, a kind and a summary and
nothing that changes what runs.

```
acme-review-prompts/
├── acme_review_prompts/
│   ├── __init__.py               # docstring only, for a prompt pack
│   ├── prompts/security.md
│   └── prompts/house-guardrail.md
├── corpus/review/security/*.json
├── cassettes/security.json
└── pack.toml                     # kind = "prompt"
```

### 3.1 Two entry-point groups, because they answer different questions

```
in_lockstep.standards      installing IS applying
  package ──▶ detect() ──▶ container        (Tier.PLUGIN, tighten-only)

in_lockstep.extensions     installing only OFFERS
  package ──╳── container                   never binds itself
     └─────▶ a line in lockstep.py ─────▶ container
```

The existing group is right to apply itself: a standards package can only tighten, so the real risk
is a repository forgetting it, and `core/standards.py` says so at length. An extension pack is the
opposite risk. It hands a model write and execute tools and it spends money, so its arrival must be
a diff somebody read.

The second group therefore does discovery and nothing else — `pack ls` can name what is installed,
and no `bind` happens until `lockstep.py` says so. This is the same structural property #104 and
#116 established from the other direction: which strategy runs is a bind-time code decision, so
nothing a ticket carries can steer a run toward an approach holding a path grant. A registry that
resolved `--strategy acme/tdd-pro` over installed packages would hand that property back.

### 3.2 The receipt: derived from code, never claimed in a file

A marketplace listing is normally prose an author wrote about their own package. This framework has
the parts to make it a computation: `capabilities` is a load-bearing frozenset checked at class
creation, `PromptLayers.projection()` already emits the section-identity list the characterization
corpus asserts on, `egress-manifest` enumerates the hosts a run may dial, and a corpus is files.

`in-lockstep pack describe` imports the pack in a sandbox and prints that set. Publishing stores the
output beside the index entry; installing recomputes it; a mismatch refuses.

```
$ in-lockstep pack describe acme-tdd-pro --json

  pack          acme-tdd-pro 2.1.0
  kind          strategy
  offers        AcmeTDD  ->  Implement          # request type: the bind key, unambiguous
  id            acme/tdd-pro                    # namespaced; it lands in the ledger
  verb          implement
  capabilities  reads_repo, spends_budget, writes_files, executes_code
  imports       modules                         # AST: what installing puts in the import graph
  policy        contributes nothing             # a strategy pack may not touch the floor
  layers        guardrail:baseline, guardrail:implement/implementing,
                guardrail:acme/house, body:acme/tdd-pro, skill:...
  corpus        14 cases (9 deterministic, 5 rubric)
  cassettes     3
  requires      in-lockstep >=0.9,<0.11
```

**The layer projection is the headline field.** It is the one line that answers *did this pack
quietly replace the framework's guardrails with its own* — a question no amount of README reading
settles, and one the composer can answer exactly, because guardrail position is a property it
guarantees rather than a rendering preference. A projection missing `guardrail:baseline` is not
forbidden. It is flagged, in the index and again at install: the same posture the standards layer
takes, which is visibility of removal rather than impossibility.

**Namespacing follows from the receipt.** `describe` refuses a strategy whose `id` is not prefixed
by the pack name, because that id is what an eval subject and a ledger record key on.

### 3.3 `imports`, and why it is a field rather than a tier

The property worth having for a prompt pack is strong and easy to state: *installing this cannot
execute anything in your process.* Prompt bodies are already data by design — `ai/prompt.py`
argues that a prompt change proposed by the improvement loop must be data rather than executable
code entering the import graph of the module that defines every binding — and a shared prompt should
inherit that.

It is nearly, but not exactly, "contains no Python". `Body.from_file(..., package=...)` resolves
through `importlib.resources`, which needs an importable package, so a data pack ships an
`__init__.py`. The checkable property is that this file and every other `.py` in the distribution
hold nothing but a docstring — an AST walk, cheap and exact.

Making that a **tier** (`kind = "prompt"` refuses any real Python) buys a category-wide guarantee and
immediately meets its awkward case: the pack that wants to ship the three-line `Prompt` subclass
binding its own body, which is the natural thing for a prompt author to include and which makes the
pack code. Then either the tier is a lie or the pack is not a prompt pack.

So `describe` computes `imports: none | modules` for **every** pack, always. One derived fact, no
second concept, and it is arguably more interesting on a strategy pack than on a prompt pack — a
strategy pack reporting `imports: none` would be lying about something. Where it becomes a rule is
§5.

---

## 4. Installing prints; it does not write

```
$ in-lockstep add acme/tdd-pro

  + pyproject.toml   acme-tdd-pro==2.1.0   (hash pinned in uv.lock)
  receipt matches published record. imports: modules.
  capabilities: reads_repo spends_budget writes_files executes_code

  installed, and inert. paste these two lines into .lockstep/lockstep.py:

      from acme_tdd_pro import AcmeTDD
      tdd = lockstep.use(AcmeTDD)

  # until you do, `in-lockstep ls` still says Implement -> Oneshot.
```

`add` resolves the entry, verifies the receipt against the published record, pins the dependency
with a hash, and prints. **It never edits `.lockstep/lockstep.py`**, and there is no flag that makes
it.

The reason is the one that already makes that file load from a trusted ref rather than from the
branch under review: it can rebind any adapter, remove any middleware and grant any tool, which is
why it is the first entry in its own protected-path deny list. "Every line in it was typed by a
person" is a property worth more than two saved keystrokes. The failure mode is good, too — a pack
that is installed but unbound does nothing at all, and `ls` says so, which is a far better state
than a binding nobody noticed arriving.

The one file `add` writes is the dependency pin, which is `uv`'s job and reverses with an uninstall.

---

## 5. The index

A static file in a git repository. No service, no accounts, no ranking to defend.

```toml
# lockstep-index/index.toml
[[pack]]
name         = "acme/tdd-pro"
distribution = "acme-tdd-pro"
index        = "https://pypi.org/simple"
kind         = "strategy"
summary      = "TDD with a mutation-tested red phase"
source       = "https://github.com/acme/tdd-pro"
receipt      = "receipts/acme-tdd-pro-2.1.0.json"   # derived, not written
```

`in-lockstep market add <url>` registers a source; an organisation runs its own alongside the
project's. A name that appears in two sources is a conflict the CLI reports rather than resolves.

### 5.1 Entry criteria, not endorsement

The project's own index is neither curated nor a bare tap. It states criteria, and all of them are
machine-checked off the receipt rather than read off a README:

1. the layer projection retains `guardrail:baseline`;
2. a pack listed as `kind = "prompt"` reports `imports: none`;
3. the pack ships a corpus and at least one cassette, so it can be measured offline before anyone
   spends anything.

The wording is load-bearing. Meeting these says the pack keeps the framework's guardrails and can be
measured before it is trusted; it says nothing whatever about whether the code is good, and the
catalog should say so in that many words. The alternative — a listing that reads as approval —
transfers a judgement this project has not made and cannot make. That is the same distinction
`evaluation/` already insists on when it reports an unjudged rubric as outstanding rather than as a
pass.

An organisation's internal tap carries no criteria, because it answers a different question: an
internal pack is trusted by the fact that someone inside the company published it. `search` groups
results by source so the difference is visible at the point of reading.

---

## 6. Measurement is the install ritual, not a badge

Cassettes sit at the `LLMInput`/`LLMOutput` seam and replay deterministically for free; `eval
--corpus` already takes a path. Together they make a $0 trial the natural first step:

```
$ in-lockstep pack try acme/tdd-pro --corpus ./our-cases

  replaying 3 cassettes, 14 pack cases + 22 of yours    # no key, no spend
  decided      31        outstanding  5  (need a judge)
  pass rate    0.87      on your cases: 0.79
```

This is the answer to the marketplace's hardest social problem. The ranking that matters is computed
by the person deciding, on their own cases, rather than by whoever wrote the listing — and it
inherits the eval module's honesty about what a rubric nobody judged is worth.

---

## 7. Three refusals

**No request-time selection.** There is no `--strategy acme/tdd-pro` resolving through the index.
The CLI's existing `--strategy tdd` stays what it is: a shipped-only convenience for a repository
that has bound nothing.

**No remote prompt bodies at run time.** A pack's markdown is vendored into the environment at
install and read through `importlib.resources`, like every shipped body. A prompt fetched during a
run is an ungoverned input to a model holding write tools.

**No pack-supplied middleware, and no pack policy that loosens.** Extension packs get a facade
narrower than `Standards`: they offer classes and resources, and may not contribute policy at all. A
pack that wants to constrain a repository is a standards package, where contributions merge
tighten-only and print with their source.

---

## 8. Build order

Steps 1 and 2 are worth shipping even if the index is never built, because they are what makes a
locally-written extension debuggable. Nothing before step 4 requires deciding what a marketplace is.

1. **Make an override visible.** `show-prompt` resolves the prompt map off the bound adapter instead
   of importing the shipped one, and grows `--diff` against the shipped body. `ls` prints the lens
   map and layer projection of each AI binding. *(cli.py, adapters/ai/\*; no new concepts.)*
2. **Derive the receipt.** `pack describe`, run first against the current repository, where it is
   immediately useful as an audit of one's own configuration and only incidentally the format an
   index will later store.
3. **Name the container.** The pack layout, `pack.toml`, the `in_lockstep.extensions` entry point
   that offers without applying, and `in_lockstep.packs.pack()` for reading a data pack's resources.
   Ship one first-party example as `examples/acme-standards` did; a prompt pack is cheapest.
   *(The one step that adds public API.)*
4. **Make installing a diff.** `add`: resolve, pin with a hash, re-derive the receipt, refuse on
   drift, print the lines. `doctor` gains DOC170–172.
5. **Publish the catalog.** `index.toml` in a git repo, `market add` / `search`, receipts committed
   beside it. The gh-pages site is the natural home for the first one.
6. **Rank by evidence.** `pack try`. Last because it needs all five above, and the step that makes
   the index worth trusting.

---

## 9. Where this corrects the design document

Two sections of `in-lockstep-design.md` describe a shape the code deliberately left behind. Both
should be amended when this lands; recording it here first so the divergence is not discovered by
somebody implementing from the older text.

**§12 names three entry-point groups** — `in_lockstep.adapters`, `in_lockstep.workflows`,
`in_lockstep.evaluators` — none of which exists. `in_lockstep.standards` shipped instead (roadmap
item 23), for standards only. This document proposes exactly one more, `in_lockstep.extensions`,
with the offer-don't-apply semantics of §3.1. The three named in §12 should be struck rather than
built: an entry point that binds an adapter without a line in `lockstep.py` is the auto-binding this
document refuses.

**§5.7 describes strategy selection by string** — `ctx.do(Implement, spec, strategy="wayfinder")`, a
`StrategySelector` over ticket labels, and a registered default. #104 and #116 replaced that: the
strategy *is* the adapter, `use()` binds it, and `via=` names an object rather than an id. The
substantive loss is §5.7's `StrategySelector` over ticket labels, and it was given up on purpose —
a strategy chosen from ticket text is a strategy an untrusted author can steer. §5.7's *measurement*
claim survives intact and is what §6 above builds on.

---

## 10. Proposed gates and checks

For `design/gates.md` when this is implemented:

| Gate | Status | Assertion |
|---|---|---|
| `GATE-PACK-1` | proposed | An `in_lockstep.extensions` entry point present in the environment produces **no** container binding after `Lockstep.detect()`; the same pack bound by a `lockstep.py` line appears in `ls` at `Tier.EXPLICIT`. |
| `GATE-PACK-2` | proposed | `pack describe` output for a pack whose `layers=` omits the shipped stack has no `guardrail:baseline` entry in its projection, and `add` prints that fact before the binding lines. |
| `GATE-PACK-3` | proposed | A receipt recomputed at install that differs from the published record refuses the install; the run leaves no partially-installed state. |
| `GATE-PACK-4` | proposed | `add` writes no bytes to `.lockstep/lockstep.py` under any flag combination — asserted by mtime and content over the full CLI surface, not by inspecting the happy path. |

For `doctor`, continuing the DOC1xx grouping:

| Check | Fails when |
|---|---|
| `DOC170` | An installed pack's capabilities have widened since the version the module acknowledges. A pack may not silently gain `reaches_network`. |
| `DOC171` | An installed pack's projection is missing `guardrail:baseline`. Warns rather than fails: it is legal and must be visible. |
| `DOC172` | An installed pack is not pinned to a hash. |

`DOC170` implies an acknowledgement spelling — `lockstep.use(AcmeTDD, acknowledged="2.4.0")` is the
obvious one, and it is deliberately in the module rather than in a lockfile, because the thing being
acknowledged is a grant.

---

## 11. Open

- **Does `acknowledged=` belong on `use()`?** It is the right place for the grant and the wrong shape
  for `use()`, whose job is completion rather than policy. An alternative is a separate
  `lockstep.trust(AcmeTDD, "2.4.0")` line that reads as what it is.
- **Prompt packs and `emphasis`.** A data pack cannot ship a `Prompt` subclass, so the version label
  and any emphasis default live in the adopter's repository. Whether `pack.toml` should be allowed to
  carry an advisory emphasis string — read as data, like `Frontmatter` — or whether that starts an
  alternate configuration surface, is unresolved. `ai/prompt.py` is explicit that frontmatter is
  "advisory input to a Python-declared binding, never an alternate configuration surface", which
  argues for no.
- **`pack try` and rubric cases.** A pack's rubric cases need a judge, and a judge costs money, which
  cuts against "measure before you spend". The honest position is that `try` reports them as
  outstanding — but a marketplace where most evidence is outstanding is a marketplace ranking on
  deterministic cases only, and that should be said out loud in the catalog.
